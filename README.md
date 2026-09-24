# SpotiSearch

Interactive CLI to move tracks between Spotify playlists with per-song match confirmation.

## Setup

1. Create an app at https://developer.spotify.com/dashboard, add a redirect URI (e.g. `http://127.0.0.1:8888/callback`).
2. Copy the env template and fill in your own app credentials:

```bash
cp .env.example .env
# then edit .env:
# SPOTIPY_CLIENT_ID=...
# SPOTIPY_CLIENT_SECRET=...
# SPOTIPY_REDIRECT_URI=http://127.0.0.1:8888/callback
```

`.env` and the `.cache` token file are gitignored — never commit them.

3. Install and run:

```bash
pip install -r requirements.txt
python spotify_transfer.py            # default: 5 candidates per track
python spotify_transfer.py --candidates 8
```

`MAX_CANDIDATES` env var also works as the default.

## Web frontend

```bash
uvicorn app:app --port 8888
```

Open http://127.0.0.1:8888 — log in once (reuses the CLI's cached token if you have one), pick or create a playlist, search, click songs to add. Already-added songs show ✓. Port must be 8888 so `/callback` matches the dashboard redirect URI.

## CLI flow

Pick source → pick target (or create new) → for each source track, arrow-key through the top search matches, Enter to pick, confirm the add, or choose Skip. Already-present tracks are skipped automatically. Ctrl+C exits with a partial summary.

Adds are immediate (one API call per confirmed track) rather than batched — progress survives an interrupted run at the cost of a few extra calls.
