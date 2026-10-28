"""Read Exportify CSV exports (English or Japanese headers)."""

from __future__ import annotations

import csv
from pathlib import Path

from .models import SpotifyTrack

# Exportify column -> our field, for each UI language it has been seen in.
COLUMNS = {
    "uri": ("Track URI", "トラックURI"),
    "title": ("Track Name", "トラック名"),
    "artist_uris": ("Artist URI(s)", "アーティストURI"),
    "artists": ("Artist Name(s)", "アーティスト名"),
    "album": ("Album Name", "アルバム名"),
    "duration_ms": ("Track Duration (ms)", "Duration (ms)", "トラックの長さ（ミリ秒）"),
    "isrc": ("ISRC",),
    "added_at": ("Added At", "追加日時"),
}
REQUIRED = ("uri", "title", "artists")


def _resolve(header: list[str]) -> dict[str, str]:
    found = {}
    for key, names in COLUMNS.items():
        for name in names:
            if name in header:
                found[key] = name
                break
    missing = [k for k in REQUIRED if k not in found]
    if missing:
        raise ValueError(f"Exportify の CSV ではないようです（列が見つかりません: {', '.join(missing)}）")
    return found


def split_artists(names: str, uris: str = "") -> list[str]:
    """Split Exportify's joined artist names.

    Names are joined with "," (older exports) or ", " – but artist names can
    contain commas themselves, so when URIs are present we use their count to
    decide how many names there are.
    """
    names = names.strip()
    if not names:
        return []
    count = len([u for u in uris.split(",") if u.strip()]) if uris else 0
    parts = [p.strip() for p in names.split(",")]
    if count and len(parts) != count:
        # Prefer the ", " separator, then merge the overflow into the last name.
        parts = [p.strip() for p in names.split(", ")]
        if len(parts) > count:
            parts = parts[: count - 1] + [", ".join(parts[count - 1 :])]
    return [p for p in parts if p]


def load_tracks(path: str | Path) -> list[SpotifyTrack]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        cols = _resolve(reader.fieldnames or [])
        tracks: list[SpotifyTrack] = []
        seen: set[str] = set()
        for row in reader:
            get = lambda key: (row.get(cols[key]) or "").strip() if key in cols else ""  # noqa: E731
            uri = get("uri")
            if not uri or uri in seen or not uri.startswith("spotify:track:"):
                # Skip duplicates and local files (spotify:local:...) which can't be matched.
                continue
            seen.add(uri)
            try:
                duration = int(float(get("duration_ms") or 0))
            except ValueError:
                duration = 0
            tracks.append(
                SpotifyTrack(
                    uri=uri,
                    title=get("title"),
                    artists=split_artists(get("artists"), get("artist_uris")),
                    album=get("album"),
                    duration_ms=duration,
                    isrc=get("isrc").upper(),
                    added_at=get("added_at"),
                )
            )
    return tracks
