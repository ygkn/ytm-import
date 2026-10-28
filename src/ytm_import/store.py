"""Persistent state in SQLite so every step can be interrupted and resumed."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .models import DEFAULT_PREMIUM_POLICY, PREMIUM_SKIP_REASON, LIKE_STATUSES, OPEN_STATUSES, Candidate, SpotifyTrack, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    uri TEXT PRIMARY KEY,
    position INTEGER NOT NULL,          -- order in the CSV (0 = most recently liked on Spotify)
    track TEXT NOT NULL,                -- SpotifyTrack JSON
    status TEXT NOT NULL DEFAULT 'pending',
    candidates TEXT NOT NULL DEFAULT '[]',
    selected_video_id TEXT,
    reason TEXT NOT NULL DEFAULT '',
    queries TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS likes (
    video_id TEXT PRIMARY KEY,
    liked_at TEXT NOT NULL,
    source TEXT NOT NULL                -- 'applied' (by this tool) or 'existing' (already liked)
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Row:
    track: SpotifyTrack
    position: int
    status: Status
    candidates: list[Candidate]
    selected_video_id: str | None
    reason: str
    queries: list[str]
    liked: bool = False

    @property
    def selected(self) -> Candidate | None:
        return next((c for c in self.candidates if c.video_id == self.selected_video_id), None)


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- import ---------------------------------------------------------------

    def import_tracks(self, tracks: list[SpotifyTrack]) -> int:
        """Insert new tracks; existing ones keep their state. Returns number added."""
        with self._lock:
            before = self.count()
            self._db.execute("BEGIN")
            for i, t in enumerate(tracks):
                self._db.execute(
                    "INSERT INTO tracks (uri, position, track, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(uri) DO UPDATE SET position = excluded.position",
                    (t.uri, i, json.dumps(t.to_dict(), ensure_ascii=False), now()),
                )
            self._db.execute("COMMIT")
            return self.count() - before

    # --- reads ----------------------------------------------------------------

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]

    def _row(self, r: tuple) -> Row:
        uri, position, track, status, cands, sel, reason, queries, liked = r
        return Row(
            track=SpotifyTrack.from_dict(json.loads(track)),
            position=position,
            status=Status(status),
            candidates=[Candidate.from_dict(c) for c in json.loads(cands)],
            selected_video_id=sel,
            reason=reason,
            queries=json.loads(queries),
            liked=bool(liked),
        )

    _SELECT = (
        "SELECT t.uri, t.position, t.track, t.status, t.candidates, t.selected_video_id, t.reason, t.queries, "
        "l.video_id IS NOT NULL FROM tracks t LEFT JOIN likes l ON l.video_id = t.selected_video_id "
        "AND t.status IN ('auto', 'confirmed')"
    )

    def get(self, uri: str) -> Row:
        with self._lock:
            r = self._db.execute(self._SELECT + " WHERE t.uri = ?", (uri,)).fetchone()
        if r is None:
            raise KeyError(uri)
        return self._row(r)

    def rows(self, statuses: tuple[Status, ...] | None = None) -> list[Row]:
        sql = self._SELECT
        args: tuple = ()
        if statuses:
            sql += f" WHERE t.status IN ({','.join('?' * len(statuses))})"
            args = tuple(s.value for s in statuses)
        with self._lock:
            return [self._row(r) for r in self._db.execute(sql + " ORDER BY t.position", args)]

    def uris_to_match(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute(
                "SELECT uri FROM tracks WHERE status IN ('pending', 'error') ORDER BY position")]

    def review_queue(self) -> list[str]:
        """Undecided tracks: review first, then not-found, then deferred."""
        order = "CASE status WHEN 'review' THEN 0 WHEN 'not_found' THEN 1 ELSE 2 END"
        marks = ",".join("?" * len(OPEN_STATUSES))
        with self._lock:
            return [r[0] for r in self._db.execute(
                f"SELECT uri FROM tracks WHERE status IN ({marks}) ORDER BY {order}, position",
                tuple(s.value for s in OPEN_STATUSES))]

    def stats(self) -> Counter[Status]:
        with self._lock:
            rows = self._db.execute("SELECT status, COUNT(*) FROM tracks GROUP BY status").fetchall()
        return Counter({Status(s): n for s, n in rows})

    def like_plan(self) -> list[Row]:
        """Rows to like, oldest Spotify like first so the newest ends on top in YouTube Music.

        Several Spotify tracks can map to one video; it is listed once.
        """
        rows = sorted(self.rows(LIKE_STATUSES), key=lambda r: r.position, reverse=True)
        seen: set[str] = set()
        plan = []
        for r in rows:
            if r.selected_video_id and r.selected_video_id not in seen:
                seen.add(r.selected_video_id)
                plan.append(r)
        return plan

    def liked_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM likes").fetchone()[0]

    # --- writes ---------------------------------------------------------------

    def save_match(
        self,
        uri: str,
        status: Status,
        candidates: list[Candidate],
        selected_video_id: str | None,
        reason: str,
        queries: list[str] | None = None,
    ) -> None:
        with self._lock:
            sets = "status = ?, candidates = ?, selected_video_id = ?, reason = ?, updated_at = ?"
            args: list = [status.value, json.dumps([c.to_dict() for c in candidates], ensure_ascii=False),
                          selected_video_id, reason, now()]
            if queries is not None:
                sets += ", queries = ?"
                args.append(json.dumps(queries, ensure_ascii=False))
            self._db.execute(f"UPDATE tracks SET {sets} WHERE uri = ?", (*args, uri))

    def set_status(self, uri: str, status: Status, selected_video_id: str | None = None,
                   reason: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE tracks SET status = ?, selected_video_id = ?, reason = COALESCE(?, reason), "
                "updated_at = ? WHERE uri = ?",
                (status.value, selected_video_id, reason, now(), uri),
            )

    def get_setting(self, key: str, default: str) -> str:
        with self._lock:
            r = self._db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return r[0] if r else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    @property
    def premium_policy(self) -> str:
        return self.get_setting("premium", DEFAULT_PREMIUM_POLICY)

    def reset_for_retry(self) -> int:
        """Send tool-made "review" / "not found" results back to the search queue."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE tracks SET status = 'pending', updated_at = ? "
                "WHERE status IN ('review', 'not_found') OR (status = 'skipped' AND reason = ?)",
                (now(), PREMIUM_SKIP_REASON))
            return cur.rowcount

    def add_candidates(self, uri: str, new: list[Candidate]) -> None:
        """Merge re-search results into the stored candidate list."""
        row = self.get(uri)
        known = {c.video_id for c in row.candidates}
        merged = row.candidates + [c for c in new if c.video_id not in known]
        with self._lock:
            self._db.execute(
                "UPDATE tracks SET candidates = ?, updated_at = ? WHERE uri = ?",
                (json.dumps([c.to_dict() for c in merged], ensure_ascii=False), now(), uri),
            )

    def mark_liked(self, video_id: str, source: str = "applied") -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO likes (video_id, liked_at, source) VALUES (?, ?, ?)",
                (video_id, now(), source),
            )

    def is_liked(self, video_id: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM likes WHERE video_id = ?", (video_id,)).fetchone() is not None
