"""Migrate Spotify liked songs (Exportify CSV) to YouTube Music likes."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import threading
from pathlib import Path

DEFAULT_DATA = Path("data/state.sqlite")

AUTH_HELP = """\
YouTube Music の認証情報を設定します。

  1. ブラウザで https://music.youtube.com を開き、ログインする
  2. 開発者ツール（macOS: ⌥⌘I / Windows・Linux: F12）→ Network タブを開き、検索欄に「browse」と入力
  3. ページ内でどこかをクリックして表示された POST リクエストを右クリック
     → Copy → Copy request headers（Chrome）/ Copy Request Headers（Firefox/Safari）
  4. 下に貼り付け、最後に改行してから Ctrl-D（Windows は Ctrl-Z → Enter）を押す
     （macOS ではコピー後に `pbpaste | ytm-import auth` でも可）

認証情報は {path} に保存され（macOS/Linux では権限 600）、画面やログには表示されません。
"""


def cmd_auth(args: argparse.Namespace) -> int:
    from .ytm import clean_headers, save_auth

    path = Path(args.auth)
    print(AUTH_HELP.format(path=path))
    raw = sys.stdin.read()
    headers = clean_headers(raw, args.authuser)
    if "cookie:" not in headers:
        print("cookie ヘッダーが見つかりません。リクエストヘッダー全体か、cookie の値をコピーしてください。", file=sys.stderr)
        return 1
    if "x-goog-authuser:" not in headers:
        print("x-goog-authuser が見つかりません。--authuser に Network タブの x-goog-authuser の値（0 や 1）を指定してください。",
              file=sys.stderr)
        return 1
    save_auth(raw, path, args.authuser)
    print(f"保存しました: {path}")
    try:
        from .ytm import Liker

        print(f"ログイン確認 OK: {Liker(path).account_name()}")
    except Exception as e:
        print(f"ログイン確認に失敗しました: {e}", file=sys.stderr)
        return 1
    return 0


def _open_store(args: argparse.Namespace):
    from . import log
    from .store import Store

    log.setup(Path(args.data).with_name("ytm-import.log"))
    return Store(args.data)


def _import_csv(store, csv_path: str) -> None:
    from .spotify_csv import load_tracks

    tracks = load_tracks(csv_path)
    added = store.import_tracks(tracks)
    print(f"CSV を読み込みました: {len(tracks)} 曲（新規 {added} 曲、既存の進捗は保持）")


def cmd_import(args: argparse.Namespace) -> int:
    _import_csv(_open_store(args), args.csv)
    return 0


def cmd_match(args: argparse.Namespace) -> int:
    """Headless matching (same as `m` in the TUI)."""
    from .runner import match_all
    from .ytm import YTMusicSearch

    store = _open_store(args)
    if args.retry_open:
        n = store.reset_for_retry()
        print(f"要確認・見つからない {n} 曲を再照合します（手動の判断は保持）")
    total = len(store.uris_to_match())
    stop = threading.Event()
    count = 0

    def on_result(row, _decision) -> None:
        nonlocal count
        count += 1
        print(f"[{count}/{total}] {row.status.value:9} {row.track.title} / {', '.join(row.track.artists)}")

    try:
        match_all(store, YTMusicSearch(), stop, on_result)
    except KeyboardInterrupt:
        stop.set()
        print("中断しました（結果は保存済み。再実行で続きから）")
    print({k.value: v for k, v in store.stats().items()})
    print(f"詳細ログ: {Path(args.data).with_name('ytm-import.log')}")
    return 0


def cmd_redecide(args: argparse.Namespace) -> int:
    from .runner import redecide

    changes = redecide(_open_store(args))
    print("変更なし" if not changes else "\n".join(f"{k}: {v}" for k, v in changes.items()))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    store = _open_store(args)
    with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["status", "spotify_title", "spotify_artists", "spotify_album", "ytm_title", "ytm_artists",
                    "ytm_album", "ytm_url", "liked", "reason"])
        for r in store.rows():
            s = r.selected
            w.writerow([r.status.value, r.track.title, ", ".join(r.track.artists), r.track.album,
                        s.title if s else "", ", ".join(s.artists) if s else "", s.album if s else "",
                        s.url if s else "", "yes" if r.liked else "", r.reason])
    print(f"書き出しました: {args.out}")
    return 0


def _print_json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=1))


def _parse_statuses(text: str):
    from .models import Status

    return tuple(Status(s.strip()) for s in text.split(",") if s.strip())


def cmd_status(args: argparse.Namespace) -> int:
    from .ops import status_summary

    _print_json(status_summary(_open_store(args)))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    from .ops import audit

    result = audit(_open_store(args))
    _print_json({k: {"count": len(v), "items": v[: args.limit]} for k, v in result.items()})
    return 0


def cmd_review_list(args: argparse.Namespace) -> int:
    """One JSON object per line: a track and its ranked candidates."""
    from .ops import review_items

    store = _open_store(args)
    for item in review_items(store, _parse_statuses(args.status), args.limit, args.offset, args.candidates):
        print(json.dumps(item, ensure_ascii=False))
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    from .ops import apply_decisions

    store = _open_store(args)
    if args.file:
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
        decisions = json.loads(text)
    else:
        if not args.uri:
            print("uri か --file を指定してください", file=sys.stderr)
            return 2
        d: dict = {"uri": args.uri}
        if args.pick:
            d |= {"action": "pick", "video_id": args.pick}
        elif args.pick_n:
            d |= {"action": "pick", "n": args.pick_n}
        else:
            d["action"] = "skip" if args.skip else "defer" if args.defer else "reset" if args.reset else None
        decisions = [d]
    try:
        warnings = apply_decisions(store, decisions)
    except ValueError as e:
        print(f"何も記録していません:\n{e}", file=sys.stderr)
        return 1
    for w in warnings:
        print(f"警告: {w}", file=sys.stderr)
    print(f"{len(decisions)} 件を記録しました")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from .ops import review_items
    from .ytm import YTMusicSearch, manual_search

    store = _open_store(args)
    row = store.get(args.uri)
    found = manual_search(YTMusicSearch(), row.track, args.query)
    store.add_candidates(args.uri, found)
    print(f"{len(found)} 件を候補に追加しました", file=sys.stderr)
    item = next(i for i in review_items(store, (row.status,), 10**6, 0, args.candidates) if i["uri"] == args.uri)
    print(json.dumps(item, ensure_ascii=False))
    return 0


def cmd_add_video(args: argparse.Namespace) -> int:
    from .ops import review_items
    from .ytm import YTMusicSearch, to_candidate

    store = _open_store(args)
    row = store.get(args.uri)
    backend = YTMusicSearch()
    found = []
    for vid in args.video_ids:
        result = backend.video(vid)
        if result is None:
            print(f"{vid}: 見つかりません", file=sys.stderr)
            continue
        found.append(to_candidate(result))
    store.add_candidates(args.uri, found)
    print(f"{len(found)} 件を候補に追加しました", file=sys.stderr)
    item = next(i for i in review_items(store, (row.status,), 10**6, 0, args.candidates) if i["uri"] == args.uri)
    print(json.dumps(item, ensure_ascii=False))
    return 0


def cmd_accept_mv(args: argparse.Namespace) -> int:
    from .log import describe_candidate, describe_track
    from .runner import accept_music_videos, music_video_plan

    store = _open_store(args)
    plan = music_video_plan(store)
    for row, video_id in plan:
        chosen = next(c for c in row.candidates if c.video_id == video_id)
        print(f"{describe_track(row.track)} -> {describe_candidate(chosen)}")
    if not args.execute:
        print(f"\n[dry-run] MV 版で確定できる曲: {len(plan)} 曲（--execute で確定。YouTube Music には書き込まない）")
        return 0
    print(f"\n{accept_music_videos(store, plan)} 曲を MV 版で確定しました")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    from .models import PREMIUM_POLICIES
    from .runner import redecide

    store = _open_store(args)
    if args.value is None:
        print(f"premium = {store.premium_policy}（{PREMIUM_POLICIES[store.premium_policy]}）")
        return 0
    store.set_setting("premium", args.value)
    print(f"premium = {args.value}（{PREMIUM_POLICIES[args.value]}）")
    changes = redecide(store)
    print("判定を更新: " + (", ".join(f"{k}: {v}" for k, v in changes.items()) or "変更なし"))
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    """Read-only: show which YouTube Music account the credentials belong to."""
    from .ytm import Liker

    print(Liker(Path(args.auth)).account_name())
    return 0


def cmd_follow(args: argparse.Namespace) -> int:
    """Follow artists listed in a JSON file [{"name", "channel_id"}]. Dry-run unless --execute."""
    from . import log
    from .log import logger
    from .ytm import Liker

    log.setup(Path(args.data).with_name("ytm-import.log"))
    artists = [a for a in json.loads(Path(args.file).read_text()) if a.get("channel_id")]
    if not args.execute:
        for a in artists:
            print(f"{a['name']} -> {a['channel_id']}")
        print(f"[dry-run] {len(artists)} 組。YouTube Music には何も書き込んでいません（--execute で実行）")
        return 0
    liker = Liker(Path(args.auth))
    print(f"アカウント: {liker.account_name()} で {len(artists)} 組をフォローします")
    try:
        followed = liker.followed_channel_ids()
    except Exception as e:
        logger.warning("could not fetch followed artists (%s); following all", e)
        followed = set()
    done = skipped = failed = consecutive = 0
    for a in artists:
        if a["channel_id"] in followed:
            skipped += 1
            continue
        try:
            liker.follow(a["channel_id"])
        except Exception as e:
            failed += 1
            consecutive += 1
            logger.error("follow FAILED %s (%s): %s", a["name"], a["channel_id"], e)
            print(f"失敗: {a['name']}: {e}", file=sys.stderr)
            if consecutive >= 3:
                print("3 回連続で失敗したため中断しました", file=sys.stderr)
                break
            continue
        consecutive = 0
        done += 1
        logger.info("follow ok %s (%s)", a["name"], a["channel_id"])
    print(f"フォロー {done} / 既にフォロー済み {skipped} / 失敗 {failed}")
    return 1 if failed else 0


def cmd_apply(args: argparse.Namespace) -> int:
    from .ops import like_plan_summary

    store = _open_store(args)
    summary = like_plan_summary(store)
    if not args.execute:
        _print_json(summary)
        print("\n[dry-run] YouTube Music には何も書き込んでいません（--execute で実行）")
        return 0
    if summary["to_like"] == 0:
        print("高評価を付ける曲はありません")
        return 0

    from .runner import apply_likes
    from .ytm import Liker

    liker = Liker(Path(args.auth))
    print(f"アカウント: {liker.account_name()} に {summary['to_like']} 曲の高評価を付けます", flush=True)
    stop = threading.Event()

    def progress(i: int, n: int, row, state: str) -> None:
        if state != "ok" or i % 25 == 0 or i == n:
            print(f"[{i}/{n}] {state} {row.track.title} / {', '.join(row.track.artists)}", flush=True)

    try:
        result = apply_likes(store, liker, stop, progress)
    except KeyboardInterrupt:
        stop.set()
        print("中断しました（付与済みの曲は記録済み。再実行で続きから）")
        return 1
    print(f"追加 {result.applied} / 既に高評価済み {result.already} / 失敗 {result.failed}"
          + (f" — {result.aborted}" if result.aborted else ""))
    return 1 if result.aborted else 0


def cmd_tui(args: argparse.Namespace) -> int:
    from .tui import YTMImportApp
    from .ytm import Liker, YTMusicSearch

    store = _open_store(args)
    if args.csv:
        _import_csv(store, args.csv)
    if store.count() == 0:
        print("曲がありません。まず CSV を指定してください: ytm-import --csv liked.csv", file=sys.stderr)
        return 1
    auth = Path(args.auth)
    YTMImportApp(store, YTMusicSearch, lambda: Liker(auth)).run()
    return 0


def main(argv: list[str] | None = None) -> int:
    from .ytm import DEFAULT_AUTH_PATH

    parser = argparse.ArgumentParser(prog="ytm-import", description=__doc__)
    parser.add_argument("--data", default=str(DEFAULT_DATA), help="状態DBのパス (default: %(default)s)")
    parser.add_argument("--auth", default=str(DEFAULT_AUTH_PATH), help="認証ファイル (default: %(default)s)")
    parser.add_argument("--csv", help="Exportify の CSV（TUI 起動前に取り込む）")
    parser.set_defaults(func=cmd_tui)
    sub = parser.add_subparsers(title="commands")
    sub.add_parser("tui", help="TUI を起動（既定）").set_defaults(func=cmd_tui)
    p = sub.add_parser("import", help="CSV を取り込む")
    p.add_argument("csv")
    p.set_defaults(func=cmd_import)
    p = sub.add_parser("auth", help="YouTube Music の認証情報を設定")
    p.add_argument("--authuser", help="x-goog-authuser の値（cookie の値だけを貼り付けるときに指定）")
    p.set_defaults(func=cmd_auth)
    p = sub.add_parser("match", help="TUI を使わずに照合だけ実行")
    p.add_argument("--retry-open", action="store_true", help="要確認・見つからない曲も再照合する")
    p.set_defaults(func=cmd_match)
    sub.add_parser("redecide", help="保存済みの検索結果で判定だけやり直す（通信なし・手動の判断は保持）").set_defaults(
        func=cmd_redecide)
    sub.add_parser("status", help="現在の状態（JSON）").set_defaults(func=cmd_status)
    p = sub.add_parser("audit", help="誤一致の可能性がある自動一致と、惜しい未決定の曲を一覧（JSON）")
    p.add_argument("--limit", type=int, default=30, help="各グループの最大表示件数")
    p.set_defaults(func=cmd_audit)
    p = sub.add_parser("review-list", help="未決定の曲と候補を 1 行 1 曲の JSON で出力")
    p.add_argument("--status", default="review,not_found,deferred", help="対象の状態（カンマ区切り）")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--candidates", type=int, default=6, help="1 曲あたりの候補数")
    p.set_defaults(func=cmd_review_list)
    p = sub.add_parser("decide", help="判断を記録（ローカルのみ。YouTube Music には書き込まない）")
    p.add_argument("uri", nargs="?")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--pick", metavar="VIDEO_ID")
    g.add_argument("--pick-n", type=int, metavar="N", help="review-list の n 番目の候補")
    g.add_argument("--skip", action="store_true")
    g.add_argument("--defer", action="store_true")
    g.add_argument("--reset", action="store_true", help="未決定に戻す")
    g.add_argument("--file", help='JSON 配列 [{"uri", "action": pick|skip|defer|reset, "video_id"|"n", "reason"?}]（- で標準入力）')
    p.set_defaults(func=cmd_decide)
    p = sub.add_parser("search", help="検索語を指定して再検索し、候補に追加（読み取りのみ）")
    p.add_argument("uri")
    p.add_argument("query")
    p.add_argument("--candidates", type=int, default=10)
    p.set_defaults(func=cmd_search)
    p = sub.add_parser("add-video", help="Web 検索などで見つけた videoId を候補に追加（読み取りのみ）")
    p.add_argument("uri")
    p.add_argument("video_ids", nargs="+")
    p.add_argument("--candidates", type=int, default=10)
    p.set_defaults(func=cmd_add_video)
    p = sub.add_parser("accept-mv", help="MV 版の一括採用（既定は dry-run）")
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=cmd_accept_mv)
    p = sub.add_parser("config", help="設定の表示・変更")
    p.add_argument("key", choices=["premium"], help="premium: Premium 限定の曲の扱い")
    p.add_argument("value", nargs="?", choices=["include", "review", "exclude"],
                   help="include=追加する（既定） / review=要確認に回す / exclude=追加しない")
    p.set_defaults(func=cmd_config)
    sub.add_parser("whoami", help="認証情報のアカウント名を表示（読み取りのみ）").set_defaults(func=cmd_whoami)
    p = sub.add_parser("follow", help="アーティストをフォロー（既定は dry-run。--execute でアカウントに書き込む）")
    p.add_argument("file", help='JSON 配列 [{"name", "channel_id"}]')
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=cmd_follow)
    p = sub.add_parser("apply", help="高評価を付ける（既定は dry-run。--execute でアカウントに書き込む）")
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=cmd_apply)
    p = sub.add_parser("export", help="照合結果を CSV に書き出す")
    p.add_argument("out")
    p.set_defaults(func=cmd_export)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
