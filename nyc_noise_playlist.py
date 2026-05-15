#!/usr/bin/env python3
"""
nyc-noise → Spotify playlist
============================
Scrapes today's listings from nyc-noise.com, uses Claude (Sonnet 4.6) to
extract artists from the messy listing text, searches Spotify for each
artist, and adds 2-3 tracks per artist to a user-created playlist named
nyc-noise-MM.DD.YY for today.

See README.md for setup. Daily run:

    python3 nyc_noise_playlist.py
    # or, skip the interactive prompt:
    python3 nyc_noise_playlist.py --playlist-url https://open.spotify.com/playlist/...
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import socketserver
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional

import anthropic
import requests
from bs4 import BeautifulSoup

# ---------- config ----------

# Cache lives next to the script (per-repo, easy for CC to inspect)
CONFIG_DIR = Path(__file__).parent / ".nyc-noise"
TOKEN_FILE = CONFIG_DIR / "spotify_token.json"
ARTIST_CACHE_FILE = CONFIG_DIR / "artist_cache.json"
PLAYLIST_CACHE_FILE = CONFIG_DIR / "playlist_cache.json"

SCOPES = "playlist-modify-public playlist-modify-private"
REDIRECT_PORT = 8765

NYC_NOISE_URL = "https://nyc-noise.com"
ANTHROPIC_MODEL = "claude-sonnet-4-6"
TRACKS_PER_ARTIST = 2  # adjust 1-3

# Bump when the cache representation or matching logic changes, so old caches
# auto-invalidate instead of producing stale wrong matches.
CACHE_SCHEMA = "v2"


# ---------- env loading ----------

def load_env() -> dict:
    env: dict = {}
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    # env vars override .env
    for k in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET",
              "SPOTIFY_REDIRECT_URI", "ANTHROPIC_API_KEY"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


# ---------- spotify oauth (PKCE) ----------

class _OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the ?code=... from Spotify's redirect."""
    received_code: Optional[str] = None
    received_state: Optional[str] = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404); self.end_headers(); return
        qs = urllib.parse.parse_qs(parsed.query)
        _OAuthCallbackHandler.received_code = qs.get("code", [None])[0]
        _OAuthCallbackHandler.received_state = qs.get("state", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h1>Spotify auth complete</h1><p>You can close this tab.</p>")

    def log_message(self, *a, **k):  # silence
        pass


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def spotify_login(client_id: str, redirect_uri: str) -> dict:
    """Run the PKCE auth flow. Returns token dict."""
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)

    auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    })

    httpd = socketserver.TCPServer(("127.0.0.1", REDIRECT_PORT), _OAuthCallbackHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    print("Opening browser for Spotify auth…")
    print(f"  (If it doesn't open, visit: {auth_url})")
    webbrowser.open(auth_url)

    waited = 0
    while _OAuthCallbackHandler.received_code is None and waited < 300:
        time.sleep(0.5); waited += 0.5
    httpd.shutdown()

    if not _OAuthCallbackHandler.received_code:
        sys.exit("Timed out waiting for Spotify auth.")
    if _OAuthCallbackHandler.received_state != state:
        sys.exit("OAuth state mismatch — aborting.")

    resp = requests.post("https://accounts.spotify.com/api/token", data={
        "grant_type": "authorization_code",
        "code": _OAuthCallbackHandler.received_code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    })
    resp.raise_for_status()
    tok = resp.json()
    tok["acquired_at"] = int(time.time())
    return tok


def spotify_refresh(client_id: str, refresh_token: str) -> dict:
    resp = requests.post("https://accounts.spotify.com/api/token", data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    })
    resp.raise_for_status()
    tok = resp.json()
    tok["acquired_at"] = int(time.time())
    if "refresh_token" not in tok:
        tok["refresh_token"] = refresh_token
    return tok


def get_spotify_token(client_id: str, redirect_uri: str) -> str:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        tok = json.loads(TOKEN_FILE.read_text())
        if tok["acquired_at"] + tok["expires_in"] - 60 < time.time():
            tok = spotify_refresh(client_id, tok["refresh_token"])
            TOKEN_FILE.write_text(json.dumps(tok))
        return tok["access_token"]
    tok = spotify_login(client_id, redirect_uri)
    TOKEN_FILE.write_text(json.dumps(tok))
    return tok["access_token"]


# ---------- scrape nyc-noise ----------

