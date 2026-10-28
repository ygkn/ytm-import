import threading

from conftest import FakeLiker, FakeSearch, result, track

from ytm_import.models import Status
from ytm_import.runner import apply_likes, match_all
from ytm_import.matcher import decide
from ytm_import.ytm import CandidatePool, build_queries, search_track, to_candidate


def tracks():
    return [
        track("ツナガルコネクト", ["天王寺璃奈 (CV.田中ちえ美)"], seconds=238, isrc="JPX1", uri="spotify:track:1"),
        track("Unknown", ["Nobody"], seconds=200, uri="spotify:track:2"),
        track("Song", ["Band"], seconds=200, uri="spotify:track:3"),
    ]


def backend():
    rina = ["天王寺璃奈 (CV.田中ちえ美)"]
    return FakeSearch({
        ("JPX1", "ja"): [result("v1", "ツナガルコネクト", rina, seconds=238)],
        ("ツナガルコネクト 天王寺璃奈 (CV.田中ちえ美)", "ja"): [
            result("v1tv", "ツナガルコネクト (TV size)", rina, seconds=105),
            result("v1", "ツナガルコネクト", rina, seconds=238),
        ],
        ("Song Band", "ja"): [result("v3", "Song", ["Band"], seconds=201)],
    })


def test_isrc_then_text_query_stops_early():
    b = backend()
    decision, _, used = search_track(b, tracks()[0])
    assert decision.status == Status.AUTO
    assert decision.selected.candidate.isrc_hit
    assert len(b.calls) == 2  # ISRC + one text query, no English fallback needed


def test_queries_include_english_fallback():
    qs = build_queries(track("Die For You", ["The Weeknd"], isrc="USX"))
    assert ("USX", "en", True) in qs and qs[0] == ("USX", "ja", True)


def test_pool_merges_locales():
    pool = CandidatePool()
    pool.add([result("w", "Die For You", ["The Weeknd"])], "en")
    pool.add([result("w", "ダイ・フォー・ユー", ["ザ・ウィークエンド"])], "ja")
    [c] = pool.candidates()
    assert c.title == "ダイ・フォー・ユー" and "Die For You" in c.titles
    assert c.artists == ["ザ・ウィークエンド"] and c.alt_artists == ["The Weeknd"]


def test_import_is_idempotent_and_keeps_progress(store):
    assert store.import_tracks(tracks()) == 3
    store.set_status("spotify:track:3", Status.SKIPPED)
    assert store.import_tracks(tracks()) == 0
    assert store.get("spotify:track:3").status == Status.SKIPPED


def test_match_all_resumes_after_stop(store):
    store.import_tracks(tracks())
    stop = threading.Event()
    seen = []

    def on_result(row, _):
        seen.append(row.track.uri)
        stop.set()  # stop after the first finished track

    match_all(store, backend(), stop, on_result, workers=1)
    assert len(seen) == 1
    assert len(store.uris_to_match()) == 2
    match_all(store, backend(), threading.Event(), workers=2)
    assert store.uris_to_match() == []
    stats = store.stats()
    assert stats[Status.AUTO] == 2 and stats[Status.NOT_FOUND] == 1


def test_search_errors_are_retried_next_run(store):
    store.import_tracks(tracks()[:1])

    class Broken:
        def search_songs(self, q, lang):
            raise ConnectionError("offline")

    match_all(store, Broken(), threading.Event())
    assert store.get("spotify:track:1").status == Status.ERROR
    assert store.uris_to_match() == ["spotify:track:1"]


def _prepare(store):
    store.import_tracks(tracks())
    match_all(store, backend(), threading.Event())
    # A second Spotify track that maps to an already planned video
    store.import_tracks(tracks() + [track("ツナガルコネクト", ["天王寺璃奈"], uri="spotify:track:4")])
    store.set_status("spotify:track:4", Status.CONFIRMED, "v1")


