# nyc-noise-playlist

Scrapes today's listings from [nyc-noise.com](https://nyc-noise.com), uses Claude to extract artist names from the messy listing text, and adds tracks to a Spotify playlist named `nyc-noise-MM.DD.YY` for today.

## What it does

1. Fetches today's section from nyc-noise.com
2. Sends the listing text to Claude Sonnet 4.6 → returns a JSON array of artist names
3. Prompts you to create an empty Spotify playlist named `nyc-noise-MM.DD.YY` (Spotify removed the create-playlist API endpoint in Feb 2026, so this step is manual — one click)
4. For each artist: searches Spotify, pulls 2 tracks from their most recent albums
5. Adds all tracks to the playlist

## One-time setup

### 1. Install dependencies

```bash
pip3 install -r requirements.txt
```

### 2. Spotify developer app

- Go to https://developer.spotify.com/dashboard → **Create app**
- Redirect URI (exact): `http://127.0.0.1:8765/callback`
- APIs: check **Web API**
- Save, then copy **Client ID** and **Client secret** from Settings

### 3. Anthropic API key

- https://console.anthropic.com → API Keys → Create key

### 4. `.env`

```bash
cp .env.example .env
```

Fill in `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `ANTHROPIC_API_KEY`. Leave `SPOTIFY_REDIRECT_URI` as-is.

## Daily run

```bash
python3 nyc_noise_playlist.py
```

What you'll see:

1. Today's listings are fetched and sent to Claude
2. Extracted artist list is printed
3. **First run only**: browser opens for Spotify auth → log in → "Spotify auth complete"
4. Script prints the expected playlist name (e.g. `nyc-noise-05.15.26`)
5. Open Spotify → create new empty playlist with that exact name → right-click → Share → Copy link
6. Paste the link in the terminal
7. Tracks get added; final summary shows hits, misses, and playlist URL

### Non-interactive mode

If you've already created the playlist, skip the prompt:

```bash
python3 nyc_noise_playlist.py --playlist-url https://open.spotify.com/playlist/...
```

## Caches (in `.nyc-noise/`)

- `spotify_token.json` — OAuth tokens; auto-refreshed
- `artist_cache.json` — artist name → Spotify URIs, makes repeat artists instant
- `playlist_cache.json` — date → playlist ID, so re-running the same day skips the prompt

To force a reset:

```bash
rm .nyc-noise/spotify_token.json    # re-auth Spotify
rm .nyc-noise/artist_cache.json     # re-search all artists
rm .nyc-noise/playlist_cache.json   # forget all known playlists
```

## Configuration

Edit the constants at the top of `nyc_noise_playlist.py`:

- `TRACKS_PER_ARTIST` — default 2, try 1 or 3
- `ANTHROPIC_MODEL` — default `claude-sonnet-4-6`; could downgrade to Haiku for cheaper extraction

## Troubleshooting

| Problem | Fix |
|---|---|
| `Missing SPOTIFY_CLIENT_ID` | `.env` not in same folder as the script |
| Browser doesn't open for auth | Copy the URL printed in terminal into your browser manually |
| `Could not find today's section` | nyc-noise.com's HTML structure changed; check `fetch_today_listings()` |
| `403` when adding tracks | Playlist must be owned by the same Spotify user you logged in as |
| Playlist name doesn't match | Rename it in Spotify, or pass `--skip-name-check` |

## Notes on the design

- **Why Claude for extraction?** The listing format is wildly varied: `"Wolf Eyes, Cholla"` vs `"Ivan Chen, Nathan Nakadegawa-Lee / William Parker / Marc Edwards"` vs `"Wire Festival, Night #2/4: 98dots, CEM, DAX J (live), DJ Healthy..."`. Regex was a losing battle; the LLM handles it cleanly.
- **Why no Get-Artist-Top-Tracks?** Spotify removed that endpoint in Feb 2026. We use search → recent albums → first track of each as a workaround.
- **Why manual playlist creation?** The Create-Playlist endpoint was also removed Feb 2026 for Dev Mode apps. Extended Quota Mode would restore it but requires Spotify approval.
