"""YouTube Music access.

Searching uses *unauthenticated* clients, so the matching phase can never
modify the account. The only write operation in this tool is
:meth:`Liker.like`, which needs the browser credentials file.
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import requests
from ytmusicapi import YTMusic
from ytmusicapi.navigation import SECTION_LIST, nav
from ytmusicapi.parsers.search import get_search_params, parse_search_results

from .log import logger
from .matcher import Decision, decide
from .models import Candidate, SpotifyTrack, Status
from .textnorm import analyze_title, similarity

DEFAULT_AUTH_PATH = Path.home() / ".config" / "ytm-import" / "browser.json"


class SearchBackend(Protocol):
    def search_songs(self, query: str, lang: str) -> list[dict[str, Any]]: ...


class RateLimiter:
    """Allow at most one call per ``interval`` seconds across threads."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            time.sleep(delay)


def retry(fn: Callable[[], Any], attempts: int = 4, base_delay: float = 2.0) -> Any:
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            if i == attempts - 1:
                raise
            delay = base_delay * 2**i
            logger.warning("request failed (%s: %s), retrying in %.0fs", type(e).__name__, e, delay)
            time.sleep(delay)


class YTMusicSearch:
    """Song search via ytmusicapi.

    ytmusicapi's ``search(filter=...)`` drops every result when the UI
    language is not English (it compares the localized shelf title "曲" with
    "song"), so the request is sent and parsed here directly.
    """

    def __init__(self, interval: float = 0.35) -> None:
        self._clients = {
            "ja": YTMusic(language="ja", location="JP"),
            "en": YTMusic(language="en", location="JP"),
        }
        self._limiter = RateLimiter(interval)
        self._album_cache: dict[str, dict[str, Any]] = {}
        self._cache_lock = threading.Lock()

    def _search(self, query: str, lang: str, filter: str) -> list[dict[str, Any]]:
        client = self._clients[lang]
        body = {"query": query, "params": get_search_params(filter, None, False)}

        def call() -> dict[str, Any]:
            self._limiter.wait()
            return client._send_request("search", body)

        response = retry(call)
        try:
            tabs = response["contents"]["tabbedSearchResultsRenderer"]["tabs"]
            sections = nav(tabs[0]["tabRenderer"]["content"], SECTION_LIST)
        except (KeyError, IndexError, TypeError):
            return []
        results: list[dict[str, Any]] = []
        for section in sections:
            shelf = section.get("musicShelfRenderer")
            if not shelf or "musicResponsiveListItemRenderer" not in shelf["contents"][0]:
                continue
            results.extend(parse_search_results(shelf["contents"], filter[:-1], None))
        return results

    def search_songs(self, query: str, lang: str) -> list[dict[str, Any]]:
        return [r for r in self._search(query, lang, "songs") if r.get("videoId")]

    def album_tracks(self, album: str, artist: str, title: str = "") -> list[dict[str, Any]]:
        """Tracks of the YouTube Music album matching the Spotify album name.

        Some labels' catalogs are barely searchable as songs (tracks are
        unavailable or only exist as music videos), but the album page still
        lists what can be played. Tracks playable only with Music Premium are
        hidden there; for those resembling ``title`` the video ID is recovered
        from the album's playlist on regular YouTube and verified.
        """
        albums = self._search(f"{album} {artist}".strip(), "ja", "albums")
        out: list[dict[str, Any]] = []
        for a in [a for a in albums if a.get("browseId") and similarity(a.get("title") or "", album) >= 0.8][:2]:
            data = self._album(a["browseId"])
            album_info = {"name": data.get("title") or "", "id": a["browseId"]}
            tracks = list(data.get("tracks", []))
            # Hidden (Premium-only) tracks, and tracks shown as a music video in place of the audio.
            hidden = [i for i, t in enumerate(tracks)
                      if (not t.get("videoId") or t.get("videoType") == "MUSIC_VIDEO_TYPE_OMV")
                      and title and similarity(t.get("title") or "", title) >= 0.6]
            if hidden and data.get("audioPlaylistId"):
                ids = self._web_playlist_ids(data["audioPlaylistId"])
                if len(ids) == len(tracks):  # same order as the album; otherwise we cannot map positions
                    for i in hidden:
                        if ids[i] == tracks[i].get("videoId"):
                            continue
                        found = self._verify_hidden(tracks[i], ids[i])
                        if found and tracks[i].get("videoId"):
                            tracks.append(found)  # keep the music video as another candidate
                        elif found:
                            tracks[i] = found
            for t in tracks:
                if not t.get("videoId"):
                    continue  # not playable on YouTube Music
                out.append({**t, "album": album_info, "artists": t.get("artists") or data.get("artists") or []})
        return out

    def _web_playlist_ids(self, playlist_id: str) -> list[str]:
        """Video IDs of a playlist as listed on www.youtube.com, including Premium-only
        and currently unplayable videos (the "show unavailable videos" view)."""
        body = {"context": {"client": {"clientName": "WEB", "clientVersion": "2.20261020.00.00",
                                       "hl": "ja", "gl": "JP"}},
                "browseId": "VL" + playlist_id, "params": "wgYCCAA="}

        def call() -> dict[str, Any]:
            self._limiter.wait()
            r = requests.post("https://www.youtube.com/youtubei/v1/browse?prettyPrint=false", json=body, timeout=20)
            r.raise_for_status()
            return r.json()

        ids: list[str] = []

        def walk(o: Any) -> None:
            if isinstance(o, dict):
                lockup = o.get("lockupViewModel")
                if lockup and lockup.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO" and lockup.get("contentId"):
                    ids.append(lockup["contentId"])
                elif "playlistVideoRenderer" in o and o["playlistVideoRenderer"].get("videoId"):
                    ids.append(o["playlistVideoRenderer"]["videoId"])
                else:
                    for v in o.values():
                        walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        try:
            walk(retry(call))
        except Exception as e:
            logger.warning("youtube playlist %s unavailable: %s", playlist_id, e)
        return list(dict.fromkeys(ids))

    def _verify_hidden(self, track: dict[str, Any], video_id: str) -> dict[str, Any] | None:
        """Confirm a recovered ID really is that album track (title and length)."""

        def call() -> dict[str, Any]:
            self._limiter.wait()
            return self._clients["ja"].get_song(video_id)

        try:
            song = retry(call)
        except Exception as e:
            logger.warning("get_song %s failed: %s", video_id, e)
            return None
        details = song.get("videoDetails", {})
        playability = song.get("playabilityStatus", {})
        playable = playability.get("status") == "OK"
        premium = "Premium" in (playability.get("reason") or "")
        seconds = int(details.get("lengthSeconds") or 0)
        if similarity(details.get("title") or "", track.get("title") or "") < 0.9:
            return None
        if track.get("duration_seconds") and abs(seconds - track["duration_seconds"]) > 2:
            return None
        # Logged out, many of these only say "動画を再生できません", yet a logged-in browser
        # shows them as Premium-only. A verified official audio track hidden from the album
        # page is therefore treated as Premium-only (inferred when the reason does not say so).
        official = details.get("musicVideoType") == "MUSIC_VIDEO_TYPE_ATV"
        if not (playable or premium or official):
            return {**track, "videoId": video_id, "isAvailable": False, "videoType": details.get("musicVideoType"),
                    "duration_seconds": seconds or track.get("duration_seconds")}
        return {**track, "videoId": video_id, "isAvailable": True, "premiumOnly": not playable,
                "premiumInferred": not playable and not premium,
                "videoType": details.get("musicVideoType"), "duration_seconds": seconds or track.get("duration_seconds")}

    def video(self, video_id: str) -> dict[str, Any] | None:
        """A video found elsewhere (e.g. a web search), as a search-result-like dict."""

        def call() -> dict[str, Any]:
            self._limiter.wait()
            return self._clients["ja"].get_song(video_id)

        song = retry(call)
        details = song.get("videoDetails", {})
        if details.get("videoId") != video_id:
            return None
        playability = song.get("playabilityStatus", {})
        playable = playability.get("status") == "OK"
        premium = "Premium" in (playability.get("reason") or "")
        official = details.get("musicVideoType") == "MUSIC_VIDEO_TYPE_ATV"
        return {"videoId": video_id, "title": details.get("title") or "",
                "artists": [{"name": details.get("author") or ""}],
                "duration_seconds": int(details.get("lengthSeconds") or 0) or None,
                "videoType": details.get("musicVideoType"),
                "isAvailable": playable or premium or official,
                "premiumOnly": not playable and (premium or official),
                "premiumInferred": not playable and not premium and official}

    def _album(self, browse_id: str) -> dict[str, Any]:
        with self._cache_lock:
            if browse_id in self._album_cache:
                return self._album_cache[browse_id]

        def call() -> dict[str, Any]:
            self._limiter.wait()
            return self._clients["ja"].get_album(browse_id)

        data = retry(call)
        with self._cache_lock:
            self._album_cache[browse_id] = data
        return data


