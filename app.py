#!/usr/bin/env python3
"""Web frontend for spotify-transfer.

Run:  uvicorn app:app --port 8888
Then open http://127.0.0.1:8888

Uses the same env vars as the CLI (SPOTIPY_CLIENT_ID / SECRET / REDIRECT_URI).
The redirect URI must be http://127.0.0.1:8888/callback (same one registered
in the Spotify dashboard). Reuses the CLI's cached token (.cache) if present.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import spotipy
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from spotipy.oauth2 import SpotifyOAuth

SCOPES = "playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public"
HERE = Path(__file__).parent

app = FastAPI(title="spotify-transfer")
oauth = SpotifyOAuth(scope=SCOPES, open_browser=False)


def get_client() -> spotipy.Spotify | None:
    token = oauth.cache_handler.get_cached_token()
    if not token:
        return None
    if oauth.is_token_expired(token):
        try:
            token = oauth.refresh_access_token(token["refresh_token"])
        except Exception:
            return None
    return spotipy.Spotify(auth=token["access_token"])


def err(msg: str, status: int = 502) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


def slim_track(t: dict) -> dict:
    images = t["album"].get("images") or []
    return {
        "id": t["id"],
        "name": t["name"],
        "artists": ", ".join(a["name"] for a in t["artists"]),
        "album": t["album"]["name"],
        "image": images[-1]["url"] if images else None,  # smallest
    }


# ---------- pages / auth ----------

@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


@app.get("/api/status")
def status():
    return {"authed": get_client() is not None}


@app.get("/login")
def login():
    return RedirectResponse(oauth.get_authorize_url())


@app.get("/callback")
def callback(code: str = ""):
    if not code:
        return err("Missing code from Spotify", 400)
    oauth.get_access_token(code, as_dict=False)  # caches the token
    return RedirectResponse("/")


# ---------- API ----------

@app.get("/api/playlists")
def playlists():
    sp = get_client()
    if not sp:
        return err("Not authenticated", 401)
    try:
        me = sp.current_user()["id"]
        items, page = [], sp.current_user_playlists(limit=50)
        while page:
            items.extend(page["items"])
            page = sp.next(page) if page.get("next") else None
        out = [
            {"id": p["id"], "name": p["name"],
             "total": (p.get("tracks") or {}).get("total")}
            for p in items
            if p and ((p.get("owner") or {}).get("id") == me or p.get("collaborative"))
        ]
        return {"playlists": out}
    except spotipy.SpotifyException as e:
        return err(str(e))


class NewPlaylist(BaseModel):
    name: str


@app.post("/api/playlists")
def create_playlist(body: NewPlaylist):
    sp = get_client()
    if not sp:
        return err("Not authenticated", 401)
    try:
        p = sp.current_user_playlist_create(body.name, public=False)
        return {"id": p["id"], "name": p["name"], "total": 0}
    except spotipy.SpotifyException as e:
        return err(str(e))


@app.get("/api/playlists/{pid}/track_ids")
def track_ids(pid: str):
    sp = get_client()
    if not sp:
        return err("Not authenticated", 401)
    try:
        ids, page = [], sp.playlist_items(pid, additional_types=("track",))
        while page:
            ids.extend(it["track"]["id"] for it in page["items"]
                       if it.get("track") and it["track"].get("id"))
            page = sp.next(page) if page.get("next") else None
        return {"ids": ids}
    except spotipy.SpotifyException as e:
        return err(str(e))


@app.get("/api/search")
def search(q: str, offset: int = 0, limit: int = 12):
    sp = get_client()
    if not sp:
        return err("Not authenticated", 401)
    if not q.strip():
        return {"tracks": []}
    try:
        result = sp.search(q=q.strip(), type="track", limit=limit, offset=offset)
        return {"tracks": [slim_track(t) for t in result["tracks"]["items"]]}
    except spotipy.SpotifyException as e:
        return err(str(e))


class AddBody(BaseModel):
    playlist_id: str
    track_id: str


@app.post("/api/add")
def add(body: AddBody):
    sp = get_client()
    if not sp:
        return err("Not authenticated", 401)
    try:
        sp.playlist_add_items(body.playlist_id, [body.track_id])
        return {"ok": True}
    except spotipy.SpotifyException as e:
        return err(str(e))