def fetch_today_listings() -> tuple[str, str]:
    """
    Returns (date_str, day_block_text) where date_str is MM.DD.YY for today
    and day_block_text is the markdown-ish text under today's heading.
    """
    today = datetime.now()
    date_str = today.strftime("%m.%d.%y")
    anchor_id = today.strftime("%m%d%y")  # e.g. "051526"

    print(f"Fetching {NYC_NOISE_URL}…")
    html = requests.get(NYC_NOISE_URL, timeout=30).text
    soup = BeautifulSoup(html, "html.parser")

    today_h = None
    for h in soup.find_all("h4"):
        a = h.find("a", href=True)
        if a and a["href"].endswith(f"#{anchor_id}"):
            today_h = h
            break
    if not today_h:
        sys.exit(f"Could not find today's section (#{anchor_id}) on the site.")

    chunks = []
    for sib in today_h.find_next_siblings():
        if sib.name == "h4":
            break
        chunks.append(sib.get_text(" ", strip=True))
    block = "\n".join(c for c in chunks if c)
    return date_str, block


# ---------- artist extraction via claude ----------

EXTRACTION_PROMPT = """\
You will receive event listings from a NYC experimental music calendar
(nyc-noise.com). Extract every performing artist, band, or DJ name from
the listings.

Rules:
- Each comma-separated act is one entry: "Wolf Eyes, Cholla" → 2 entries.
- Names joined by "/" indicate a single collaborative group: return the
  full joined string as ONE entry, e.g. "William Parker / Marc Edwards".
- Parenthetical band-member info: keep the outer name only.
  "Moment Machine (Jason Lindner x Currency Audio)" → "Moment Machine".
- Drop parenthetical tags: (live), (DJ), (Album Release), (Record Release),
  (Cassette Release), (CD Release).
- "X b2b Y" DJ sets → one entry "X b2b Y".
- Skip "TBA", "Special Guest", "$50", numeric placeholders.
- Skip venue names, presenter/curator credits ("X presents:", "curated by Y"),
  and festival names that aren't performing entities (e.g. "Wire Festival,
  Night #2/4:" is a header, not an artist — but the names that follow ARE).
- Preserve unusual capitalization, punctuation, and diacritics exactly.

Return ONLY a JSON array of strings. No preamble, no markdown fences.

Listings:
<<<
{listings}
>>>
"""

def extract_artists(client: anthropic.Anthropic, listings_text: str) -> list[str]:
    msg = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4000,
        messages=[{
            "role": "user",
            "content": EXTRACTION_PROMPT.format(listings=listings_text),
        }],
    )
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        artists = json.loads(text)
    except json.JSONDecodeError:
        print("Couldn't parse Claude's response as JSON. Raw output:")
        print(text)
        sys.exit(1)
    seen, out = set(), []
    for a in artists:
        if a not in seen:
            seen.add(a); out.append(a)
    return out


# ---------- spotify search ----------

def load_artist_cache() -> dict:
    if ARTIST_CACHE_FILE.exists():
        return json.loads(ARTIST_CACHE_FILE.read_text())
    return {}