def to_candidate(result: dict[str, Any]) -> Candidate:
    return Candidate(
        video_id=result["videoId"],
        titles=[result.get("title") or ""],
        artists=[a["name"] for a in result.get("artists") or [] if a.get("name")],
        album=(result.get("album") or {}).get("name") or "",
        duration_s=result.get("duration_seconds"),
        video_type=result.get("videoType"),
        is_available=result.get("isAvailable", True),
        premium_only=bool(result.get("premiumOnly")),
        premium_inferred=bool(result.get("premiumInferred")),
    )


class CandidatePool:
    """Collects candidates by videoId, merging data seen in several searches/locales.

    Japanese-locale names are preferred for display; English-locale names are
    kept as aligned aliases so romanized Spotify credits can still match.
    """

    def __init__(self) -> None:
        self.by_id: dict[str, Candidate] = {}
        self._has_ja: set[str] = set()

    def add(self, results: list[dict[str, Any]], lang: str, isrc_query: bool = False) -> None:
        for i, r in enumerate(results):
            c = to_candidate(r)
            c.isrc_hit = isrc_query and i == 0
            existing = self.by_id.get(c.video_id)
            if existing is None:
                self.by_id[c.video_id] = c
                if lang == "ja":
                    self._has_ja.add(c.video_id)
                continue
            existing.isrc_hit |= c.isrc_hit
            if existing.duration_s is None:
                existing.duration_s = c.duration_s
            if lang == "ja" and c.video_id not in self._has_ja:
                # First Japanese sighting of a candidate found via English search.
                self._has_ja.add(c.video_id)
                existing.alt_artists = existing.artists
                existing.artists = c.artists
                existing.titles = [t for t in c.titles if t not in existing.titles] + existing.titles
                existing.album = c.album or existing.album
                continue
            for t in c.titles:
                if t not in existing.titles:
                    existing.titles.append(t)
            if lang != "ja" and not existing.alt_artists and c.artists != existing.artists:
                existing.alt_artists = c.artists

    def candidates(self) -> list[Candidate]:
        return list(self.by_id.values())


