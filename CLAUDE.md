# nyc-noise-playlist — guidance for Claude Code

Daily tool: scrape nyc-noise.com → extract artists with Claude → build a Spotify playlist named `nyc-noise-MM.DD.YY`.

## Daily run

```bash
python3 nyc_noise_playlist.py
```

The script will pause and ask the user to create a Spotify playlist named exactly `nyc-noise-MM.DD.YY` for today, then paste its URL.

**Do not try to create the playlist for the user.** Spotify's API removed playlist-creation in Feb 2026 for Dev Mode apps. The user creates it manually in the Spotify app, then pastes the URL back. If the user has already created the playlist, you can run:

```bash
python3 nyc_noise_playlist.py --playlist-url <url>
```

## Setup checks

Before running, verify:
- `.env` exists with `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REDIRECT_URI`, `ANTHROPIC_API_KEY`
- Deps installed: `pip3 install -r requirements.txt`

## Common tasks

- **Re-run today** with a different playlist: delete the date key from `.nyc-noise/playlist_cache.json`, create a new playlist in Spotify, re-run.
- **Clear artist cache** (e.g. after Spotify catalog updates): `rm .nyc-noise/artist_cache.json`
- **Re-auth Spotify**: `rm .nyc-noise/spotify_token.json`
- **Inspect today's extraction without writing to Spotify**: not currently supported — would need a `--dry-run` flag.

## Architecture notes

- `fetch_today_listings()` parses the homepage by finding the `<h4>` whose inner `<a href>` ends with `#MMDDYY`, then collects siblings until the next `<h4>`. If it fails with "Could not find today's section", the site's HTML structure changed.
- Artist extraction uses Sonnet 4.6 (`claude-sonnet-4-6`). The prompt is in `EXTRACTION_PROMPT`.
- Spotify's Get-Artist-Top-Tracks endpoint was removed Feb 2026, so the script uses **search artist → recent albums → first track of each**. This is intentional, not a stopgap.
- **Strict match-verification pass.** `search_artist_tracks()` keeps only Spotify results whose name exact-matches (case-insensitive, normalized) the searched name, then calls `verify_artist_match()` — Claude picks the right candidate based on genre relevance (experimental / noise / electronic / free jazz / avant-garde / etc., per `VERIFY_PROMPT`) or rejects all. No fallback to track-search: if no relevant exact-match exists, the artist is skipped rather than risking a false positive like "Freddy K" → Freddie King.
- OAuth uses Authorization Code + PKCE. Refresh tokens persist in `.nyc-noise/spotify_token.json`.
- **Cache versioning.** `CACHE_SCHEMA` is part of the artist-cache key. Bump it whenever matching logic changes so old cached URIs don't poison new results.

## What NOT to do

- Don't commit `.env` or `.nyc-noise/` — both are gitignored for good reason.
- Don't switch to Spotify Client Credentials flow — playlist modification needs user auth.
- Don't switch to deprecated Implicit Grant.
- **Don't try to replace the Claude extraction with regex.** The listing format is too varied; that's why we use the LLM. Several rounds of this were considered and rejected.
- Don't add a "create playlist programmatically" feature — the endpoint is gone in Dev Mode. If asked, suggest applying for Extended Quota Mode with Spotify, or stick with the manual one-click create.
- Don't call audio-features, audio-analysis, recommendations, or related-artists endpoints — all removed Feb 2026.

## Spotify API gotchas

- **Use `/items`, not `/tracks`, for playlist mutations.** `POST /v1/playlists/{id}/tracks` is deprecated and Dev Mode apps now get a bare `403 Forbidden` from it with no `www-authenticate` header — easy to mistake for a scope/ownership problem. The replacement is `POST /v1/playlists/{id}/items` (same `{uris: [...]}` payload, returns `201` + `snapshot_id`). Same rename applies to `DELETE` — the new endpoint also takes a different payload shape.
- **When debugging a Spotify 403, before suspecting scopes/ownership:** verify the endpoint isn't deprecated. Symptom pattern = reads succeed, writes 403, no `www-authenticate` / `X-Message` headers → check current docs for the endpoint path. Design-time notes (including the ones in [CHAT_CONTEXT.md](CHAT_CONTEXT.md)) can go stale; Spotify's 2026 contraction is ongoing.
- **Verify writes by observing concrete state, not opaque version identifiers.** Spotify's `snapshot_id`, ETags, `last-modified` timestamps and similar "did the resource change?" tokens can come back stale from `GET` endpoints immediately after a successful `POST/PUT/DELETE` (CDN caching, eventual consistency between write replica and read replica). General rule: when you want to confirm a write succeeded, compare a concrete, content-derived value before vs after — track count, item list, field value, body hash. Opaque version tokens are fine for *optimistic concurrency* (passing them back in on the next write), but not as a "did my write land?" signal.

## Open improvements (in priority order)

1. **`--dry-run` flag**: extract + search but don't post to Spotify.
2. **Richer extraction**: have Claude return structured event objects (venue, time, artists, headliner) instead of a flat artist list. Enables venue-grouped playlists, headliner-weighted track counts, etc.
3. **Headliner emphasis**: 3 tracks for the first artist in each event, 1 for supporting acts.
4. **Split `/`-joined collaboratives** when no Spotify match exists for the joined string. The extraction prompt currently treats `A / B / C` as one entry, which is correct for canonical groups but produces zero matches for ad-hoc improv lineups; could fall back to searching each member individually.
