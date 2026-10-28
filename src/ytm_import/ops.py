"""Non-interactive operations behind the CLI (used by the Claude Code skill)."""

from __future__ import annotations

from typing import Any

from .log import describe_candidate, describe_track, logger
from .matcher import album_match, rank
from .models import OPEN_STATUSES, PREMIUM_SKIP_REASON, ScoredCandidate, Status
from .runner import music_video_plan
from .store import Row, Store
from .textnorm import analyze_title, normalize

TYPE_NAMES = {"MUSIC_VIDEO_TYPE_ATV": "audio", "MUSIC_VIDEO_TYPE_OMV": "MV", "MUSIC_VIDEO_TYPE_UGC": "UGC"}


def candidate_dict(n: int, sc: ScoredCandidate, selected: str | None = None) -> dict[str, Any]:
    c = sc.candidate
    d: dict[str, Any] = {
        "n": n,
        "video_id": c.video_id,
        "title": c.title,
        "artists": c.artists,
        "album": c.album,
        "duration": c.duration_s,
        "diff": None if sc.duration_diff is None else round(sc.duration_diff, 1),
        "type": TYPE_NAMES.get(c.video_type or "", c.video_type),
        "exact": sc.exact,
        "score": round(sc.score),
        "notes": sc.notes,
    }
    if len(c.titles) > 1:
        d["other_titles"] = c.titles[1:]
    if c.alt_artists and c.alt_artists != c.artists:
        d["other_artists"] = c.alt_artists
    if not c.is_available:
        d["available"] = False
    if c.video_id == selected:
        d["selected"] = True
    return d


def track_dict(row: Row) -> dict[str, Any]:
    t = row.track
    d: dict[str, Any] = {
        "uri": t.uri,
        "status": row.status.value,
        "title": t.title,
        "artists": t.artists,
        "album": t.album,
        "duration": round(t.duration_s),
        "reason": row.reason,
    }
    if row.selected:
        d["selected"] = describe_candidate(row.selected)
    if row.liked:
        d["liked"] = True
    return d


def status_summary(store: Store) -> dict[str, Any]:
    stats = store.stats()
    plan = store.like_plan()
    return {
        "total": store.count(),
        "by_status": {s.value: stats.get(s, 0) for s in Status if stats.get(s, 0)},
        "undecided": sum(stats.get(s, 0) for s in OPEN_STATUSES),
        "to_like": sum(1 for r in plan if not r.liked),
        "liked": store.liked_count(),
        "music_video_bulk_candidates": len(music_video_plan(store)),
        "premium_policy": store.premium_policy,
        **premium_counts(store),
    }


def review_items(store: Store, statuses: tuple[Status, ...], limit: int, offset: int = 0,
                 max_candidates: int = 6) -> list[dict[str, Any]]:
    if set(statuses) <= set(OPEN_STATUSES):
        wanted = {r.track.uri for r in store.rows(statuses)}
        uris = [u for u in store.review_queue() if u in wanted]  # same order as the TUI review queue
    else:
        uris = [r.track.uri for r in store.rows(statuses)]
    out = []
    for uri in uris[offset:offset + limit]:
        row = store.get(uri)
        d = track_dict(row)
        d["candidates"] = [candidate_dict(i, sc, row.selected_video_id)
                           for i, sc in enumerate(rank(row.track, row.candidates, store.premium_policy)[:max_candidates], 1)]
        out.append(d)
    return out


def _selected_scored(row: Row) -> ScoredCandidate | None:
    return next((s for s in rank(row.track, row.candidates) if s.candidate.video_id == row.selected_video_id), None)


def premium_counts(store: Store) -> dict[str, int]:
    plan = [r for r in store.like_plan() if not r.liked]
    return {
        "premium_only_in_plan": sum(1 for r in plan if r.selected and r.selected.premium_only),
        "skipped_by_premium_policy": sum(1 for r in store.rows((Status.SKIPPED,)) if r.reason == PREMIUM_SKIP_REASON),
    }


