import pytest

from ytm_import.textnorm import analyze_title, artist_overlap, artists_match, labels_match, normalize, similarity


def test_normalize_fullwidth_and_dashes():
    assert normalize("ＳＴＡＲＴ：ＤＡＳＨ！！　〜x〜") == "start:dash!! ~x~"
    assert similarity("タイム・トラベル", "タイム･トラベル") == 1.0


@pytest.mark.parametrize(
    "title, base, tags",
    [
        ("ツナガルコネクト (TV size)", "ツナガルコネクト", {"tv_size"}),
        ("ツナガルコネクト (Off Vocal)", "ツナガルコネクト", {"instrumental"}),
        ("START:DASH!! (Bitter-Sweet Mix)", "start:dash!!", {"remix"}),
        ("START:DASH!! [第13話挿入歌]", "start:dash!!", set()),
        ("キラキラ(原曲歌手:aiko)[ガイド無しカラオケ]", "キラキラ", {"cover", "instrumental"}),
        ("サバシスター's THEME - 2024 ver.", "サバシスター's theme", {"version"}),
        ("Song - Live at Budokan", "song", {"live"}),
        ("Song (Remastered 2009)", "song", set()),
        ("[Alexandros]", "[alexandros]", set()),
    ],
)
def test_analyze_title(title, base, tags):
    info = analyze_title(title)
    assert info.base == base
    assert set(info.tags) == tags


def test_feat_with_nested_parentheses():
    info = analyze_title("ワールドイズマイン (Anime ver.) [CPK! Remix]（feat. かぐや(cv.夏吉ゆうこ)）")
    assert info.feat == ["かぐや(cv.夏吉ゆうこ)"]
    assert info.labels == ["anime ver.", "cpk! remix"]


def test_version_labels_distinguish_singers():
    spotify = analyze_title("ワールドイズマイン - かぐや&月見ヤチヨ ver. - CPK! Remix")
    same = analyze_title("ワールドイズマイン (かぐや&月見ヤチヨ ver.) [CPK! Remix]")
    other = analyze_title("ワールドイズマイン (やなぎなぎ ver.) [CPK! Remix]")
    assert labels_match(spotify.labels, same.labels)
    assert not labels_match(spotify.labels, other.labels)


@pytest.mark.parametrize(
    "a, b, expected",
    [
        ("天王寺璃奈 (CV.田中ちえ美)", "天王寺璃奈（CV：田中ちえ美）", True),
        ("ryo (supercell)", "ryo(supercell)", True),
        ("ryo", "ryo (supercell)", True),
        ("Lezel", "AZALEA（久遠七海 starring Lezel）", True),
        # Same franchise, different unit
        ("ヒプノシスマイク -D.R.B- (麻天狼)", "ヒプノシスマイク -D.R.B- (どついたれ本舗)", False),
        # Same character name, different voice actor
        ("かぐや(cv.夏吉ゆうこ)", "かぐや(cv.早見沙織)", False),
        ("μ's", "高坂穂乃果(CV.新田恵海)", False),
        ("aiko", "Daoko", False),
    ],
)
def test_artists_match(a, b, expected):
    assert artists_match(a, b) is expected


def test_artist_overlap_penalizes_extra_members():
    o = artist_overlap(["μ's"], [["高坂穂乃果(CV.新田恵海)"], ["南ことり(CV.内田 彩)"], ["園田海未(CV.三森すずこ)"], ["μ's"]])
    assert o.recall == 1.0 and o.precision == 0.25
    assert o.f1 < 0.5


def test_artist_overlap_uses_alternate_spellings():
    o = artist_overlap(["The Weeknd"], [["ザ・ウィークエンド", "The Weeknd"]])
    assert o.f1 == 1.0
