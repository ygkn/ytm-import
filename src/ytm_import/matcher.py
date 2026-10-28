"""Score YouTube Music candidates against a Spotify track and decide.

The rules are deliberately conservative: a track is only matched
automatically when a candidate passes *every* strict check (title, version
markers such as live / remix / TV size / off vocal, artist credits including
character (CV) credits, duration and audio-track type). Anything else goes to
manual review with the reasons spelled out.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import PREMIUM_SKIP_REASON, Candidate, ScoredCandidate, SpotifyTrack, Status
from .textnorm import (
    TitleInfo,
    analyze_title,
    artist_overlap,
    artists_match,
    label_tags,
    labels_match,
    merge_labels,
    similarity,
)

TITLE_EXACT = 0.92
ARTIST_F1_EXACT = 0.5
DURATION_EXACT_S = 3.0
DURATION_EXACT_ISRC_S = 6.0
# Exact candidates within this many seconds of each other are treated as the
# same recording released on different albums (single / album / best-of).
SAME_RECORDING_S = 2.0

TAG_LABELS = {
    "live": "ライブ",
    "session": "一発録り/セッション",
    "remix": "リミックス",
    "instrumental": "インスト/カラオケ",
    "tv_size": "TVサイズ/ショート",
    "acoustic": "アコースティック",
    "piano_box": "ピアノ/オルゴール",
    "cover": "カバー",
    "ringtone": "着信音",
    "a_cappella": "アカペラ",
    "demo": "デモ",
    "edit": "エディット",
    "speed": "スピード変更",
    "orchestra": "オーケストラ",
    "chorus": "合唱",
    "solo": "ソロ",
    "language": "言語違い",
    "version": "ver.",
}


@dataclass
class Decision:
    status: Status
    selected: ScoredCandidate | None
    ranked: list[ScoredCandidate] = field(default_factory=list)
    reason: str = ""


def title_options(cand: Candidate) -> list[TitleInfo]:
    """Every known spelling of the candidate's title.

    English locale titles look like "原題 - Romanized [Remix]", so each half is
    tried as well. Note that the Japanese locale sometimes *drops* version
    info ("ピュア (feat. 橋本絵莉子)" vs "ピュア - Pure [Kabanagu Remix β]").
    """
    options: list[str] = []
    for t in cand.titles:
        options.append(t)
        if " - " in t:
            options.extend(p for p in t.split(" - ") if p.strip())
    return [analyze_title(o) for o in dict.fromkeys(options)]


def album_labels(album: str) -> list[str]:
    """Version labels in an album title, e.g. "百色リメイン (双葉&香子ver.)" -> ["双葉&香子ver."].

    Character-song singles often carry the singer only in the album name.
    """
    return analyze_title(album).labels if album else []


def version_of(info: TitleInfo, album: str) -> tuple[set[str], list[str]]:
    # Album labels only stand in when the title itself carries no version info.
    # A soundtrack album's "Japanese Version" does not make every song on it Japanese.
    extra = [] if info.labels or info.tags else [x for x in album_labels(album) if "language" not in label_tags(x)]
    tags = set(info.tags)
    for label in extra:
        tags |= label_tags(label)
    return tags, merge_labels(info.labels, extra)


def album_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return similarity(a, b) >= 0.85 and labels_match(album_labels(a), album_labels(b))


def score_candidate(track: SpotifyTrack, cand: Candidate, premium: str = "include") -> ScoredCandidate:
    t_info = analyze_title(track.title)
    options = title_options(cand) or [analyze_title("")]
    title_sim = max(max(similarity(t_info.base, o.base), similarity(t_info.full, o.full)) for o in options)
    notes: list[str] = []
    exact = True

    if title_sim < TITLE_EXACT:
        exact = False
        notes.append(f"曲名の一致度 {title_sim:.0%}")

    # --- version markers (live, remix, TV size, singer ver., ...) ---
    # One spelling must carry exactly the same version info, and no spelling
    # may carry a version marker the Spotify track does not have.
    t_tags, t_labels = version_of(t_info, track.album)
    versions = [version_of(o, cand.album) for o in options]
    all_tags = set().union(*(tags for tags, _ in versions))
    extra = all_tags - t_tags
    same = [(tags, labels) for tags, labels in versions if tags == t_tags and labels_match(t_labels, labels)]
    version_ok = bool(same) and not extra
    if extra:
        notes.append("候補のみ: " + "・".join(TAG_LABELS[t] for t in sorted(extra)))
    if not version_ok:
        missing = t_tags - all_tags
        if missing:
            notes.append("元曲のみ: " + "・".join(TAG_LABELS[t] for t in sorted(missing)))
        elif not extra:
            c_labels = merge_labels(*(labels for _, labels in versions))
            notes.append(f"版の表記が違う（{' / '.join(t_labels) or 'なし'} ↔ {' / '.join(c_labels) or 'なし'}）")
        exact = False

    # --- artists (feat. credits in the title count as artists) ---
    spotify_artists = list(track.artists) + [a for a in t_info.feat if a not in track.artists]
    credits = cand.credits()
    # feat. credits from the preferred (Japanese) title; English ones are romanized duplicates.
    feats = analyze_title(cand.title).feat or [f for o in options for f in o.feat]
    credits += [[f] for f in dict.fromkeys(feats) if not any(artists_match(f, s) for c in credits for s in c)]
    overlap = artist_overlap(spotify_artists, credits)
    artist_f1 = overlap.f1
    # A guest named only in the candidate's title ("（with かばん）") marks another
    # version – but only when every Spotify artist is already accounted for;
    # otherwise the guest is probably one of them spelled in another script
    # ("HATSUNE MIKU" vs "初音ミク").
    guests = [f for f in feats if not any(artists_match(f, a) for a in spotify_artists)]
    if guests and overlap.recall == 1:
        exact = False
        notes.append("候補のみの参加者: " + "、".join(guests))
    artist_problem = ""
    if overlap.recall == 0:
        artist_problem = "アーティスト不一致"
    elif artist_f1 < ARTIST_F1_EXACT:
        artist_problem = f"歌唱者の構成が違う（一致 {overlap.recall:.0%} / 候補側 {overlap.precision:.0%}）"
    elif not overlap.primary:
        notes.append("メインアーティストが候補に無い")

    # --- duration ---
    diff: float | None = None
    if cand.duration_s is not None and track.duration_ms:
        diff = cand.duration_s - track.duration_s
        limit = DURATION_EXACT_ISRC_S if cand.isrc_hit else DURATION_EXACT_S
        # YouTube Music reports whole seconds, so allow for rounding.
        if abs(diff) > limit + 0.5:
            exact = False
            notes.append(f"長さが {diff:+.0f} 秒違う")
    elif track.duration_ms:
        exact = False
        notes.append("長さ不明")

    # --- type / availability ---
    is_mv = cand.video_type == "MUSIC_VIDEO_TYPE_OMV"
    exact_but_mv = (exact and not artist_problem and is_mv and cand.is_available
                    and (diff is None or abs(diff) <= DURATION_EXACT_S + 0.5))
    if cand.video_type and cand.video_type != "MUSIC_VIDEO_TYPE_ATV":
        exact = False
        notes.append({"MUSIC_VIDEO_TYPE_OMV": "MV（音源ではない）", "MUSIC_VIDEO_TYPE_UGC": "ユーザー投稿動画"}.get(
            cand.video_type, cand.video_type))
    if not cand.is_available:
        exact = False
        notes.append("再生不可（YouTube Music 上で再生できない状態）")

    same_album = album_match(track.album, cand.album)
    if artist_problem:
        # Credits are often abbreviated or spelled differently (aliases, readings,
        # truncated cast lists). Overlook that only with strong evidence that it is
        # the same recording.
        strong = (cand.isrc_hit and same_album and diff is not None and abs(diff) <= 1.5
                  and title_sim >= TITLE_EXACT and version_ok and cand.video_type in (None, "MUSIC_VIDEO_TYPE_ATV"))
        if strong:
            notes.append(f"クレジット表記の違いを許容: {artist_problem}（ISRC・アルバム・長さが一致）")
        else:
            exact = False
            notes.append(artist_problem)
    if same_album:
        notes.append("アルバム一致")
    if cand.isrc_hit:
        notes.append("ISRC検索1位")
    premium_blocked = False
    if cand.premium_only:
        label = "Premium限定（推定）" if cand.premium_inferred else "Premium限定"
        if premium == "include":
            notes.append(label)
        else:
            premium_blocked = exact
            exact = False
            notes.append(label + "（設定により" + ("要確認）" if premium == "review" else "除外）"))

    dur_score = 0.0 if diff is None else max(0.0, 1 - max(0.0, abs(diff) - 1) / 15)
    score = (
        40 * title_sim
        + 25 * artist_f1
        + 20 * dur_score
        + (5 if same_album else 0)
        + (10 if version_ok else -25)
        + (5 if cand.isrc_hit else 0)
        - (10 if cand.video_type not in (None, "MUSIC_VIDEO_TYPE_ATV") else 0)
    )
    return ScoredCandidate(
        candidate=cand,
        score=max(0.0, min(100.0, score)),
        exact=exact,
        notes=notes,
        title_sim=title_sim,
        artist_f1=artist_f1,
        duration_diff=diff,
        exact_but_mv=exact_but_mv,
        same_album=same_album,
        artist_recall=overlap.recall,
        premium_blocked=premium_blocked,
        version_ok=version_ok,
    )


def music_video_pick(track: SpotifyTrack, candidates: list[Candidate], premium: str = "include",
                     ) -> ScoredCandidate | None:
    """The official music video to use when no audio track exists, or None.

    Requires title, version, artists, album and length (within 3 s) to match.
    Skipped when several such videos disagree on length.
    """
    ranked = rank(track, candidates, premium)
    if premium == "exclude" and any(s.premium_blocked for s in ranked):
        return None  # the policy says to leave this track out
    ok = [s for s in ranked if s.exact_but_mv and s.same_album]
    if not ok:
        return None
    ok.sort(key=lambda s: abs(s.duration_diff or 0))
    durations = [s.candidate.duration_s for s in ok if s.candidate.duration_s is not None]
    if durations and max(durations) - min(durations) > SAME_RECORDING_S:
        return None
    return ok[0]


def _preference(sc: ScoredCandidate, track: SpotifyTrack) -> tuple:
    c = sc.candidate
    # YouTube Music's ISRC search is not exact (it can surface another version
    # of the song), so the album is the stronger signal.
    return (
        album_match(track.album, c.album),
        sc.artist_recall,  # e.g. "ray feat. 初音ミク" over plain "ray" when Spotify credits Miku
        c.isrc_hit,
        -abs(sc.duration_diff or 0),
        sc.score,
    )


def rank(track: SpotifyTrack, candidates: list[Candidate], premium: str = "include") -> list[ScoredCandidate]:
    scored = [score_candidate(track, c, premium) for c in candidates]
    scored.sort(key=lambda s: (s.exact, s.score), reverse=True)
    return scored


def decide(track: SpotifyTrack, candidates: list[Candidate], premium: str = "include") -> Decision:
    ranked = rank(track, candidates, premium)
    if not ranked:
        return Decision(Status.NOT_FOUND, None, ranked, "検索結果なし")

    exacts = [s for s in ranked if s.exact]
    if not exacts and premium == "exclude" and any(s.premium_blocked for s in ranked):
        return Decision(Status.SKIPPED, None, ranked, PREMIUM_SKIP_REASON)
    if not exacts and premium == "review" and any(s.premium_blocked for s in ranked):
        return Decision(Status.REVIEW, None, ranked, "Premium限定の候補のみ一致（設定により要確認）")
    if not exacts:
        best = ranked[0]
        # Only worth a human look if something resembles the title.
        plausible = any(s.title_sim >= 0.6 and s.artist_f1 > 0 for s in ranked) or any(
            s.title_sim >= 0.85 for s in ranked
        )
        status = Status.REVIEW if plausible else Status.NOT_FOUND
        reason = "確実な一致なし: " + ("、".join(best.notes) or "—")
        return Decision(status, None, ranked, reason)

    exacts.sort(key=lambda s: _preference(s, track), reverse=True)
    best = exacts[0]
    others = exacts[1:]
    strong = best.candidate.isrc_hit or album_match(track.album, best.candidate.album)
    same_recording = all(
        best.candidate.duration_s is not None
        and o.candidate.duration_s is not None
        and abs(best.candidate.duration_s - o.candidate.duration_s) <= SAME_RECORDING_S
        for o in others
    )
    # Same song credited to more of the Spotify artists, but failing some check
    # (e.g. credits written differently): could be another singer line-up.
    rivals = [
        s for s in ranked
        if s is not best and s.version_ok and s.title_sim >= TITLE_EXACT
        and s.artist_recall > best.artist_recall
        and s.duration_diff is not None and abs(s.duration_diff) <= DURATION_EXACT_S + 0.5
    ]
    if rivals:
        return Decision(Status.REVIEW, None, ranked,
                        f"歌唱者の構成が違う可能性のある候補あり（{rivals[0].candidate.title} / "
                        f"{', '.join(rivals[0].candidate.artists)}）")
    if strong or same_recording:
        reason = "完全一致" if not others else f"完全一致（同一音源の候補 {len(exacts)} 件から選択）"
        # Keep the chosen one on top for display.
        ranked.remove(best)
        ranked.insert(0, best)
        return Decision(Status.AUTO, best, ranked, reason)

    return Decision(
        Status.REVIEW,
        None,
        ranked,
        f"条件を満たす候補が {len(exacts)} 件あり、長さが異なるため判断できない",
    )
