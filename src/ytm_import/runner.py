"""Long-running jobs (matching and liking), independent from the UI."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Protocol

from .log import describe_candidate, describe_track, logger
from .matcher import Decision, decide, music_video_pick
from .models import PREMIUM_SKIP_REASON, Status
from .store import Row, Store
from .ytm import SearchBackend, search_track


def match_one(store: Store, backend: SearchBackend, uri: str) -> Decision | None:
    row = store.get(uri)
    try:
        decision, candidates, queries = search_track(backend, row.track, store.premium_policy)
    except Exception as e:  # network trouble etc. – keep going, retry next run
        logger.exception("match error: %s", describe_track(row.track))
        store.save_match(uri, Status.ERROR, [], None, f"検索エラー: {e}")
        return None
    # Store candidates in ranked order so the review screen shows the best first.
    ordered = [s.candidate for s in decision.ranked] or candidates
    selected = decision.selected.candidate.video_id if decision.selected else None
    store.save_match(uri, decision.status, ordered[:20], selected, decision.reason, queries)
    top = decision.selected or (decision.ranked[0] if decision.ranked else None)
    logger.info(
        "match %-9s %s -> %s | %s | candidates=%d queries=%s",
        decision.status.value, describe_track(row.track),
        describe_candidate(top.candidate if top else None) + ("" if decision.selected else " (top, not selected)"),
        decision.reason, len(candidates), queries,
    )
    return decision


def redecide(store: Store) -> dict[str, int]:
    """Re-run the matcher on stored candidates (no network).

    Only tool-made decisions (auto / review / not found) are revisited; the
    user's own decisions are never touched.
    """
    changes: dict[str, int] = {}
    premium = store.premium_policy
    for row in store.rows((Status.AUTO, Status.REVIEW, Status.NOT_FOUND, Status.SKIPPED)):
        if row.liked or (row.status == Status.SKIPPED and row.reason != PREMIUM_SKIP_REASON):
            continue  # never touch the user's own skips
        d = decide(row.track, row.candidates, premium)
        selected = d.selected.candidate.video_id if d.selected else None
        if (d.status, selected) != (row.status, row.selected_video_id):
            key = f"{row.status.value} -> {d.status.value}"
            changes[key] = changes.get(key, 0) + 1
        store.save_match(row.track.uri, d.status, [s.candidate for s in d.ranked], selected, d.reason)
    logger.info("redecide: %s", changes or "no changes")
    return changes


def music_video_plan(store: Store) -> list[tuple[Row, str]]:
    """(row, video_id) for undecided tracks whose only good match is a music video."""
    plan = []
    for row in store.rows((Status.REVIEW,)):
        pick = music_video_pick(row.track, row.candidates, store.premium_policy)
        if pick:
            plan.append((row, pick.candidate.video_id))
    return plan


def accept_music_videos(store: Store, plan: list[tuple[Row, str]]) -> int:
    """Confirm the planned music videos (local decision only; nothing is sent to YouTube Music)."""
    for row, video_id in plan:
        store.set_status(row.track.uri, Status.CONFIRMED, video_id, "MV版を一括採用")
        chosen = next((c for c in row.candidates if c.video_id == video_id), None)
        logger.info("review mv-bulk   %s -> %s", describe_track(row.track), describe_candidate(chosen))
    return len(plan)


def match_all(
    store: Store,
    backend: SearchBackend,
    stop: threading.Event,
    on_result: Callable[[Row, Decision | None], None] = lambda r, d: None,
    workers: int = 3,
) -> int:
    """Match every pending track. Safe to stop at any time; finished tracks are saved."""
    uris = store.uris_to_match()
    logger.info("match start: %d tracks to search", len(uris))
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        it = iter(uris)
        running: dict[Future, str] = {}

        def submit_next() -> None:
            if stop.is_set():
                return
            uri = next(it, None)
            if uri is not None:
                running[pool.submit(match_one, store, backend, uri)] = uri

        for _ in range(workers):
            submit_next()
        while running:
            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            for fut in finished:
                uri = running.pop(fut)
                done += 1
                on_result(store.get(uri), fut.result())
                submit_next()
    logger.info("match %s: %d searched, %s", "stopped" if stop.is_set() else "finished", done,
                {k.value: v for k, v in store.stats().items()})
    return done


class LikeClient(Protocol):
    def account_name(self) -> str: ...
    def liked_video_ids(self) -> set[str]: ...
    def like(self, video_id: str) -> None: ...


@dataclass
class ApplyResult:
    applied: int = 0
    already: int = 0
    failed: int = 0
    aborted: str = ""


def apply_likes(
    store: Store,
    client: LikeClient,
    stop: threading.Event,
    on_progress: Callable[[int, int, Row, str], None] = lambda i, n, r, s: None,
    max_consecutive_failures: int = 3,
) -> ApplyResult:
    """Like every planned video that is not liked yet. Idempotent and resumable."""
    result = ApplyResult()
    try:
        existing = client.liked_video_ids()
    except Exception as e:
        # Only an optimization: likes applied by this tool are recorded locally,
        # and liking an already liked video is harmless.
        logger.warning("could not fetch existing likes (%s); relying on local records", e)
        existing = set()
    plan = store.like_plan()
    for row in plan:
        if row.selected_video_id in existing and not store.is_liked(row.selected_video_id):
            store.mark_liked(row.selected_video_id, source="existing")
            result.already += 1
            logger.info("like skip (already liked) %s", describe_candidate(row.selected))
    todo = [r for r in plan if not store.is_liked(r.selected_video_id or "")]
    logger.info("apply start: plan=%d already=%d to_like=%d", len(plan), result.already, len(todo))
    consecutive = 0
    for i, row in enumerate(todo, 1):
        if stop.is_set():
            result.aborted = "ユーザーが停止しました"
            break
        video_id = row.selected_video_id
        assert video_id
        try:
            client.like(video_id)
        except Exception as e:
            logger.error("like FAILED %s: %s", describe_candidate(row.selected), e)
            result.failed += 1
            consecutive += 1
            on_progress(i, len(todo), row, f"失敗: {e}")
            if consecutive >= max_consecutive_failures:
                result.aborted = f"{consecutive} 回連続で失敗したため中断しました（{e}）"
                break
            continue
        consecutive = 0
        store.mark_liked(video_id)
        result.applied += 1
        logger.info("like ok %s <- %s", describe_candidate(row.selected), describe_track(row.track))
        on_progress(i, len(todo), row, "ok")
    logger.info("apply end: %s", result)
    return result
