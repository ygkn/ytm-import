"""File log of every decision and every write, for later inspection.

Credentials are never logged.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .models import Candidate, SpotifyTrack

logger = logging.getLogger("ytm_import")


def setup(path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False  # keep log lines out of the terminal / TUI
    return path


def describe_track(t: SpotifyTrack) -> str:
    return f"{t.title} / {', '.join(t.artists)} [{t.album}] {t.duration_s:.0f}s ({t.uri})"


def describe_candidate(c: Candidate | None) -> str:
    if c is None:
        return "-"
    return f"{c.title} / {', '.join(c.artists)} [{c.album}] {c.duration_s}s ({c.video_id})"