def save_artist_cache(cache: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ARTIST_CACHE_FILE.write_text(json.dumps(cache, indent=2))


VERIFY_PROMPT = """\
You verify Spotify artist matches for a playlist sourced from nyc-noise.com,
a NYC experimental/noise music calendar.

Searched name: "{searched}"

Spotify candidates whose names exact-match (case-insensitive):
{candidates_block}

Rules:
1. RELEVANCE: the artist must plausibly perform music in one of these areas:
   experimental, improvisational, avant-garde, noise, electronic, free jazz,
   ambient, drone, industrial, IDM, techno, house, post-punk, free improvisation,
   modern classical, harsh noise, contemporary composition, leftfield, or other
   underground/non-mainstream music. REJECT mainstream pop, country, religious,
   blues-rock standards, classical-orchestral standards, holiday music, mariachi,
   children's, etc.
2. UNLABELED ARTISTS: if a candidate has no genres listed, low popularity
   (under ~30) is itself a signal this is an obscure underground artist —
   accept it if the name matches exactly and nothing else disqualifies it.
3. MULTIPLE MATCHES: if more than one candidate qualifies, pick the most
   relevant (genres closest to the list in rule 1).

Return ONLY a JSON object on a single line:
  {{"choice": <1-based index of chosen candidate>}}
or, if none qualify:
  {{"choice": null, "reason": "<short reason>"}}
"""


def _normalize_name(s: str) -> str:
    """Lowercase, collapse whitespace, strip common punctuation for exact-match."""
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[’'`´]", "'", s)
    return s


def verify_artist_match(
    client: anthropic.Anthropic, searched: str, candidates: list[dict]
) -> Optional[int]:
    """Ask Claude to pick the right Spotify artist (or reject all).

    Returns the 0-based index into `candidates`, or None to skip the artist.
    """
    if not candidates:
        return None
    lines = []
    for i, a in enumerate(candidates, 1):
        genres = a.get("genres") or []
        genre_str = ", ".join(genres) if genres else "(no genres listed)"
        lines.append(
            f'{i}. "{a["name"]}" — genres: [{genre_str}], popularity: {a.get("popularity", "?")}'
        )
    prompt = VERIFY_PROMPT.format(searched=searched, candidates_block="\n".join(lines))

    try:
        msg = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        data = json.loads(text)
    except (anthropic.APIError, json.JSONDecodeError) as e:
        # Be strict: if verification can't run, skip rather than guess.
        print(f"    (verify error for {searched!r}: {e} — skipping)")
        return None

    choice = data.get("choice")
    if choice is None or not isinstance(choice, int):
        return None
    if not (1 <= choice <= len(candidates)):
        return None
    return choice - 1


def search_artist_tracks(
    name: str, token: str, n_tracks: int, anthropic_client: anthropic.Anthropic
) -> list[str]:
    """
    Find Spotify artist, verify with Claude, then fetch their tracks.

    Returns list of URIs. Strict matching:
      1) /search?type=artist
      2) keep only candidates whose name exact-matches (normalized)
      3) ask Claude to pick the right one by genre relevance, or reject all
      4) /artists/{id}/albums → first track of each recent album

    No fallback to track-search — better to skip than add wrong music.
    """
    headers = {"Authorization": f"Bearer {token}"}

    r = requests.get(
        "https://api.spotify.com/v1/search",
        headers=headers,
        params={"q": name, "type": "artist", "limit": 10},
    )
    if r.status_code != 200:
        return []
    artists = r.json().get("artists", {}).get("items", [])

    norm = _normalize_name(name)
    exact = [a for a in artists if _normalize_name(a["name"]) == norm]
    if not exact:
        return []

    idx = verify_artist_match(anthropic_client, name, exact)
    if idx is None:
        return []
    artist = exact[idx]
    artist_id = artist["id"]

    r = requests.get(
        f"https://api.spotify.com/v1/artists/{artist_id}/albums",
        headers=headers,
        params={"include_groups": "album,single", "limit": 10},
    )
    if r.status_code != 200:
        return []
    albums = r.json().get("items", [])
    if not albums:
        return []

    uris = []
    seen_track_names = set()
    for alb in albums:
        if len(uris) >= n_tracks:
            break
        r = requests.get(
            f"https://api.spotify.com/v1/albums/{alb['id']}/tracks",
            headers=headers, params={"limit": 1},
        )
        if r.status_code != 200:
            continue
        tracks = r.json().get("items", [])
        if not tracks:
            continue
        t = tracks[0]
        key = t["name"].lower()
        if key in seen_track_names:
            continue
        seen_track_names.add(key)
        uris.append(t["uri"])
    return uris


# ---------- playlist prompt ----------

PLAYLIST_URL_RE = re.compile(r"playlist[/:]([A-Za-z0-9]{22})")

def load_playlist_cache() -> dict:
    if PLAYLIST_CACHE_FILE.exists():
        return json.loads(PLAYLIST_CACHE_FILE.read_text())
    return {}

def save_playlist_cache(cache: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PLAYLIST_CACHE_FILE.write_text(json.dumps(cache, indent=2))


def parse_playlist_id(raw: str) -> str:
    raw = raw.strip()
    m = PLAYLIST_URL_RE.search(raw)
    pid = m.group(1) if m else raw
    if not re.fullmatch(r"[A-Za-z0-9]{22}", pid):
        sys.exit(f"That doesn't look like a valid Spotify playlist ID: {pid}")
    return pid


def prompt_for_playlist(date_str: str) -> str:
    expected_name = f"nyc-noise-{date_str}"
    cache = load_playlist_cache()
    if date_str in cache:
        print(f"Using cached playlist ID for {date_str}: {cache[date_str]}")
        return cache[date_str]

    print()
    print("=" * 60)
    print(f"  Please create a Spotify playlist named exactly:")
    print(f"  → {expected_name}")
    print("=" * 60)
    print()
    raw = input("Paste the playlist URL or ID: ")
    playlist_id = parse_playlist_id(raw)
    cache[date_str] = playlist_id
    save_playlist_cache(cache)
    return playlist_id


def verify_playlist_name(playlist_id: str, expected_name: str, token: str) -> None:
    r = requests.get(
        f"https://api.spotify.com/v1/playlists/{playlist_id}",
        headers={"Authorization": f"Bearer {token}"},
        params={"fields": "name,owner"},
    )
    r.raise_for_status()
    actual = r.json()["name"]
    if actual != expected_name:
        print(f"⚠  Playlist is named '{actual}', expected '{expected_name}'.")
        if input("Continue anyway? [y/N] ").lower() != "y":
            sys.exit(0)


def add_tracks(playlist_id: str, uris: list[str], token: str) -> None:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    for i in range(0, len(uris), 100):
        chunk = uris[i:i+100]
        r = requests.post(
            f"https://api.spotify.com/v1/playlists/{playlist_id}/items",
            headers=headers, json={"uris": chunk},
        )
        if not r.ok:
            print(f"  ✗ Failed to add chunk: {r.status_code} {r.text}")
            r.raise_for_status()


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser(description="Build today's nyc-noise Spotify playlist.")
    parser.add_argument(
        "--playlist-url",
        help="Skip the interactive prompt and use this playlist URL or ID.",
    )
    parser.add_argument(
        "--skip-name-check",
        action="store_true",
        help="Don't verify the playlist name matches nyc-noise-MM.DD.YY.",
    )
    args = parser.parse_args()

    env = load_env()
    for k in ("SPOTIFY_CLIENT_ID", "SPOTIFY_REDIRECT_URI", "ANTHROPIC_API_KEY"):
        if not env.get(k):
            sys.exit(f"Missing {k} — set it in .env or as env var.")

    # 1. scrape
    date_str, listings = fetch_today_listings()
    print(f"\nToday is {date_str}. Found {len(listings)} chars of listings.\n")
    if not listings.strip():
        print("No events listed today. Nothing to do.")
        return

    # 2. extract artists via Claude
    print("Asking Claude to extract artists…")
    anthropic_client = anthropic.Anthropic(api_key=env["ANTHROPIC_API_KEY"])
    artists = extract_artists(anthropic_client, listings)
    print(f"Got {len(artists)} unique artists:\n")
    for a in artists:
        print(f"  • {a}")
    print()

    # 3. spotify token + playlist
    token = get_spotify_token(env["SPOTIFY_CLIENT_ID"], env["SPOTIFY_REDIRECT_URI"])
    if args.playlist_url:
        playlist_id = parse_playlist_id(args.playlist_url)
        cache = load_playlist_cache()
        cache[date_str] = playlist_id
        save_playlist_cache(cache)
    else:
        playlist_id = prompt_for_playlist(date_str)
    if not args.skip_name_check:
        verify_playlist_name(playlist_id, f"nyc-noise-{date_str}", token)

    # 4. resolve URIs
    cache = load_artist_cache()
    all_uris = []
    hits, misses = [], []
    for a in artists:
        cache_key = f"{a}::{TRACKS_PER_ARTIST}::{CACHE_SCHEMA}"
        if cache_key in cache:
            uris = cache[cache_key]
            print(f"  ⌐ {a} → {len(uris)} (cached)")
        else:
            uris = search_artist_tracks(a, token, TRACKS_PER_ARTIST, anthropic_client)
            cache[cache_key] = uris
            time.sleep(0.05)
            print(f"  {'✓' if uris else '✗'} {a} → {len(uris)} tracks")
        if uris:
            hits.append(a); all_uris.extend(uris)
        else:
            misses.append(a)
    save_artist_cache(cache)

    # dedupe URIs
    seen, deduped = set(), []
    for u in all_uris:
        if u not in seen:
            seen.add(u); deduped.append(u)

    # 5. add to playlist
    if deduped:
        print(f"\nAdding {len(deduped)} tracks to playlist…")
        add_tracks(playlist_id, deduped, token)

    # 6. summary
    print("\n" + "─" * 50)
    print(f"Done. Playlist: nyc-noise-{date_str}")
    print(f"  Tracks added: {len(deduped)}")
    print(f"  Hits ({len(hits)}): {', '.join(hits) or '—'}")
    if misses:
        print(f"  Misses ({len(misses)}): {', '.join(misses)}")
    print(f"  → https://open.spotify.com/playlist/{playlist_id}")


if __name__ == "__main__":
    main()
