import json
import threading

import pytest
from conftest import FakeSearch, result, track

from ytm_import import main
from ytm_import.models import Status
from ytm_import.ops import apply_decisions, audit, review_items, status_summary
from ytm_import.runner import match_all


def prepared(store):
    store.import_tracks([
        track("START:DASH!!", ["μ's"], seconds=257, uri="spotify:track:1"),
        track("Song", ["Band"], seconds=200, uri="spotify:track:2"),
    ])
    members = ["高坂穂乃果(CV.新田恵海)", "南ことり(CV.内田 彩)", "園田海未(CV.三森すずこ)", "μ's"]
    match_all(store, FakeSearch({
        ("START:DASH!! μ's", "ja"): [result("trio", "START:DASH!!", members, seconds=254),
                                     result("tv", "START:DASH!! (TVサイズ)", ["μ's"], seconds=185)],
        ("Song Band", "ja"): [result("song", "Song", ["Band"], seconds=200)],
    }), threading.Event())
    return store


def test_review_items_lists_ranked_candidates(store):
    [item] = review_items(prepared(store), (Status.REVIEW, Status.NOT_FOUND), 10)
    assert item["uri"] == "spotify:track:1"
    assert [c["video_id"] for c in item["candidates"]] == ["trio", "tv"]
    assert item["candidates"][0]["n"] == 1 and not item["candidates"][0]["exact"]


def test_apply_decisions_is_all_or_nothing(store):
    prepared(store)
    with pytest.raises(ValueError):
        apply_decisions(store, [{"uri": "spotify:track:1", "action": "pick", "n": 1},
                                {"uri": "spotify:track:1", "action": "pick", "video_id": "unknown"}])
    assert store.get("spotify:track:1").status == Status.REVIEW
    apply_decisions(store, [{"uri": "spotify:track:1", "action": "pick", "n": 1}])
    assert store.get("spotify:track:1").selected_video_id == "trio"
    apply_decisions(store, [{"uri": "spotify:track:1", "action": "reset"}])
    assert store.get("spotify:track:1").status == Status.REVIEW


def test_status_and_audit(store):
    prepared(store)
    s = status_summary(store)
    assert s["to_like"] == 1 and s["undecided"] == 1
    assert "near_miss:歌唱者の構成が違う" in audit(store)


def test_cli_apply_is_dry_run_by_default(store, tmp_path, capsys, monkeypatch):
    prepared(store)
    path = str(store.path)

    def boom(*a, **k):
        raise AssertionError("must not touch the account in dry-run")

    monkeypatch.setattr("ytm_import.ytm.Liker", boom)
    assert main(["--data", path, "apply"]) == 0
    out = capsys.readouterr().out
    assert '"to_like": 1' in out and "dry-run" in out
    assert main(["--data", path, "accept-mv"]) == 0


def test_cli_decide_from_json(store, tmp_path):
    prepared(store)
    f = tmp_path / "d.json"
    f.write_text(json.dumps([{"uri": "spotify:track:1", "action": "skip"}]), encoding="utf-8")
    assert main(["--data", str(store.path), "decide", "--file", str(f)]) == 0
    assert store.get("spotify:track:1").status == Status.SKIPPED
    assert main(["--data", str(store.path), "decide", "spotify:track:1", "--pick", "nope"]) == 1


def test_premium_policy_switch_is_reversible_and_keeps_user_skips(store, capsys):
    store.import_tracks([track("桃", ["スピッツ"], "さざなみCD", 236, uri="spotify:track:p"),
                         track("Song", ["Band"], seconds=200, uri="spotify:track:s")])
    b = FakeSearch({("Song Band", "ja"): [result("song", "Song", ["Band"], seconds=200)]})

    class WithPremium(FakeSearch):
        def album_tracks(self, album, artist, title=""):
            return [{**result("momo", "桃", ["スピッツ"], "さざなみCD", 236), "premiumOnly": True}]

    match_all(store, WithPremium(b.responses), threading.Event())
    assert store.get("spotify:track:p").status == Status.AUTO
    path = str(store.path)
    store.set_status("spotify:track:s", Status.SKIPPED, None, "自分でスキップ")

    assert main(["--data", path, "config", "premium", "exclude"]) == 0
    assert store.get("spotify:track:p").status == Status.SKIPPED
    assert status_summary(store)["skipped_by_premium_policy"] == 1
    assert main(["--data", path, "config", "premium", "review"]) == 0
    assert store.get("spotify:track:p").status == Status.REVIEW
    assert main(["--data", path, "config", "premium", "include"]) == 0
    assert store.get("spotify:track:p").status == Status.AUTO
    assert store.get("spotify:track:s").status == Status.SKIPPED  # the user's own skip is untouched