def build_queries(track: SpotifyTrack) -> list[tuple[str, str, bool]]:
    """(query, lang, is_isrc) in the order they are tried."""
    info = analyze_title(track.title)
    title = track.title
    artists = " ".join(track.artists[:3])
    first = track.artists[0] if track.artists else ""
    queries: list[tuple[str, str, bool]] = []
    if track.isrc:
        queries.append((track.isrc, "ja", True))
    queries.append((f"{title} {artists}".strip(), "ja", False))
    if info.base != title.casefold() or len(track.artists) > 1:
        queries.append((f"{info.base} {first}".strip(), "ja", False))
    # Singer/version given only in the album name, e.g. "百色リメイン (双葉&香子ver.)"
    album_labels = [x for x in analyze_title(track.album).labels if x not in info.labels] if track.album else []
    if album_labels:
        queries.append((f"{info.base} {' '.join(album_labels)} {first}".strip(), "ja", False))
    # English locale: romanized names for artists credited in Latin script on Spotify.
    if track.isrc:
        queries.append((track.isrc, "en", True))
    queries.append((f"{title} {artists}".strip(), "en", False))
    return queries


def search_track(backend: SearchBackend, track: SpotifyTrack, premium: str = "include",
                 ) -> tuple[Decision, list[Candidate], list[str]]:
    """Run queries until one candidate is a confident match or queries run out."""
    pool = CandidatePool()
    used: list[str] = []
    decision = decide(track, [], premium)
    for query, lang, is_isrc in build_queries(track):
        used.append(f"[{lang}] {query}")
        pool.add(backend.search_songs(query, lang), lang, isrc_query=is_isrc)
        if is_isrc:
            continue  # ISRC results alone never decide; always run a text query too
        decision = decide(track, pool.candidates(), premium)
        if decision.status == Status.AUTO:
            break
    album_tracks = getattr(backend, "album_tracks", None)
    if decision.status != Status.AUTO and track.album and album_tracks:
        first = track.artists[0] if track.artists else ""
        used.append(f"[album] {track.album} {first}")
        pool.add(album_tracks(track.album, first, track.title), "ja")
        decision = decide(track, pool.candidates(), premium)
    return decision, pool.candidates(), used


