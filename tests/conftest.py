from __future__ import annotations

from typing import Any

import pytest

from ytm_import.models import Candidate, SpotifyTrack
from ytm_import.store import Store


def track(title: str, artists: list[str], album: str = "", seconds: float = 240, isrc: str = "",
          uri: str | None = None) -> SpotifyTrack:
    return SpotifyTrack(uri=uri or f"spotify:track:{abs(hash((title, tuple(artists)))) % 10**12}",
                        title=title, artists=artists, album=album, duration_ms=int(seconds * 1000), isrc=isrc)


def cand(video_id: str, title: str, artists: list[str], album: str = "", seconds: int | None = 240,
         video_type: str = "MUSIC_VIDEO_TYPE_ATV", isrc_hit: bool = False) -> Candidate:
    return Candidate(video_id=video_id, titles=[title], artists=artists, album=album, duration_s=seconds,
                     video_type=video_type, isrc_hit=isrc_hit)


def result(video_id: str, title: str, artists: list[str], album: str = "", seconds: int = 240,
           video_type: str = "MUSIC_VIDEO_TYPE_ATV") -> dict[str, Any]:
    """Shape of a ytmusicapi song search result."""
    return {"videoId": video_id, "title": title, "artists": [{"name": a, "id": None} for a in artists],
            "album": {"name": album, "id": None}, "duration_seconds": seconds, "videoType": video_type,
            "isAvailable": True}


class FakeSearch:
    def __init__(self, responses: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
                 default: list[dict[str, Any]] | None = None) -> None:
        self.responses = responses or {}
        self.default = default or []
        self.calls: list[tuple[str, str]] = []

    def search_songs(self, query: str, lang: str) -> list[dict[str, Any]]:
        self.calls.append((query, lang))
        return self.responses.get((query, lang), self.default)


class FakeLiker:
    def __init__(self, existing: set[str] | None = None, fail: set[str] | None = None) -> None:
        self.existing = existing or set()
        self.fail = fail or set()
        self.liked: list[str] = []

    def account_name(self) -> str:
        return "テストアカウント"

    def liked_video_ids(self) -> set[str]:
        return set(self.existing)

    def like(self, video_id: str) -> None:
        if video_id in self.fail:
            raise RuntimeError("boom")
        self.liked.append(video_id)


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "state.sqlite")
    yield s
    s.close()