def test_apply_likes_oldest_first_once_per_video(store):
    _prepare(store)
    liker = FakeLiker()
    r = apply_likes(store, liker, threading.Event())
    # Oldest Spotify like first: track 4 (position 3, -> v1), then track 3 (-> v3);
    # track 1 also maps to v1 and is not liked twice.
    assert liker.liked == ["v1", "v3"]
    assert r.applied == 2
    # Re-running does nothing
    liker2 = FakeLiker()
    r2 = apply_likes(store, liker2, threading.Event())
    assert liker2.liked == [] and r2.applied == 0


def test_apply_skips_already_liked(store):
    _prepare(store)
    liker = FakeLiker(existing={"v1"})
    r = apply_likes(store, liker, threading.Event())
    assert liker.liked == ["v3"] and r.already == 1
    assert store.get("spotify:track:1").liked


def test_apply_continues_when_existing_likes_cannot_be_fetched(store):
    _prepare(store)
    apply_likes(store, FakeLiker(), threading.Event())
    store.set_status("spotify:track:2", Status.CONFIRMED, "v2")

    class BrokenListLiker(FakeLiker):
        def liked_video_ids(self) -> set[str]:
            raise ValueError("Expecting value")

    liker = BrokenListLiker()
    r = apply_likes(store, liker, threading.Event())
    assert liker.liked == ["v2"] and r.applied == 1  # local records still prevent re-liking v1/v3


def test_apply_aborts_after_consecutive_failures_and_resumes(store):
    _prepare(store)
    r = apply_likes(store, FakeLiker(fail={"v3", "v1"}), threading.Event(), max_consecutive_failures=2)
    assert r.failed == 2 and r.aborted
    liker = FakeLiker()
    apply_likes(store, liker, threading.Event())
    assert liker.liked == ["v1", "v3"]


def test_only_auto_and_confirmed_are_liked(store):
    _prepare(store)
    store.set_status("spotify:track:3", Status.SKIPPED)
    store.set_status("spotify:track:1", Status.DEFERRED)
    store.set_status("spotify:track:4", Status.REVIEW)
    liker = FakeLiker()
    apply_likes(store, liker, threading.Event())
    assert liker.liked == []


def test_redecide_keeps_user_decisions(store):
    store.import_tracks(tracks())
    match_all(store, backend(), threading.Event())
    store.set_status("spotify:track:3", Status.SKIPPED)
    # Simulate an outdated automatic decision
    store.set_status("spotify:track:1", Status.REVIEW, None, "old")
    from ytm_import.runner import redecide

    changes = redecide(store)
    assert changes == {"review -> auto": 1}
    assert store.get("spotify:track:1").status == Status.AUTO
    assert store.get("spotify:track:3").status == Status.SKIPPED


def test_log_records_matches_and_likes(store, tmp_path):
    from ytm_import import log

    path = log.setup(tmp_path / "ytm-import.log")
    store.import_tracks(tracks())
    match_all(store, backend(), threading.Event())
    apply_likes(store, FakeLiker(existing={"v3"}), threading.Event())
    text = path.read_text(encoding="utf-8")
    assert "match auto      ツナガルコネクト" in text
    assert "match not_found Unknown" in text
    assert "like skip (already liked) Song" in text
    assert "like ok ツナガルコネクト" in text


def test_album_tracks_fallback_offers_music_video_for_review(store):
    t = track("誰か", ["いきものがかり"], "〇", 317, uri="spotify:track:9")

    class WithAlbum(FakeSearch):
        def album_tracks(self, album, artist, title=""):
            self.calls.append((f"album:{album}", "ja"))
            return [result("mv", "誰か", ["いきものがかり"], "〇", 318, video_type="MUSIC_VIDEO_TYPE_OMV")]

    b = WithAlbum()
    decision, _, used = search_track(b, t)
    assert ("album:〇", "ja") in b.calls
    assert decision.status == Status.REVIEW  # music video: never automatic
    assert decision.ranked[0].candidate.video_id == "mv"


