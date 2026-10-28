import threading

from conftest import FakeLiker, FakeSearch, result, track
from textual.widgets import DataTable, Input

from ytm_import.models import Status
from ytm_import.runner import match_all
from ytm_import.tui import ApplyScreen, ConfirmModal, HomeScreen, ListScreen, ReviewScreen, YTMImportApp


def setup(store):
    store.import_tracks([
        track("START:DASH!!", ["μ's"], seconds=257, uri="spotify:track:1"),
        track("Song", ["Band"], seconds=200, uri="spotify:track:2"),
        track("Other", ["Band"], seconds=200, uri="spotify:track:3"),
    ])
    members = ["高坂穂乃果(CV.新田恵海)", "南ことり(CV.内田 彩)", "園田海未(CV.三森すずこ)", "μ's"]
    backend = FakeSearch({
        ("START:DASH!! μ's", "ja"): [
            result("trio", "START:DASH!!", members, seconds=254),
            result("tv", "START:DASH!! (TVサイズ)", ["μ's"], seconds=185),
        ],
        ("Song Band", "ja"): [result("song", "Song", ["Band"], seconds=200)],
        ("Other Band", "ja"): [result("o1", "Other (Live)", ["Band"], seconds=230)],
        ("manual", "ja"): [result("group", "START:DASH!!", ["μ's"], seconds=256)],
    })
    return backend


async def test_match_review_and_apply_flow(store):
    backend = setup(store)
    liker = FakeLiker()
    app = YTMImportApp(store, lambda: backend, lambda: liker)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)
        await pilot.press("m")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert store.stats()[Status.AUTO] == 1
        assert len(store.review_queue()) == 2

        # Review: first queued track is START:DASH!! – re-search, then pick the new candidate.
        await pilot.press("r")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ReviewScreen)
        assert store.get(screen.uri).track.title == "START:DASH!!"
        await pilot.press("slash")
        await pilot.pause()
        search = app.screen.query_one(Input)
        search.value = "manual"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        table = screen.query_one("#candidates", DataTable)
        assert table.row_count == 3
        assert screen.scored[0].candidate.video_id == "group"  # exact match ranked first
        await pilot.press("1")
        await pilot.pause()
        assert store.get("spotify:track:1").status == Status.CONFIRMED
        assert store.get("spotify:track:1").selected_video_id == "group"
        # Next track: defer it.
        await pilot.press("d")
        assert store.get("spotify:track:3").status == Status.DEFERRED
        await pilot.press("escape")
        await pilot.pause()

        # Apply: nothing is written until the confirmation word is typed.
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, ApplyScreen)
        await pilot.press("x")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)
        assert liker.liked == []
        app.screen.query_one(Input).value = "no"
        await pilot.press("enter")
        await pilot.pause()
        assert liker.liked == [] and isinstance(app.screen, ConfirmModal)
        app.screen.query_one(Input).value = "yes"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert sorted(liker.liked) == ["group", "song"]
        assert store.liked_count() == 2


async def test_cancel_confirmation_writes_nothing(store):
    backend = setup(store)
    match_all(store, backend, threading.Event())
    liker = FakeLiker()
    app = YTMImportApp(store, lambda: backend, lambda: liker)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        await pilot.press("a", "x")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ApplyScreen)
        assert liker.liked == []


async def test_list_filter_and_edit_auto_match(store):
    backend = setup(store)
    match_all(store, backend, threading.Event())
    app = YTMImportApp(store, lambda: backend, FakeLiker)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ListScreen)
        table = screen.query_one("#all", DataTable)
        assert table.row_count == 3
        await pilot.press("f")  # auto only
        await pilot.pause()
        assert table.row_count == 1
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ReviewScreen)
        await pilot.press("s")  # user decides not to migrate this one
        assert store.get("spotify:track:2").status == Status.SKIPPED
        await pilot.press("escape")
        await pilot.pause()
        assert table.row_count == 0


async def test_enter_decides_exactly_one_track(store):
    backend = setup(store)
    match_all(store, backend, threading.Event())
    app = YTMImportApp(store, lambda: backend, FakeLiker)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        screen = app.screen
        first, second = screen.uris[0], screen.uris[1]
        await pilot.press("enter")
        await pilot.pause()
        assert store.get(first).status == Status.CONFIRMED
        assert store.get(second).status != Status.CONFIRMED
        assert screen.index == 1


async def test_quit_during_matching_stops_workers(store):
    import time

    store.import_tracks([track(f"Song {i}", ["Band"], uri=f"spotify:track:s{i}") for i in range(20)])

    class Slow:
        def search_songs(self, q, lang):
            time.sleep(0.2)
            return []

    app = YTMImportApp(store, Slow, FakeLiker)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        await pilot.press("m")
        await pilot.pause(0.1)
        app.exit()
    start = time.monotonic()
    for t in __import__("threading").enumerate():
        if t.name.startswith("ThreadPoolExecutor"):
            t.join(timeout=5)
    assert time.monotonic() - start < 3
    assert store.uris_to_match()  # stopped before finishing everything


async def test_bulk_accept_music_videos(store):
    from ytm_import.tui import MusicVideoScreen

    store.import_tracks([track("誰か", ["いきものがかり"], "〇", 317, uri="spotify:track:mv"),
                         track("Song", ["Band"], seconds=200, uri="spotify:track:2")])
    b = FakeSearch({("誰か いきものがかり", "ja"): [
        result("mv", "誰か", ["いきものがかり"], "〇", 318, video_type="MUSIC_VIDEO_TYPE_OMV")]})
    match_all(store, b, threading.Event())
    assert store.get("spotify:track:mv").status == Status.REVIEW
    liker = FakeLiker()
    app = YTMImportApp(store, lambda: b, lambda: liker)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
        assert isinstance(app.screen, MusicVideoScreen)
        assert len(app.screen.plan) == 1
        await pilot.press("x")
        await pilot.pause()
        app.screen.query_one(Input).value = "yes"
        await pilot.press("enter")
        await pilot.pause()
        row = store.get("spotify:track:mv")
        assert row.status == Status.CONFIRMED and row.selected_video_id == "mv"
        assert liker.liked == []  # local decision only


async def test_premium_setting_modal(store):
    from textual.widgets import OptionList

    from ytm_import.tui import PremiumModal

    setup(store)
    app = YTMImportApp(store, FakeSearch, FakeLiker)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        assert isinstance(app.screen, PremiumModal)
        assert app.screen.query_one(OptionList).highlighted == 0
        await pilot.press("down", "down", "enter")
        await pilot.pause()
        assert store.premium_policy == "exclude"