def manual_search(backend: SearchBackend, track: SpotifyTrack, query: str) -> list[Candidate]:
    """User-initiated re-search from the review screen."""
    pool = CandidatePool()
    pool.add(backend.search_songs(query, "ja"), "ja")
    pool.add(backend.search_songs(query, "en"), "en")
    return pool.candidates()


# --- authenticated part -------------------------------------------------------


# Headers describing the browser's own request body or connection; replaying them breaks
# ytmusicapi's requests (e.g. "content-encoding: gzip" on an uncompressed body gives HTTP 400).
_DROP_HEADERS = {"content-encoding", "content-length", "host", "connection",
                 "decoded"}  # Chrome's decoded view of x-client-data, not a header
_HEADER_LINE = re.compile(r"^([A-Za-z0-9-]+):\s*(.*)$")
_HEADER_NAME = re.compile(r"^:?[A-Za-z0-9-]+:?$")


def _header_pairs(headers_raw: str) -> list[tuple[str, str]]:
    lines = [line.strip() for line in headers_raw.splitlines() if line.strip()]
    if any(line.lower().startswith("cookie: ") for line in lines) or all(map(_HEADER_LINE.match, lines)):
        # "name: value" per line (Chrome's raw view, Firefox, Safari)
        return [(m.group(1), m.group(2)) for m in map(_HEADER_LINE.match, lines) if m]
    # Newer Chrome "Copy request headers": the name and the value on separate lines
    pairs, i = [], 0
    while i + 1 < len(lines):
        if _HEADER_NAME.match(lines[i]):
            pairs.append((lines[i].rstrip(":"), lines[i + 1]))
            i += 2
        else:
            i += 1
    return pairs


def clean_headers(headers_raw: str, authuser: str | None = None) -> str:
    """Normalize a browser's copied request headers to "name: value" lines.

    Drops the request line ("POST /youtubei/... HTTP/1.1"), HTTP/2 pseudo-headers
    (":authority" etc.) and headers that only apply to the browser's original request.
    A bare cookie value (copied with "Copy value") is accepted as the cookie header;
    ``authuser`` sets x-goog-authuser, which selects the account when several are signed in.
    """
    headers = {name.lower(): value for name, value in _header_pairs(headers_raw)
               if not name.startswith(":") and name.lower() not in _DROP_HEADERS}
    raw = headers_raw.strip()
    if "cookie" not in headers and "SAPISID=" in raw and "\n" not in raw:
        headers = {"cookie": raw.removeprefix("cookie:").strip()}
    if authuser is not None:
        headers["x-goog-authuser"] = authuser
    if "cookie" in headers and "authorization" not in headers:
        # ytmusicapi recognizes browser auth by this header and recomputes it from the
        # SAPISID cookie for every request, so a placeholder is enough.
        headers["authorization"] = "SAPISIDHASH placeholder"
    return "\n".join(f"{name}: {value}" for name, value in headers.items())


