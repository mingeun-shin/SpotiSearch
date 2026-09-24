#!/usr/bin/env python3
"""Transfer tracks between Spotify playlists with interactive matching.

Auth: set SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, SPOTIPY_REDIRECT_URI env vars.

Design note — immediate vs batched adds: tracks are added one at a time on
confirmation. Costs more API calls than batching 100 at a time, but progress
survives a mid-run quit/crash and idempotency checks stay simple. For
playlists of a few hundred tracks the extra calls are negligible.
"""

import argparse
import logging
import os
import pathlib
import sys
import time

from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).parent / ".env")

import questionary
import spotipy
from spotipy.oauth2 import SpotifyOAuth

SCOPES = "playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public"
DEFAULT_MAX_CANDIDATES = int(os.environ.get("MAX_CANDIDATES", "5"))
MAX_RETRIES = 3

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("spotify-transfer")


def get_client() -> spotipy.Spotify:
    missing = [v for v in ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET", "SPOTIPY_REDIRECT_URI")
               if not os.environ.get(v)]
    if missing:
        sys.exit(f"Missing env vars: {', '.join(missing)}")
    return spotipy.Spotify(auth_manager=SpotifyOAuth(scope=SCOPES, open_browser=True))


def api_call(fn, *args, **kwargs):
    """Call a Spotipy function with retry on rate limit / transient errors.

    Returns the result, or None if all retries failed (caller logs and skips).
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except spotipy.SpotifyException as e:
            if e.http_status == 429:
                wait = int(e.headers.get("Retry-After", 2)) if e.headers else 2
                log.warning("Rate limited, sleeping %ss (attempt %d/%d)", wait, attempt, MAX_RETRIES)
                time.sleep(wait)
            elif e.http_status and e.http_status >= 500:
                log.warning("Server error %s, retrying (attempt %d/%d)", e.http_status, attempt, MAX_RETRIES)
                time.sleep(2 * attempt)
            else:
                log.error("API error: %s", e)
                return None
        except Exception as e:  # network blips etc.
            log.warning("Request failed (%s), retrying (attempt %d/%d)", e, attempt, MAX_RETRIES)
            time.sleep(2 * attempt)
    log.error("Giving up after %d attempts: %s", MAX_RETRIES, getattr(fn, "__name__", fn))
    return None


def paginate(sp: spotipy.Spotify, first_page) -> list:
    """Collect all items across pages."""
    items = []
    page = first_page
    while page:
        items.extend(page["items"])
        page = api_call(sp.next, page) if page.get("next") else None
    return items


def get_user_playlists(sp: spotipy.Spotify) -> list[dict]:
    first = api_call(sp.current_user_playlists, limit=50)
    if first is None:
        sys.exit("Could not fetch playlists.")
    return paginate(sp, first)


def pick_playlist(playlists: list[dict], prompt: str, allow_create: bool = False,
                  allow_skip: bool = False):
    choices = []
    if allow_skip:
        choices.append(questionary.Choice("🔎 No source — just search & add songs", value="__skip__"))
    for p in playlists:
        if not p:  # API can return null entries
            continue
        total = (p.get("tracks") or {}).get("total")
        label = f"{p['name']}  ({total} tracks)" if total is not None else p["name"]
        choices.append(questionary.Choice(label, value=p))
    if allow_create:
        choices.append(questionary.Choice("➕ Create a new playlist…", value="__create__"))
    return questionary.select(prompt, choices=choices).ask()


def create_playlist(sp: spotipy.Spotify) -> dict | None:
    name = questionary.text("New playlist name:").ask()
    if not name:
        return None
    # POST /me/playlists — the old /users/{id}/playlists endpoint returns 403
    # for development-mode apps since Spotify's Feb 2026 API migration.
    return api_call(sp.current_user_playlist_create, name, public=False)


def get_playlist_track_ids(sp: spotipy.Spotify, playlist_id: str) -> set[str]:
    first = api_call(sp.playlist_items, playlist_id, additional_types=("track",))
    if first is None:
        return set()
    return {
        it["track"]["id"]
        for it in paginate(sp, first)
        if it.get("track") and it["track"].get("id")
    }


def get_source_tracks(sp: spotipy.Spotify, playlist_id: str) -> list[dict]:
    first = api_call(sp.playlist_items, playlist_id, additional_types=("track",))
    if first is None:
        sys.exit("Could not fetch source playlist tracks.")
    return [
        it["track"] for it in paginate(sp, first)
        if it.get("track") and it["track"].get("id")  # skip local/unavailable tracks
    ]


def search_candidates(sp: spotipy.Spotify, track: dict, limit: int) -> list[dict]:
    name = track["name"]
    artist = track["artists"][0]["name"] if track["artists"] else ""
    result = api_call(sp.search, q=f"track:{name} artist:{artist}", type="track", limit=limit)
    items = result["tracks"]["items"] if result else []
    if not items:  # fallback to a looser query
        result = api_call(sp.search, q=f"{name} {artist}", type="track", limit=limit)
        items = result["tracks"]["items"] if result else []
    return items


def fmt(track: dict) -> str:
    artists = ", ".join(a["name"] for a in track["artists"])
    return f"{track['name']} — {artists} [{track['album']['name']}]"


def process_track(sp, track, target, target_ids: set[str], max_candidates: int) -> str:
    """Returns 'added', 'skipped', or 'failed'."""
    label = fmt(track)

    if track["id"] in target_ids:
        log.info("Already in target, skipping: %s", label)
        return "skipped"

    candidates = search_candidates(sp, track, max_candidates)
    if not candidates:
        log.warning("No matches found: %s", label)
        return "failed"

    choices = [questionary.Choice(fmt(c), value=c) for c in candidates]
    choices.append(questionary.Choice("⏭  Skip this track", value="__skip__"))
    chosen = questionary.select(f"Match for: {label}", choices=choices).ask()
    if chosen is None or chosen == "__skip__":  # explicit skip or Ctrl+C
        return "skipped"

    if chosen["id"] in target_ids:
        log.info("Match already in target: %s", fmt(chosen))
        return "skipped"

    if not questionary.confirm(f"Add '{fmt(chosen)}' to '{target['name']}'?", default=True).ask():
        return "skipped"

    result = api_call(sp.playlist_add_items, target["id"], [chosen["id"]])
    if result is None:
        return "failed"
    target_ids.add(chosen["id"])
    return "added"


def search_loop(sp: spotipy.Spotify, target: dict, target_ids: set[str], limit: int) -> int:
    """Free-text search-and-add loop. Returns number of tracks added.

    Plain queries (no field filters) so Spotify's own fuzzy matching handles
    typos, partial titles, and artist-name searches.
    """
    print(f"\nSearch mode → adding to '{target['name']}'. Blank search or Ctrl+C to quit.\n")
    added = 0
    while True:
        query = questionary.text("Search song/artist:").ask()
        if query is None or not query.strip():
            break
        query = query.strip()

        def fetch(offset: int) -> list[dict]:
            result = api_call(sp.search, q=query, type="track", limit=limit, offset=offset)
            return result["tracks"]["items"] if result else []

        items = fetch(0)
        if not items:
            print("No results.")
            continue

        chosen = None
        while True:  # results menu; can page in more results
            choices = []
            for t in items:
                suffix = "  ✓ already in playlist" if t["id"] in target_ids else ""
                choices.append(questionary.Choice(fmt(t) + suffix, value=t))
            choices.append(questionary.Choice("⬇  More results", value="__more__"))
            choices.append(questionary.Choice("↩  New search", value="__new__"))
            chosen = questionary.select(f"Results for '{query}':", choices=choices).ask()
            if chosen == "__more__":
                more = fetch(len(items))
                if not more:
                    print("No more results.")
                else:
                    items.extend(more)
                continue
            break

        if chosen is None or chosen == "__new__":
            continue
        if chosen["id"] in target_ids:
            print("Already in playlist — not re-added.")
            continue
        if api_call(sp.playlist_add_items, target["id"], [chosen["id"]]) is None:
            print("Add failed (see log).")
            continue
        target_ids.add(chosen["id"])
        added += 1
        print(f"✔ Added: {fmt(chosen)}")
    return added


def main():
    parser = argparse.ArgumentParser(description="Transfer tracks between Spotify playlists interactively.")
    parser.add_argument("--candidates", type=int, default=DEFAULT_MAX_CANDIDATES,
                        help=f"Max match candidates per track (default {DEFAULT_MAX_CANDIDATES})")
    args = parser.parse_args()

    sp = get_client()
    playlists = get_user_playlists(sp)
    if not playlists:
        sys.exit("No playlists found.")

    source = pick_playlist(playlists, "Source playlist (to transfer from):", allow_skip=True)
    if source is None:
        sys.exit("Cancelled.")
    if source == "__skip__":
        source = None

    # Spotify only lets you add tracks to playlists you own (or collaborative
    # ones) — followed and Spotify-generated playlists reject writes.
    me = api_call(sp.current_user)
    my_id = me["id"] if me else ""
    candidates = [
        p for p in playlists
        if p and (not source or p["id"] != source["id"])
        and ((p.get("owner") or {}).get("id") == my_id or p.get("collaborative"))
    ]
    target = pick_playlist(candidates, "Target playlist (only ones you can modify):",
                           allow_create=True)
    if target is None:
        sys.exit("Cancelled.")
    if target == "__create__":
        target = create_playlist(sp)
        if target is None:
            sys.exit("Playlist creation failed or cancelled.")

    target_ids = get_playlist_track_ids(sp, target["id"])
    counts = {"added": 0, "skipped": 0, "failed": 0}

    try:
        if source:
            log.info("Fetching tracks…")
            tracks = get_source_tracks(sp, source["id"])
            print(f"\n{len(tracks)} tracks in '{source['name']}' → '{target['name']}'\n")
            if not tracks:
                owner = (source.get("owner") or {}).get("id", "")
                if owner == "spotify":
                    print("This is a Spotify-generated playlist (Blend/Discover/etc.) — "
                          "dev-mode apps can't read its tracks. Going to search mode.")
                else:
                    print("Source playlist has no readable tracks — going straight to search mode.")
            for i, track in enumerate(tracks, 1):
                print(f"[{i}/{len(tracks)}]")
                counts[process_track(sp, track, target, target_ids, args.candidates)] += 1

        counts["added"] += search_loop(sp, target, target_ids, max(args.candidates, 10))
    except KeyboardInterrupt:
        print("\nInterrupted — partial summary below.")

    print(f"\nDone. Added: {counts['added']}  Skipped: {counts['skipped']}  Failed: {counts['failed']}")


if __name__ == "__main__":
    main()