def test_reset_for_retry_keeps_user_decisions(store):
    store.import_tracks(tracks())
    match_all(store, backend(), threading.Event())
    store.set_status("spotify:track:3", Status.SKIPPED)
    store.set_status("spotify:track:1", Status.REVIEW)
    assert store.reset_for_retry() == 2  # track 1 (review) and track 2 (not found)
    assert store.get("spotify:track:3").status == Status.SKIPPED


def test_premium_only_track_recovered_from_youtube_playlist():
    import threading as th

    from ytm_import.ytm import YTMusicSearch

    yt = object.__new__(YTMusicSearch)
    yt._album_cache, yt._cache_lock = {}, th.Lock()
    yt._search = lambda q, lang, f: [{"title": "さざなみCD", "browseId": "MPRE1"}]
    yt._album = lambda bid: {"title": "さざなみCD", "audioPlaylistId": "OLAK1", "artists": [{"name": "スピッツ"}],
                             "tracks": [{"videoId": "a1", "title": "僕のギター", "duration_seconds": 199},
                                        {"videoId": None, "title": "桃", "duration_seconds": 237, "isAvailable": False}]}
    yt._web_playlist_ids = lambda pid: ["a1", "SJHjgufU_Lg"]

    class Client:
        def get_song(self, vid):
            return {"videoDetails": {"title": "桃", "lengthSeconds": "236", "musicVideoType": "MUSIC_VIDEO_TYPE_ATV"},
                    "playabilityStatus": {"status": "UNPLAYABLE",
                                          "reason": "この動画を視聴できるのは、Music Premium のメンバーのみです"}}

    yt._clients = {"ja": Client()}
    yt._limiter = type("L", (), {"wait": lambda self: None})()
    tracks = yt.album_tracks("さざなみCD", "スピッツ", "桃")
    momo = next(t for t in tracks if t["title"] == "桃")
    assert momo["videoId"] == "SJHjgufU_Lg" and momo["premiumOnly"]

    t = track("桃", ["スピッツ"], "さざなみCD", 236)
    d = decide(t, [to_candidate(x) for x in tracks])
    assert d.status == Status.AUTO and d.selected.candidate.premium_only


def test_hidden_official_track_inferred_premium():
    import threading as th

    from ytm_import.ytm import YTMusicSearch

    yt = object.__new__(YTMusicSearch)
    yt._album_cache, yt._cache_lock = {}, th.Lock()
    yt._search = lambda q, lang, f: [{"title": "YELLOW", "browseId": "MPRE2"}]
    yt._album = lambda bid: {"title": "YELLOW", "audioPlaylistId": "OLAK2", "artists": [{"name": "SCANDAL"}],
                             "tracks": [{"videoId": None, "title": "今夜はピザパーティー", "duration_seconds": 212}]}
    yt._web_playlist_ids = lambda pid: ["TL35VyElXAU"]

    class Client:
        def get_song(self, vid):
            return {"videoDetails": {"title": "今夜はピザパーティー", "lengthSeconds": "212",
                                     "musicVideoType": "MUSIC_VIDEO_TYPE_ATV"},
                    "playabilityStatus": {"status": "UNPLAYABLE", "reason": "動画を再生できません"}}

    yt._clients = {"ja": Client()}
    yt._limiter = type("L", (), {"wait": lambda self: None})()
    [t] = yt.album_tracks("YELLOW", "SCANDAL", "今夜はピザパーティー")
    assert t["videoId"] == "TL35VyElXAU" and t["premiumOnly"] and t["premiumInferred"]
    spotify = track("今夜はピザパーティー", ["SCANDAL"], "YELLOW", 212)
    d = decide(spotify, [to_candidate(t)])
    assert d.status == Status.AUTO
    assert "Premium限定（推定）" in d.ranked[0].notes
    assert decide(spotify, [to_candidate(t)], premium="review").status == Status.REVIEW
