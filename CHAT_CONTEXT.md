# Chat Context — design conversation

Reconstructed summary of the conversation that produced this tool. Not a verbatim transcript — just the design decisions and rationale, for handoff to Claude Code or future-you.

## Original ask

User wanted a tool to create a Spotify playlist from each day's nyc-noise.com rundown.

## Spotify Web API exploration

Started by exploring the Spotify Web API. Key findings:

- REST API at `https://api.spotify.com/v1/`. OAuth 2.0 for auth.
- Auth flows: Client Credentials (server-to-server, public data only), Authorization Code (full user access via backend), **Authorization Code + PKCE** (full access, no client secret needed — best for native/CLI tools), Implicit Grant (deprecated, avoid).
- **February 2026 was a major API contraction.** Endpoints removed:
  - `POST /users/{user_id}/playlists` (Create Playlist) — gone in Dev Mode
  - `GET /artists/{id}/top-tracks` — gone
  - `GET /recommendations` — gone
  - audio-features, audio-analysis — gone
  - related-artists, new-releases, featured-playlists — gone
  - 30-second preview URLs — gone from most objects
- What survived: catalog reads, search, user library, playback control, playlist reads, **adding/removing tracks from existing playlists**.
- Rate limits: rolling 30-second window per app. Dev Mode has a 25-user cap. Extended Quota Mode requires Spotify approval.

## Adding tracks to a playlist (the surviving endpoint)

- `POST /v1/playlists/{playlist_id}/tracks`
- Body: `{"uris": [...], "position": 0}`. Up to 100 URIs per request; paginate beyond.
- Scopes: `playlist-modify-public` and/or `playlist-modify-private`
- Auth must be user-context (not Client Credentials). User must own the playlist or be a collaborator.
- Returns `snapshot_id` (opaque version ID).
- Gotchas: dupes added freely (dedupe client-side if you care), `spotify:local:...` rejected, `403` if no write access.

## Pipeline design

1. Scrape today's section from nyc-noise.com (anchors are `#MMDDYY`)
2. Extract artist names from the listing text
3. For each artist → Spotify search → URIs
4. Add URIs to a pre-existing playlist

## The artist extraction problem

Listing text is wildly varied:
- `"Moment Machine (Jason Lindner x Currency Audio), Eucademix (Yuka Honda), Aanandi (DJ)"` — band names with members in parens, plus DJ tags
- `"Ivan Chen, Nathan Nakadegawa-Lee / William Parker / Marc Edwards"` — commas separate acts, slashes separate group members
- `"Matt Evans' Aquatic House (Album Release), Chris Ryan Williams"` — possessive + project + tag
- `"Wire Festival, Night #2/4: 98dots, CEM, DAX J (live), DJ Healthy..."` — festival header with 15 comma-separated DJs
- `"7pm: [TBA]"` — placeholder, no artist

**Decision: don't try to regex this.** Send the day's text to Claude API with a structured-output prompt, get back a JSON array of artist names. Sonnet 4.6 (`claude-sonnet-4-6`) chosen over Haiku for reliability on edge cases. Cost is negligible (~2k input tokens per day).

The prompt is `EXTRACTION_PROMPT` in the script. Key rules: comma = separate acts; `/` = same group; strip parenthetical tags; preserve `b2b` DJ pairings as one entry; skip TBA/venue names/curator credits.

## The Get-Artist-Top-Tracks problem

That endpoint died Feb 2026, but it's the natural pick-best-track entry point. Workaround in `search_artist_tracks()`:
1. Search artists by name, get artist ID
2. Get the artist's recent albums (`/artists/{id}/albums?include_groups=album,single`)
3. Pull the first track from each of the top N albums
4. If artist not found, fall back to raw track-search

Imperfect but deterministic and rate-limit-friendly.

## The create-playlist problem

`POST /users/{id}/playlists` also died Feb 2026 in Dev Mode. Options considered:
- **Pre-create a rotating pool** of 30 empty playlists, cycle through them — rejected as tedious
- **Apply for Extended Quota Mode** — too much friction, uncertain approval
- **Single playlist, replace contents daily** — works, no archive
- **Prompt the user to create today's playlist manually** ← **chosen**

The chosen flow: script computes today's expected name (`nyc-noise-05.15.26`), prompts user to create the playlist with that exact name, validates the name matches when given the URL/ID, then proceeds. Each day's playlist persists in the user's Spotify library as a natural archive.

## User decisions (from a multi-choice prompt)

- **Playlist behavior**: New playlist per day, auto-named by date (`nyc-noise-MM.DD.YY`)
- **Tracks per artist**: 2–3 (script defaults to 2 via `TRACKS_PER_ARTIST = 2`)
- **Language**: Python
- **Model**: Sonnet 4.6 for everything

## Caches

Three JSON files in `.nyc-noise/` next to the script:
- `spotify_token.json` — access + refresh tokens
- `artist_cache.json` — `"<artist>::<n_tracks>"` → list of Spotify URIs (cuts repeat-artist lookups)
- `playlist_cache.json` — date → playlist ID (re-running same day skips the prompt)

## Why a `CLAUDE.md` was added

User wanted to run the tool via Claude Code. `CLAUDE.md` tells CC the daily command, what NOT to do (don't try to create playlists programmatically, don't replace LLM extraction with regex), and the architecture rationale. Also added a `--playlist-url` CLI flag so CC can drive the script non-interactively after the user creates the playlist.

## Open improvements (not yet implemented)

1. Match-verification pass — after Spotify returns a candidate, ask Claude `{searched_for, got_back}` → "is this the right artist?" to catch false positives like NYC noise act "Tanya" → Tanya Tucker
2. `--dry-run` flag
3. Structured event objects from Claude (venue, time, artists, headliner) → enables venue grouping or headliner-weighted track counts
4. Headliner emphasis: 3 tracks for the first listed artist per event, 1 for supporting acts