def audit(store: Store) -> dict[str, list[dict[str, Any]]]:
    """Automatic matches worth a second look, and undecided tracks that nearly matched."""
    risky: dict[str, list[dict[str, Any]]] = {
        "auto_title_differs": [],
        "auto_partial_artists": [],
        "auto_credit_tolerated": [],
        "auto_other_album_and_length_gap": [],
    }
    for row in store.rows((Status.AUTO,)):
        sc = _selected_scored(row)
        if sc is None:
            continue
        c = sc.candidate
        item = {"uri": row.track.uri, "spotify": describe_track(row.track), "chosen": describe_candidate(c),
                "notes": sc.notes}
        if normalize(analyze_title(row.track.title).full) != normalize(analyze_title(c.title).full):
            risky["auto_title_differs"].append(item)
        if any(n.startswith("クレジット表記の違いを許容") for n in sc.notes):
            risky["auto_credit_tolerated"].append(item)
        elif sc.artist_f1 < 1:
            risky["auto_partial_artists"].append(item)
        if (not album_match(row.track.album, c.album) and not c.isrc_hit
                and abs(sc.duration_diff or 0) >= 2):
            risky["auto_other_album_and_length_gap"].append(item)

    near: dict[str, list[dict[str, Any]]] = {}
    for row in store.rows((Status.REVIEW, Status.NOT_FOUND)):
        ranked = rank(row.track, row.candidates, store.premium_policy)
        if not ranked or ranked[0].exact_but_mv:
            continue
        top = ranked[0]
        fails = [n for n in top.notes if n not in ("アルバム一致", "ISRC検索1位") and not n.startswith("メインアーティスト")]
        if len(fails) == 1:
            key = "near_miss:" + fails[0].split("（")[0].split(" ")[0].rstrip(":")
            near.setdefault(key, []).append({"uri": row.track.uri, "spotify": describe_track(row.track),
                                             "top": describe_candidate(top.candidate), "notes": top.notes})
    return {**risky, **near}


ACTIONS = ("pick", "skip", "defer", "reset")


def apply_decisions(store: Store, decisions: list[dict[str, Any]]) -> list[str]:
    """Record decisions like {"uri": ..., "action": "pick", "video_id": ... | "n": 1}.

    All entries are validated first; nothing is written if any entry is invalid.
    Returns warnings (e.g. a changed pick for an already liked track).
    """
    errors: list[str] = []
    planned: list[tuple[Row, Status, str | None, str]] = []
    for i, d in enumerate(decisions):
        uri, action = d.get("uri"), d.get("action")
        try:
            row = store.get(str(uri))
        except KeyError:
            errors.append(f"#{i}: 不明な uri {uri!r}")
            continue
        if action not in ACTIONS:
            errors.append(f"#{i}: action は {', '.join(ACTIONS)} のいずれか（{action!r}）")
            continue
        if action == "pick":
            ranked = rank(row.track, row.candidates)
            video_id = d.get("video_id")
            if video_id is None and isinstance(d.get("n"), int) and 1 <= d["n"] <= len(ranked):
                video_id = ranked[d["n"] - 1].candidate.video_id
            if video_id not in {c.video_id for c in row.candidates}:
                errors.append(f"#{i}: {row.track.title}: 候補に無い video_id / n（{d}）")
                continue
            planned.append((row, Status.CONFIRMED, video_id, d.get("reason") or "手動で選択"))
        elif action == "skip":
            planned.append((row, Status.SKIPPED, None, d.get("reason") or "スキップ（移行しない）"))
        elif action == "defer":
            planned.append((row, Status.DEFERRED, None, d.get("reason") or "後で判断"))
        else:
            planned.append((row, Status.REVIEW, None, "未決定に戻した"))
    if errors:
        raise ValueError("\n".join(errors))

    warnings = []
    for row, status, video_id, reason in planned:
        if row.liked and video_id != row.selected_video_id:
            warnings.append(f"{row.track.title}: 元の候補は既に高評価済み（必要なら YouTube Music 側で解除）")
        store.set_status(row.track.uri, status, video_id, reason)
        chosen = next((c for c in row.candidates if c.video_id == video_id), None)
        logger.info("review %-9s %s -> %s (cli)", status.value, describe_track(row.track), describe_candidate(chosen))
    return warnings


def like_plan_summary(store: Store, sample: int = 5) -> dict[str, Any]:
    plan = store.like_plan()
    todo = [r for r in plan if not r.liked]
    stats = store.stats()
    return {
        "to_like": len(todo),
        "already_liked": len(plan) - len(todo),
        "from_auto": sum(1 for r in todo if r.status == Status.AUTO),
        "from_confirmed": sum(1 for r in todo if r.status == Status.CONFIRMED),
        "not_included_undecided": sum(stats.get(s, 0) for s in OPEN_STATUSES),
        "not_included_skipped": stats.get(Status.SKIPPED, 0),
        "premium_policy": store.premium_policy,
        **premium_counts(store),
        "first": [f"{describe_track(r.track)} -> {describe_candidate(r.selected)}" for r in todo[:sample]],
        "last": [f"{describe_track(r.track)} -> {describe_candidate(r.selected)}" for r in todo[-sample:]],
    }

