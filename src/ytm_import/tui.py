"""Textual UI."""

from __future__ import annotations

import threading
from collections.abc import Callable

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets.option_list import Option
from textual.widgets import DataTable, Footer, Header, Input, Label, OptionList, ProgressBar, RichLog, Static

from .log import describe_candidate, describe_track, logger
from .matcher import rank
from .models import (
    LIKE_STATUSES,
    OPEN_STATUSES,
    STATUS_LABELS,
    PREMIUM_POLICIES,
    STATUS_STYLES,
    Candidate,
    ScoredCandidate,
    Status,
)
from .runner import (
    ApplyResult,
    LikeClient,
    accept_music_videos,
    apply_likes,
    match_all,
    music_video_plan,
    redecide,
)
from .store import Row, Store
from .ytm import SearchBackend, manual_search


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = round(seconds)
    return f"{s // 60}:{s % 60:02d}"


def title_cell(c: Candidate) -> Text:
    """Preferred title plus the other-locale spelling (which may carry version info)."""
    text = Text(c.title, style="bold")
    for other in c.titles[1:2]:
        text.append(f"  {other}", style="dim")
    return text


def status_text(status: Status, liked: bool = False) -> Text:
    label = STATUS_LABELS[status] + (" ♥" if liked else "")
    return Text(label, style=STATUS_STYLES[status])


# --- modals -----------------------------------------------------------------


class SearchModal(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "キャンセル")]

    def __init__(self, initial: str) -> None:
        super().__init__()
        self.initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("YouTube Music を再検索（Enterで検索 / Escでキャンセル）")
            yield Input(value=self.initial, id="query")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    """Requires typing a word, so a stray Enter can never trigger a write."""

    BINDINGS = [Binding("escape", "cancel", "キャンセル")]

    def __init__(self, message: str, word: str = "yes") -> None:
        super().__init__()
        self.message = message
        self.word = word

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.message)
            yield Label(f"実行するには [b]{self.word}[/b] と入力して Enter（Escでキャンセル）")
            yield Input(id="confirm")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.value.strip().lower() == self.word:
            self.dismiss(True)
        else:
            self.notify(f"「{self.word}」と入力してください", severity="warning")

    def action_cancel(self) -> None:
        self.dismiss(False)


class PremiumModal(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "キャンセル")]

    def __init__(self, current: str) -> None:
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(
                "[b]YouTube Music Premium 会員だけが再生できる曲の扱い[/b]\n"
                "変更すると、保存済みの検索結果で判定をやり直します（通信なし・手動の判断は保持）。"
            )
            options = OptionList(*[Option(("● " if k == self.current else "  ") + label, id=k)
                                   for k, label in PREMIUM_POLICIES.items()])
            yield options

    def on_mount(self) -> None:
        ol = self.query_one(OptionList)
        ol.highlighted = list(PREMIUM_POLICIES).index(self.current)
        ol.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


# --- home -------------------------------------------------------------------


HELP = """\
[b]手順[/b]
  [b]1.[/b] [reverse] m [/reverse] 照合 … 全曲を検索し、確実な曲だけ自動で決定（途中で止めても再開できます）
  [b]2.[/b] [reverse] r [/reverse] 確認 … 曖昧な曲を候補から選ぶ / スキップ / 後回し
     [reverse] l [/reverse] 一覧 … 自動一致を含む全曲の結果を確認・修正
     [reverse] v [/reverse] MV版 … 音源が無く MV だけ一致する曲をまとめて採用
     [reverse] o [/reverse] 設定 … Premium 限定の曲の扱い（追加する / 確認する / 追加しない）
  [b]3.[/b] [reverse] a [/reverse] 適用 … 内容を確認してから YouTube Music に高評価を付ける

照合中はアカウントに一切アクセスしません。書き込みは手順 3 で明示的に確認したときだけ行います。
"""


