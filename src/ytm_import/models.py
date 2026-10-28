from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


@dataclass
class SpotifyTrack:
    uri: str
    title: str
    artists: list[str]
    album: str = ""
    duration_ms: int = 0
    isrc: str = ""
    added_at: str = ""

    @property
    def duration_s(self) -> float:
        return self.duration_ms / 1000

    @property
    def url(self) -> str:
        return "https://open.spotify.com/track/" + self.uri.rsplit(":", 1)[-1]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SpotifyTrack:
        return cls(**d)


@dataclass
class Candidate:
    """A YouTube Music song as returned by search (possibly merged across locales)."""

    video_id: str
    titles: list[str]  # first one is the preferred (Japanese locale) title
    artists: list[str]  # one name per credited artist (Japanese locale when available)
    album: str = ""
    duration_s: int | None = None
    video_type: str | None = None
    is_available: bool = True
    isrc_hit: bool = False  # this was the top result of an ISRC search
    premium_only: bool = False  # playable only with YouTube Music Premium
    premium_inferred: bool = False  # ...judged from context, YouTube only said "unplayable"
    # Same credits spelled in another locale, index-aligned with ``artists``
    # (e.g. "Rina Tennoji (CV: Chiemi Tanaka)"). Empty when unknown.
    alt_artists: list[str] = field(default_factory=list)

    def credits(self) -> list[list[str]]:
        """Spellings for each credited artist."""
        aligned = len(self.alt_artists) == len(self.artists)
        return [[a, self.alt_artists[i]] if aligned else [a] for i, a in enumerate(self.artists)]

    @property
    def title(self) -> str:
        return self.titles[0] if self.titles else ""

    @property
    def url(self) -> str:
        return f"https://music.youtube.com/watch?v={self.video_id}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Candidate:
        return cls(**d)


@dataclass
class ScoredCandidate:
    candidate: Candidate
    score: float  # 0..100, for ordering and display
    exact: bool  # passes every strict check
    notes: list[str] = field(default_factory=list)  # human readable reasons (Japanese)
    title_sim: float = 0.0
    artist_f1: float = 0.0
    duration_diff: float | None = None
    # Passes every check except being an official music video instead of an audio track.
    exact_but_mv: bool = False
    same_album: bool = False
    artist_recall: float = 0.0  # share of Spotify artists credited on the candidate
    premium_blocked: bool = False  # would be exact, but the Premium policy holds it back
    version_ok: bool = False


class Status(StrEnum):
    PENDING = "pending"  # not searched yet
    AUTO = "auto"  # confidently matched automatically
    REVIEW = "review"  # ambiguous, waiting for the user
    NOT_FOUND = "not_found"  # no plausible candidate
    CONFIRMED = "confirmed"  # user picked a candidate
    SKIPPED = "skipped"  # user decided not to migrate
    DEFERRED = "deferred"  # user postponed the decision
    ERROR = "error"  # search failed; retried on the next run


# How to treat tracks playable only with YouTube Music Premium.
PREMIUM_POLICIES = {
    "include": "追加する",
    "review": "確認する（要確認に回す）",
    "exclude": "追加しない（スキップ）",
}
DEFAULT_PREMIUM_POLICY = "include"
PREMIUM_SKIP_REASON = "Premium限定のため除外（設定）"

# Statuses whose selected video will be liked.
LIKE_STATUSES = (Status.AUTO, Status.CONFIRMED)
# Statuses that need a human decision.
OPEN_STATUSES = (Status.REVIEW, Status.NOT_FOUND, Status.DEFERRED)

STATUS_LABELS = {
    Status.PENDING: "未照合",
    Status.AUTO: "自動一致",
    Status.REVIEW: "要確認",
    Status.NOT_FOUND: "見つからない",
    Status.CONFIRMED: "手動確定",
    Status.SKIPPED: "スキップ",
    Status.DEFERRED: "後で判断",
    Status.ERROR: "エラー",
}

STATUS_STYLES = {
    Status.PENDING: "dim",
    Status.AUTO: "green",
    Status.REVIEW: "yellow",
    Status.NOT_FOUND: "red",
    Status.CONFIRMED: "cyan",
    Status.SKIPPED: "bright_black",
    Status.DEFERRED: "magenta",
    Status.ERROR: "bold red",
}