def save_auth(headers_raw: str, path: Path = DEFAULT_AUTH_PATH, authuser: str | None = None) -> Path:
    """Convert raw request headers to ytmusicapi's browser.json, readable only by the user."""
    from ytmusicapi import setup

    headers_raw = clean_headers(headers_raw, authuser)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Create the file with 0600 before any secret is written to it.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    setup(filepath=str(path), headers_raw=headers_raw)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def check_auth_file(path: Path) -> str | None:
    """Return a warning if the credentials file is missing or too permissive."""
    if not path.exists():
        return f"認証ファイルがありません: {path}（`ytm-import auth` で作成してください）"
    if os.name == "nt":
        # No POSIX permission bits on Windows; the file lives in the user's profile directory.
        return None
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return f"認証ファイルが他ユーザーから読める状態です（{oct(mode)}）。`chmod 600 {path}` を実行してください"
    return None


def merge_cookie_header(header: str, updates: dict[str, str]) -> tuple[str, list[str]]:
    """Apply Set-Cookie updates to a Cookie header. Returns the new header and the changed names."""
    jar = dict(part.split("=", 1) for part in (p.strip() for p in header.split(";")) if "=" in part)
    changed = sorted(name for name, value in updates.items() if jar.get(name) != value)
    jar.update(updates)
    return "; ".join(f"{name}={value}" for name, value in jar.items()), changed


class Liker:
    """The only component that writes to the YouTube Music account."""

    def __init__(self, auth_path: Path = DEFAULT_AUTH_PATH, interval: float = 1.0) -> None:
        problem = check_auth_file(auth_path)
        if problem:
            raise RuntimeError(problem)
        self._auth_path = auth_path
        self._yt = YTMusic(str(auth_path), language="ja", location="JP")
        self._limiter = RateLimiter(interval)
        # ytmusicapi sends the saved Cookie header verbatim, but Google rotates some cookies
        # (__Secure-*PSIDTS) via Set-Cookie and soon rejects the old values. Keep them current.
        self._yt._session.hooks["response"].append(self._absorb_cookies)

    def _absorb_cookies(self, response: Any, *args: Any, **kwargs: Any) -> None:
        updates = {c.name: c.value for c in response.cookies if c.value}
        if not updates:
            return
        headers = self._yt.base_headers
        cookie, changed = merge_cookie_header(headers["cookie"], updates)
        if not changed:
            return
        headers["cookie"] = cookie
        try:
            data = json.loads(self._auth_path.read_text())
            data["cookie"] = cookie
            fd = os.open(self._auth_path, os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except OSError as e:
            logger.warning("could not save refreshed cookies: %s", e)
        logger.info("cookies refreshed: %s", ", ".join(changed))  # names only, never values

    def account_name(self) -> str:
        info = retry(lambda: self._yt.get_account_info())
        return info.get("accountName") or info.get("channelHandle") or "?"

    def liked_video_ids(self) -> set[str]:
        playlist = retry(lambda: self._yt.get_liked_songs(limit=None))
        return {t["videoId"] for t in playlist.get("tracks", []) if t.get("videoId")}

    def followed_channel_ids(self) -> set[str]:
        subs = retry(lambda: self._yt.get_library_subscriptions(limit=None))
        return {a["browseId"] for a in subs if a.get("browseId")}

    def follow(self, channel_id: str) -> None:
        """Subscribe to an artist (also subscribes to its YouTube channel)."""

        def call() -> Any:
            self._limiter.wait()
            return self._yt.subscribe_artists([channel_id])

        retry(call, attempts=3)

    def like(self, video_id: str) -> None:
        def call() -> Any:
            self._limiter.wait()
            return self._yt.rate_song(video_id, "LIKE")

        retry(call, attempts=3)