class HomeScreen(Screen):
    BINDINGS = [
        Binding("m", "match", "照合を開始/再開"),
        Binding("s", "stop", "照合を停止"),
        Binding("r", "review", "要確認を処理"),
        Binding("l", "list", "一覧"),
        Binding("v", "music_videos", "MV版を一括採用"),
        Binding("o", "settings", "設定"),
        Binding("a", "apply", "高評価を適用"),
        Binding("q", "app.quit", "終了"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._stop = threading.Event()
        self._job_running = False

    @property
    def store(self) -> Store:
        return self.app.store  # type: ignore[attr-defined]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="home-top"):
            yield DataTable(id="stats", cursor_type="none", zebra_stripes=False)
            yield Static(HELP, id="help")
        yield Label("", id="progress-label")
        yield ProgressBar(id="progress", show_eta=True)
        yield RichLog(id="log", markup=True, max_lines=500)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#stats", DataTable)
        table.add_columns("状態", "曲数")
        self.refresh_stats()
        self.query_one("#log", RichLog).write("[dim]照合の結果がここに流れます（✓ 自動一致 / ? 要確認 / ✗ 見つからない）[/]")

    def on_screen_resume(self) -> None:
        self.refresh_stats()

    def refresh_stats(self) -> None:
        stats = self.store.stats()
        total = self.store.count()
        table = self.query_one("#stats", DataTable)
        table.clear()
        for status in Status:
            table.add_row(status_text(status), Text(str(stats.get(status, 0)), justify="right"))
        to_like = sum(stats.get(s, 0) for s in LIKE_STATUSES)
        table.add_row(Text("合計", style="bold"), Text(str(total), justify="right", style="bold"))
        table.add_row(Text("高評価予定", style="green"), Text(str(to_like), justify="right"))
        table.add_row(Text("高評価済み ♥", style="red"), Text(str(self.store.liked_count()), justify="right"))
        table.add_row(Text("Premium限定", style="dim"), Text(PREMIUM_POLICIES[self.store.premium_policy].split("（")[0], style="dim"))
        pending = stats.get(Status.PENDING, 0) + stats.get(Status.ERROR, 0)
        open_ = sum(stats.get(s, 0) for s in OPEN_STATUSES)
        bar = self.query_one("#progress", ProgressBar)
        bar.update(total=total or None, progress=total - pending)
        if not self._job_running:
            msg = (
                f"未照合 {pending} 曲 — [b]m[/b] で照合を開始" if pending
                else f"照合完了。未決定 {open_} 曲 — [b]r[/b] で確認" if open_
                else "すべて決定済み — [b]a[/b] で高評価を適用できます"
            )
            self.query_one("#progress-label", Label).update(msg)

    def action_match(self) -> None:
        if self._job_running:
            self.notify("照合はすでに実行中です")
            return
        if not self.store.uris_to_match():
            self.notify("未照合の曲はありません")
            return
        self._stop.clear()
        self._job_running = True
        self.query_one("#progress-label", Label).update("照合中…（[b]s[/b] で停止。停止しても結果は保存されます）")
        self.run_match()

    def action_stop(self) -> None:
        if self._job_running:
            self._stop.set()
            self.query_one("#progress-label", Label).update("停止中…（実行中の検索が終わるのを待っています）")

    @work(thread=True, exclusive=True, group="match")
    def run_match(self) -> None:
        log = self.query_one("#log", RichLog)
        icons = {Status.AUTO: "[green]✓[/]", Status.REVIEW: "[yellow]?[/]",
                 Status.NOT_FOUND: "[red]✗[/]", Status.ERROR: "[bold red]![/]"}

        def on_result(row: Row, _decision: object) -> None:
            sel = row.selected
            target = f" → {sel.title} / {', '.join(sel.artists)}" if sel else f"  [dim]{row.reason}[/]"
            line = f"{icons.get(row.status, '·')} {row.track.title} / {', '.join(row.track.artists)}{target}"
            try:
                self.app.call_from_thread(log.write, line)
                self.app.call_from_thread(self.refresh_stats)
            except RuntimeError:  # app is shutting down
                self._stop_event.set()

        try:
            backend = self.app.backend()  # type: ignore[attr-defined]
            match_all(self.store, backend, self._stop, on_result)
        except Exception as e:  # pragma: no cover - surfaced to the user
            self.app.call_from_thread(self.notify, f"照合エラー: {e}", severity="error")
        finally:
            self._job_running = False
            try:
                self.app.call_from_thread(self.refresh_stats)
                self.app.call_from_thread(self.notify, "照合を終了しました")
            except RuntimeError:
                pass

    def action_review(self) -> None:
        queue = self.store.review_queue()
        if not queue:
            self.notify("確認が必要な曲はありません")
            return
        self.app.push_screen(ReviewScreen(queue))

    def action_list(self) -> None:
        self.app.push_screen(ListScreen())

    def action_settings(self) -> None:
        if self._job_running:
            self.notify("照合の実行中は変更できません", severity="warning")
            return

        def chosen(policy: str | None) -> None:
            if policy and policy != self.store.premium_policy:
                self.store.set_setting("premium", policy)
                changes = redecide(self.store)
                self.refresh_stats()
                summary = ", ".join(f"{k}: {v}" for k, v in changes.items()) or "変更なし"
                self.notify(f"Premium 限定の曲: {PREMIUM_POLICIES[policy]}（判定を更新: {summary}）", timeout=8)

        self.app.push_screen(PremiumModal(self.store.premium_policy), chosen)

    def action_music_videos(self) -> None:
        if self._job_running:
            self.notify("照合の実行中は使えません", severity="warning")
            return
        self.app.push_screen(MusicVideoScreen())

    def action_apply(self) -> None:
        if self._job_running:
            self.notify("照合の実行中は適用できません", severity="warning")
            return
        self.app.push_screen(ApplyScreen())


# --- review -------------------------------------------------------------------


class ReviewScreen(Screen):
    BINDINGS = [
        Binding("enter", "choose", "この候補に決定", show=True, priority=False),
        Binding("s", "skip", "スキップ"),
        Binding("d", "defer", "後で判断"),
        Binding("slash", "search", "再検索"),
        Binding("p", "play", "候補を開く"),
        Binding("o", "open_spotify", "Spotifyで開く"),
        Binding("u", "undo", "未決定に戻す"),
        Binding("right,n", "next", "次へ"),
        Binding("left,b", "prev", "前へ"),
        Binding("escape", "back", "戻る"),
        *[Binding(str(i), f"choose_index({i - 1})", show=False) for i in range(1, 10)],
    ]

    def __init__(self, uris: list[str], index: int = 0) -> None:
        super().__init__()
        self.uris = uris
        self.index = index
        self.scored: list[ScoredCandidate] = []

    @property
    def store(self) -> Store:
        return self.app.store  # type: ignore[attr-defined]

    @property
    def uri(self) -> str:
        return self.uris[self.index]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="review"):
            yield Static(id="position")
            yield Static(id="source")
            yield Static(id="reason")
            yield DataTable(id="candidates", cursor_type="row", zebra_stripes=True)
            yield Static(id="detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#candidates", DataTable)
        for label, width in [("#", 3), ("判定", 6), ("曲名", 40), ("アーティスト", 26), ("アルバム", 26),
                             ("長さ", 5), ("差", 5), ("メモ", 60)]:
            table.add_column(label, width=width)
        self.render_track()
        table.focus()

    def render_track(self) -> None:
        row = self.store.get(self.uri)
        t = row.track
        self.app.sub_title = "曲の確認"
        pos = Text.assemble(
            (f" {self.index + 1} / {len(self.uris)} ", "reverse"), "  現在: ",
            status_text(row.status, row.liked),
        )
        if row.selected:
            pos.append(f"  → {row.selected.title} / {', '.join(row.selected.artists)}", style="cyan")
        pos.append("    Enter / 1-9: 候補に決定", style="dim")
        self.query_one("#position", Static).update(pos)
        source = Text()
        source.append("Spotify  ", style="bold green")
        source.append(t.title, style="bold")
        source.append(f"\n  アーティスト: {', '.join(t.artists)}")
        source.append(f"\n  アルバム:     {t.album}")
        source.append(f"\n  長さ: {fmt_duration(t.duration_s)}   ISRC: {t.isrc or '-'}", style="dim")
        self.query_one("#source", Static).update(source)
        self.query_one("#reason", Static).update(Text(f"判定理由: {row.reason}", style="yellow"))

        self.scored = rank(t, row.candidates, self.store.premium_policy)
        table = self.query_one("#candidates", DataTable)
        table.clear()
        selected_index = 0
        for i, sc in enumerate(self.scored):
            c = sc.candidate
            mark = Text("✓ 一致", style="green") if sc.exact else Text(f"{sc.score:.0f}点", style="yellow")
            diff = sc.duration_diff
            diff_text = Text("" if diff is None else f"{diff:+.0f}s",
                             style="green" if diff is not None and abs(diff) <= 3 else "red")
            chosen = c.video_id == row.selected_video_id
            table.add_row(
                Text(("▶" if chosen else "") + str(i + 1), style="bold cyan" if chosen else ""),
                mark,
                title_cell(c),
                ", ".join(c.artists),
                c.album,
                fmt_duration(c.duration_s),
                diff_text,
                Text("; ".join(sc.notes), style="dim"),
                key=c.video_id,
            )
            if chosen:
                selected_index = i
        if self.scored:
            table.move_cursor(row=selected_index)
        self.update_detail()

    def current(self) -> ScoredCandidate | None:
        table = self.query_one("#candidates", DataTable)
        if not self.scored or table.cursor_row is None or table.cursor_row >= len(self.scored):
            return None
        return self.scored[table.cursor_row]

    def update_detail(self) -> None:
        sc = self.current()
        if not sc:
            self.query_one("#detail", Static).update(
                Text("候補がありません。/ で再検索するか、s でスキップしてください。", style="red"))
            return
        c = sc.candidate
        text = Text()
        text.append("YouTube Music  ", style="bold red")
        text.append(c.title, style="bold")
        if len(c.titles) > 1:
            text.append(f"  ({' / '.join(c.titles[1:])})", style="dim")
        text.append(f"\n  アーティスト: {', '.join(c.artists)}")
        if c.alt_artists and c.alt_artists != c.artists:
            text.append(f"  ({', '.join(c.alt_artists)})", style="dim")
        text.append(f"\n  アルバム:     {c.album}")
        text.append(f"\n  {c.url}", style="dim")
        self.query_one("#detail", Static).update(text)

    @on(DataTable.RowHighlighted)
    def _highlighted(self) -> None:
        self.update_detail()

    @on(DataTable.RowSelected)
    def _selected(self) -> None:
        self.action_choose()

    def _decide(self, status: Status, video_id: str | None, reason: str) -> None:
        row = self.store.get(self.uri)
        if row.liked and video_id != row.selected_video_id:
            self.notify("この曲の元の候補は既に高評価済みです。必要なら YouTube Music 側で解除してください",
                        severity="warning", timeout=8)
        self.store.set_status(self.uri, status, video_id, reason)
        chosen = next((c for c in row.candidates if c.video_id == video_id), None)
        logger.info("review %-9s %s -> %s", status.value, describe_track(row.track), describe_candidate(chosen))
        self.action_next()

    def action_choose(self) -> None:
        sc = self.current()
        if sc:
            self._decide(Status.CONFIRMED, sc.candidate.video_id, "手動で選択")

    def action_choose_index(self, i: int) -> None:
        if i < len(self.scored):
            self.query_one("#candidates", DataTable).move_cursor(row=i)
            self.action_choose()

    def action_skip(self) -> None:
        self._decide(Status.SKIPPED, None, "スキップ（移行しない）")

    def action_defer(self) -> None:
        self._decide(Status.DEFERRED, None, "後で判断")

    def action_undo(self) -> None:
        self.store.set_status(self.uri, Status.REVIEW, None, "未決定に戻した")
        logger.info("review undo      %s", describe_track(self.store.get(self.uri).track))
        self.render_track()

    def action_next(self) -> None:
        if self.index + 1 < len(self.uris):
            self.index += 1
            self.render_track()
        else:
            self.notify("最後の曲です。お疲れさまでした！")
            self.render_track()

    def action_prev(self) -> None:
        if self.index > 0:
            self.index -= 1
            self.render_track()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_play(self) -> None:
        sc = self.current()
        if sc:
            self.app.open_url(sc.candidate.url)

    def action_open_spotify(self) -> None:
        self.app.open_url(self.store.get(self.uri).track.url)

    def action_search(self) -> None:
        t = self.store.get(self.uri).track
        self.app.push_screen(SearchModal(f"{t.title} {' '.join(t.artists[:2])}"), self._on_query)

    def _on_query(self, query: str | None) -> None:
        if query:
            self.notify(f"検索中: {query}")
            self.run_search(self.uri, query)

    @work(thread=True, exclusive=True, group="search")
    def run_search(self, uri: str, query: str) -> None:
        try:
            backend = self.app.backend()  # type: ignore[attr-defined]
            found = manual_search(backend, self.store.get(uri).track, query)
        except Exception as e:
            self.app.call_from_thread(self.notify, f"検索エラー: {e}", severity="error")
            return
        self.store.add_candidates(uri, found)
        logger.info("research %r -> %d results for %s", query, len(found), describe_track(self.store.get(uri).track))
        self.app.call_from_thread(self.notify, f"{len(found)} 件の結果を候補に追加しました")
        if uri == self.uri:
            self.app.call_from_thread(self.render_track)


# --- list -------------------------------------------------------------------


FILTERS: list[tuple[str, tuple[Status, ...] | None]] = [
    ("すべて", None),
    ("自動一致", (Status.AUTO,)),
    ("未決定", OPEN_STATUSES),
    ("手動確定", (Status.CONFIRMED,)),
    ("スキップ", (Status.SKIPPED,)),
    ("未照合/エラー", (Status.PENDING, Status.ERROR)),
]


class ListScreen(Screen):
    BINDINGS = [
        Binding("f", "cycle_filter", "絞り込み切替"),
        Binding("slash", "focus_search", "文字で絞り込み"),
        Binding("escape", "back", "戻る"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.filter_index = 0

    @property
    def store(self) -> Store:
        return self.app.store  # type: ignore[attr-defined]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="list-bar"):
            yield Label("", id="filter-label")
            yield Input(placeholder="曲名・アーティストで絞り込み（/）", id="text-filter")
        yield DataTable(id="all", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#all", DataTable)
        for label, width in [("#", 4), ("状態", 10), ("Spotify 曲名", 34), ("アーティスト", 22),
                             ("→ YouTube Music", 34), ("アーティスト", 22), ("理由", 60)]:
            table.add_column(label, width=width)
        self.reload()
        table.focus()

    def on_screen_resume(self) -> None:
        self.reload()

    def reload(self) -> None:
        name, statuses = FILTERS[self.filter_index]
        needle = self.query_one("#text-filter", Input).value.strip().casefold()
        rows = self.store.rows(statuses)
        if needle:
            rows = [r for r in rows if needle in f"{r.track.title} {' '.join(r.track.artists)}".casefold()]
        self.uris = [r.track.uri for r in rows]
        table = self.query_one("#all", DataTable)
        cursor = table.cursor_row
        table.clear()
        for r in rows:
            sel = r.selected
            table.add_row(
                str(r.position + 1),
                status_text(r.status, r.liked),
                r.track.title,
                ", ".join(r.track.artists),
                sel.title if sel else "",
                ", ".join(sel.artists) if sel else "",
                Text(r.reason, style="dim"),
                key=r.track.uri,
            )
        if rows and cursor is not None:
            table.move_cursor(row=min(cursor, len(rows) - 1))
        self.query_one("#filter-label", Label).update(
            f"[b]{name}[/b] {len(rows)} 曲  （[b]f[/b] で切替 / Enter で詳細・修正）")

    def action_cycle_filter(self) -> None:
        self.filter_index = (self.filter_index + 1) % len(FILTERS)
        self.reload()

    def action_focus_search(self) -> None:
        self.query_one("#text-filter", Input).focus()

    @on(Input.Changed, "#text-filter")
    def _text_changed(self) -> None:
        self.reload()

    @on(Input.Submitted, "#text-filter")
    def _text_submitted(self) -> None:
        self.query_one("#all", DataTable).focus()

    @on(DataTable.RowSelected, "#all")
    def _open(self, event: DataTable.RowSelected) -> None:
        self.app.push_screen(ReviewScreen(self.uris, event.cursor_row))

    def action_back(self) -> None:
        if self.query_one("#text-filter", Input).has_focus:
            self.query_one("#all", DataTable).focus()
        else:
            self.app.pop_screen()


# --- bulk music videos --------------------------------------------------------


class MusicVideoScreen(Screen):
    """Preview and confirm music videos for tracks that have no audio-track match."""

    BINDINGS = [
        Binding("x", "accept", "まとめて採用…"),
        Binding("p", "play", "MVを開く"),
        Binding("escape", "back", "戻る"),
    ]

    @property
    def store(self) -> Store:
        return self.app.store  # type: ignore[attr-defined]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="mv-summary")
        yield DataTable(id="mv-plan", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#mv-plan", DataTable)
        for label, width in [("#", 4), ("Spotify", 44), ("アルバム", 30), ("長さ", 5),
                             ("→ YouTube Music の MV", 40), ("長さ", 5), ("差", 5)]:
            table.add_column(label, width=width)
        self.reload()
        table.focus()

    def reload(self) -> None:
        self.plan = music_video_plan(self.store)
        table = self.query_one("#mv-plan", DataTable)
        table.clear()
        for i, (row, video_id) in enumerate(self.plan, 1):
            c = next(c for c in row.candidates if c.video_id == video_id)
            diff = (c.duration_s or 0) - row.track.duration_s
            table.add_row(str(i), f"{row.track.title} / {', '.join(row.track.artists)}", row.track.album,
                          fmt_duration(row.track.duration_s), title_cell(c), fmt_duration(c.duration_s),
                          f"{diff:+.0f}s", key=row.track.uri)
        self.query_one("#mv-summary", Static).update(
            f"[b]MV 版で確定できる要確認の曲: {len(self.plan)} 曲[/b]\n"
            "  YouTube Music に音源が無く、曲名・版・アーティスト・アルバム・長さ（3秒以内）が一致する MV だけです。\n"
            "  MV はイントロや効果音が入っていることがあります。気になる曲は [b]p[/b] で開いて確認できます。\n"
            "  確定後も一覧（l）から個別に戻せます。ここでは YouTube Music に何も書き込みません。\n"
            "  [b]x[/b] でまとめて採用")

    def action_play(self) -> None:
        table = self.query_one("#mv-plan", DataTable)
        if self.plan and table.cursor_row is not None:
            _, video_id = self.plan[table.cursor_row]
            self.app.open_url(f"https://music.youtube.com/watch?v={video_id}")

    def action_accept(self) -> None:
        if not self.plan:
            self.notify("対象の曲はありません")
            return

        def done(ok: bool | None) -> None:
            if ok:
                n = accept_music_videos(self.store, self.plan)
                self.notify(f"{n} 曲を MV 版で確定しました")
                self.app.pop_screen()

        self.app.push_screen(ConfirmModal(f"要確認の [b]{len(self.plan)} 曲[/b]を MV 版で確定します。"), done)

    def action_back(self) -> None:
        self.app.pop_screen()


# --- apply ------------------------------------------------------------------


class ApplyScreen(Screen):
    BINDINGS = [
        Binding("x", "execute", "高評価を実行…"),
        Binding("s", "stop", "停止"),
        Binding("escape", "back", "戻る"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._stop = threading.Event()
        self._job_running = False

    @property
    def store(self) -> Store:
        return self.app.store  # type: ignore[attr-defined]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="apply-top"):
            yield Static(id="summary")
        yield ProgressBar(id="apply-progress", show_eta=True)
        yield DataTable(id="plan", cursor_type="row", zebra_stripes=True)
        yield RichLog(id="apply-log", markup=True, max_lines=300)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#plan", DataTable)
        for label, width in [("順", 5), ("状態", 10), ("Spotify", 44), ("→ YouTube Music", 34),
                             ("アーティスト", 24), ("アルバム", 30)]:
            table.add_column(label, width=width)
        self.reload()

    def reload(self) -> None:
        plan = self.store.like_plan()
        stats = self.store.stats()
        remaining = [r for r in plan if not r.liked]
        open_ = sum(stats.get(s, 0) for s in OPEN_STATUSES)
        pending = stats.get(Status.PENDING, 0) + stats.get(Status.ERROR, 0)
        lines = [
            f"[b]高評価を付ける曲: {len(remaining)} 曲[/b]（計画 {len(plan)} 曲 / 付与済み {len(plan) - len(remaining)} 曲）",
            f"  自動一致 {stats.get(Status.AUTO, 0)} ・ 手動確定 {stats.get(Status.CONFIRMED, 0)}"
            f" ・ スキップ {stats.get(Status.SKIPPED, 0)}（対象外）",
            "  Spotify で古くお気に入りした順に付けるので、YouTube Music でも新しい曲が上に並びます。",
            "  既に高評価済みの曲は API を呼ばずに記録だけします。何度実行しても重複しません。",
        ]
        premium = sum(1 for r in remaining if r.selected and r.selected.premium_only)
        lines.append(f"  Premium 限定の曲: {premium} 曲を含む（設定: {PREMIUM_POLICIES[self.store.premium_policy]}。"
                     "ホーム画面の [b]o[/b] で変更）")
        if open_ or pending:
            lines.append(f"[yellow]⚠ 未決定 {open_} 曲・未照合 {pending} 曲は今回の対象外です（後で再実行できます）[/]")
        lines.append("[b]x[/b] で実行（アカウント名を表示して確認します）")
        self.query_one("#summary", Static).update("\n".join(lines))
        table = self.query_one("#plan", DataTable)
        table.clear()
        for i, r in enumerate(remaining, 1):
            sel = r.selected
            assert sel
            table.add_row(str(i), status_text(r.status, r.liked), f"{r.track.title} / {', '.join(r.track.artists)}",
                          sel.title, ", ".join(sel.artists), sel.album, key=r.track.uri)
        bar = self.query_one("#apply-progress", ProgressBar)
        bar.update(total=len(plan) or None, progress=len(plan) - len(remaining))

    def action_execute(self) -> None:
        if self._job_running:
            return
        if not [r for r in self.store.like_plan() if not r.liked]:
            self.notify("高評価を付ける曲はありません")
            return
        self.prepare()

    @work(thread=True, exclusive=True, group="apply")
    def prepare(self) -> None:
        try:
            liker = self.app.liker()  # type: ignore[attr-defined]
            account = liker.account_name()
        except Exception as e:
            self.app.call_from_thread(self.notify, f"認証エラー: {e}", severity="error", timeout=10)
            return
        count = len([r for r in self.store.like_plan() if not r.liked])
        message = (
            f"YouTube Music アカウント [b]{account}[/b] の「高評価した音楽」に\n"
            f"[b]{count} 曲[/b]を追加します。この操作はアカウントを変更します。"
        )

        def confirmed(ok: bool | None) -> None:
            if ok:
                self._stop.clear()
                self._job_running = True
                self.run_apply(liker)

        self.app.call_from_thread(self.app.push_screen, ConfirmModal(message), confirmed)

    @work(thread=True, exclusive=True, group="apply-run")
    def run_apply(self, liker: LikeClient) -> None:
        log = self.query_one("#apply-log", RichLog)
        bar = self.query_one("#apply-progress", ProgressBar)
        call = self.app.call_from_thread
        call(log.write, "高評価済みの曲を取得しています…")

        def progress(i: int, n: int, row: Row, state: str) -> None:
            mark = "[green]♥[/]" if state == "ok" else f"[red]✗ {state}[/]"
            sel = row.selected
            try:
                call(log.write, f"{i}/{n} {mark} {sel.title if sel else ''} / {', '.join(sel.artists) if sel else ''}")
                call(bar.advance, 1)
            except RuntimeError:  # app is shutting down; the like itself is already recorded
                self._stop_event.set()

        try:
            result: ApplyResult = apply_likes(self.store, liker, self._stop, progress)
        except Exception as e:
            call(self.notify, f"エラー: {e}", severity="error", timeout=10)
            result = ApplyResult(aborted=str(e))
        finally:
            self._job_running = False
        summary = f"完了: 追加 {result.applied} / 既に高評価済み {result.already} / 失敗 {result.failed}"
        if result.aborted:
            summary += f" — {result.aborted}（もう一度 x で続きから再開できます）"
        call(log.write, f"[b]{summary}[/b]")
        call(self.notify, summary, timeout=10)
        call(self.reload)

    def action_stop(self) -> None:
        if self._job_running:
            self._stop.set()
            self.notify("停止します…")

    def action_back(self) -> None:
        if self._job_running:
            self.notify("実行中です。s で停止してから戻ってください", severity="warning")
            return
        self.app.pop_screen()


# --- app --------------------------------------------------------------------


class YTMImportApp(App):
    TITLE = "Spotify → YouTube Music"
    CSS = """
    .dialog { width: 80; height: auto; padding: 1 2; border: thick $accent; background: $surface; }
    SearchModal, ConfirmModal { align: center middle; }
    #home-top { height: 17; }
    #stats { width: 30; }
    #help { padding: 0 2; }
    #progress-label { padding: 1 1 0 1; }
    #progress { padding: 0 1; }
    #log { border: round $panel; }
    #position { padding: 0 1; }
    #source { padding: 1 1 0 1; height: auto; }
    #reason { padding: 0 1 1 1; }
    #candidates { height: 1fr; }
    #detail { height: auto; padding: 1; border-top: solid $panel; }
    #list-bar { height: 3; }
    #filter-label { width: auto; padding: 1; }
    #text-filter { width: 1fr; }
    #all { height: 1fr; }
    #apply-top { height: auto; max-height: 10; padding: 1; }
    #mv-summary { padding: 1; }
    #mv-plan { height: 1fr; }
    #apply-progress { padding: 0 1; }
    #plan { height: 1fr; }
    #apply-log { height: 10; border: round $panel; }
    """

    def __init__(
        self,
        store: Store,
        backend_factory: Callable[[], SearchBackend],
        liker_factory: Callable[[], LikeClient],
    ) -> None:
        super().__init__()
        self.store = store
        self._backend_factory = backend_factory
        self._backend: SearchBackend | None = None
        self._backend_lock = threading.Lock()
        self._liker_factory = liker_factory

    def backend(self) -> SearchBackend:
        with self._backend_lock:
            if self._backend is None:
                self._backend = self._backend_factory()
            return self._backend

    def liker(self) -> LikeClient:
        return self._liker_factory()

    def on_mount(self) -> None:
        self.push_screen(HomeScreen())

    def exit(self, *args, **kwargs) -> None:  # type: ignore[override]
        # Stop background jobs so the process can exit promptly; finished work is already saved.
        for screen in self.screen_stack:
            event = getattr(screen, "_stop_event", None)
            if isinstance(event, threading.Event):
                event.set()
        super().exit(*args, **kwargs)

