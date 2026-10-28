from conftest import cand, track

from ytm_import.matcher import decide
from ytm_import.models import Status

RINA = ["天王寺璃奈 (CV.田中ちえ美)"]


def test_picks_full_version_over_tv_size_and_off_vocal():
    t = track("ツナガルコネクト", RINA, "サイコーハート / La Bella Patria / ツナガルコネクト【天王寺璃奈盤】", 237.6)
    cands = [
        cand("tv", "ツナガルコネクト (TV size)", RINA, seconds=105),
        cand("off", "ツナガルコネクト (Off Vocal)", RINA, seconds=236),
        cand("full", "ツナガルコネクト", RINA, "サイコーハート / La Bella Patria / ツナガルコネクト【エマ・ヴェルデ盤】", 238),
    ]
    d = decide(t, cands)
    assert d.status == Status.AUTO
    assert d.selected.candidate.video_id == "full"


def test_only_karaoke_and_covers_is_not_auto():
    t = track("キラキラ", ["aiko"], "キラキラ", 307)
    cands = [
        cand("k", "キラキラ(原曲歌手:aiko)[ガイド無しカラオケ]", ["歌っちゃ王"], seconds=302),
        cand("c", "キラキラ (カバー)", ["nonomi"], seconds=342),
        cand("d", "キラキラ", ["Daoko"], seconds=248),
    ]
    d = decide(t, cands)
    assert d.status != Status.AUTO
    assert d.selected is None


def test_group_vs_member_lineup_is_not_auto():
    """START:DASH!! by μ's vs the 3-member version with near-identical length."""
    t = track("START:DASH!!", ["μ's"], "μ's Best Album Best Live! Collection Ⅱ", 257)
    members = ["高坂穂乃果(CV.新田恵海)", "南ことり(CV.内田 彩)", "園田海未(CV.三森すずこ)", "μ's"]
    d = decide(t, [cand("trio", "START:DASH!!", members, "ススメ→トゥモロウ／START:DASH!!", 254)])
    assert d.status == Status.REVIEW
    d = decide(t, [cand("trio", "START:DASH!!", members, seconds=254),
                   cand("group", "START:DASH!! [第13話挿入歌]", ["μ's"], "No brand girls / START:DASH!!", 256)])
    assert d.status == Status.AUTO
    assert d.selected.candidate.video_id == "group"


def test_remix_singer_versions():
    t = track("ワールドイズマイン - Anime ver. - CPK! Remix",
              ["ryo (supercell)", "かぐや(cv.夏吉ゆうこ)", "月見ヤチヨ(cv.早見沙織)"], "Ex-Otogibanashi", 100)
    feat = "（feat. かぐや(cv.夏吉ゆうこ)）"
    cands = [
        cand("kaguya", f"ワールドイズマイン (かぐや&月見ヤチヨ ver.) [CPK! Remix]{feat}",
             ["ryo (supercell)", "月見ヤチヨ(cv.早見沙織)"], "Ex-Otogibanashi", 226),
        cand("nagi", "ワールドイズマイン (やなぎなぎ ver.) [CPK! Remix]（feat. ChoKaguyaHime）",
             ["ryo (supercell)", "やなぎなぎ"], "CPK! Remix", 226),
        cand("anime", f"ワールドイズマイン (Anime ver.) [CPK! Remix]{feat}",
             ["ryo (supercell)", "月見ヤチヨ(cv.早見沙織)"], "Ex-Otogibanashi", 100),
        cand("live", "ワールドイズマイン-初音ミク「マジカルミライ 2021」Live-（feat. 初音ミク）",
             ["ryo (supercell)", "初音ミク"], seconds=255),
    ]
    d = decide(t, cands)
    assert d.status == Status.AUTO
    assert d.selected.candidate.video_id == "anime"
    by_id = {s.candidate.video_id: s for s in d.ranked}
    assert not by_id["kaguya"].exact  # different version label
    assert not by_id["live"].exact


def test_live_version_is_not_matched_to_studio():
    t = track("divine intervention", ["fhána"], "Outside of Melancholy", 298)
    d = decide(t, [cand("live", "divine intervention (Live)", ["fhána"], seconds=298)])
    assert d.status == Status.REVIEW
    assert any("ライブ" in n for n in d.ranked[0].notes)


def test_same_recording_on_several_albums_prefers_album_match():
    t = track("夢で逢う", ["SHISHAMO"], "SHISHAMO 5", 249)
    cands = [
        cand("best", "夢で逢う", ["SHISHAMO"], "恋を知っているすべてのあなたへ", 250),
        cand("orig", "夢で逢う", ["SHISHAMO"], "SHISHAMO 5", 250),
    ]
    d = decide(t, cands)
    assert d.status == Status.AUTO
    assert d.selected.candidate.video_id == "orig"


def test_same_title_different_lengths_without_tiebreaker_goes_to_review():
    t = track("Song", ["Band"], "Unknown Album", 200)
    cands = [cand("a", "Song", ["Band"], "Single", 198), cand("b", "Song", ["Band"], "Best", 202)]
    assert decide(t, cands).status == Status.REVIEW


def test_isrc_hit_breaks_ties_and_allows_slightly_larger_length_gap():
    t = track("Song", ["Band"], "Unknown Album", 200)
    cands = [cand("a", "Song", ["Band"], "Single", 198),
             cand("b", "Song", ["Band"], "Best", 205, isrc_hit=True)]
    d = decide(t, cands)
    assert d.status == Status.AUTO and d.selected.candidate.video_id == "b"


def test_different_unit_of_same_franchise_is_rejected():
    t = track("TOMOSHIBI", ["ヒプノシスマイク -D.R.B- (麻天狼)"], seconds=313)
    d = decide(t, [cand("x", "TOMOSHIBI", ["ヒプノシスマイク -D.R.B- (どついたれ本舗)"], seconds=313)])
    assert d.status != Status.AUTO


def test_music_video_is_not_auto():
    t = track("Song", ["Band"], seconds=200)
    d = decide(t, [cand("mv", "Song", ["Band"], seconds=201, video_type="MUSIC_VIDEO_TYPE_OMV")])
    assert d.status == Status.REVIEW


def test_english_locale_title_and_artist_aliases():
    t = track("Die For You", ["The Weeknd"], "Starboy", 260)
    c = cand("w", "ダイ・フォー・ユー", ["ザ・ウィークエンド"], "Starboy", 261)
    c.titles.append("Die For You")
    c.alt_artists = ["The Weeknd"]
    assert decide(t, [c]).status == Status.AUTO


def test_nothing_plausible_is_not_found():
    t = track("日なたの窓に憧れて", ["スピッツ"], seconds=373)
    d = decide(t, [cand("x", "流れ星", ["スピッツ"], seconds=312), cand("y", "運命の人", ["スピッツ"], seconds=310)])
    assert d.status == Status.NOT_FOUND
    assert decide(t, []).status == Status.NOT_FOUND


def test_singer_version_only_in_album_name():
    """Spotify: plain title, singer only in the album name. ISRC search surfaced another version."""
    t = track("百色リメイン", ["スタァライト九九組"], "百色リメイン (双葉&香子ver.)", 276)
    cands = [
        cand("junna", "百色リメイン", ["スタァライト九九組"], "百色リメイン (純那&ななver.)", 277, isrc_hit=True),
        cand("futaba", "百色リメイン (双葉&香子ver.)", ["スタァライト九九組"], "百色リメイン (双葉&香子ver.)", 277),
        cand("maya", "百色リメイン (真矢&クロディーヌver.)", ["スタァライト九九組"], "百色リメイン (真矢&クロディーヌver.)", 277),
    ]
    d = decide(t, cands)
    assert d.status == Status.AUTO
    assert d.selected.candidate.video_id == "futaba"


def test_similar_short_version_labels_are_different():
    t = track("Gilded Dreams", ["星見純那(CV:佐藤日向)"], "Gilded Dreams (Ale ver.)", 288)
    d = decide(t, [cand("sal", "Gilded Dreams", ["星見純那(CV:佐藤日向)"], "Gilded Dreams (Sal ver.)", 288,
                        isrc_hit=True)])
    assert d.status == Status.REVIEW
    assert "アルバム一致" not in d.ranked[0].notes


def test_deluxe_album_edition_is_not_a_version():
    t = track("Song", ["Band"], "Album (Deluxe Version)", 200)
    assert decide(t, [cand("a", "Song", ["Band"], "Album", 200)]).status == Status.AUTO


def test_version_only_in_english_locale_title():
    """The Japanese locale drops remix names; the English title keeps them."""
    t = track("ピュア (feat. 橋本絵莉子) - Kabanagu Remix β", ["PAS TASTA", "橋本絵莉子", "Kabanagu"], "ピュア REMIXES", 226)

    def remix(vid, name):
        c = cand(vid, "ピュア (feat. 橋本絵莉子)", ["PAS TASTA"], "ピュア REMIXES", 226)
        c.titles.append(f"ピュア - Pure [{name}] (feat. Eriko Hashimoto)")
        return c

    d = decide(t, [remix("other", "Yunomi Remix"), remix("kaba", "Kabanagu Remix β")])
    assert d.status == Status.AUTO
    assert d.selected.candidate.video_id == "kaba"


def test_version_marker_in_any_spelling_blocks_match():
    t = track("Song", ["Band"], seconds=100)
    c = cand("tv", "Song", ["Band"], seconds=100)
    c.titles.append("Song - Song (TV Size)")
    assert decide(t, [c]).status != Status.AUTO


def test_first_take_is_a_different_recording():
    t = track("ヒトリゴト", ["ClariS"], "Fairy Party", 233)
    d = decide(t, [cand("ft", "ヒトリゴト - From THE FIRST TAKE", ["ClariS"], "ヒトリゴト - From THE FIRST TAKE", 231)])
    assert d.status != Status.AUTO


def test_from_movie_suffix_is_ignored():
    t = track("Faith - From \"Sing\" Original Motion Picture Soundtrack", ["Stevie Wonder"], "Sing", 163)
    assert decide(t, [cand("f", "Faith", ["Stevie Wonder"], "Sing", 162)]).status == Status.AUTO


def test_unit_with_member_list_and_accents():
    t = track("STEP by STEP UP↑↑↑↑", ["fourfolium〔涼風青葉(CV:高田憂希)/滝本ひふみ(CV:山口愛)〕"], seconds=253)
    assert decide(t, [cand("a", "STEP by STEP UP↑↑↑↑", ["fourfolium"], seconds=253)]).status == Status.AUTO
    t = track("Cipher.", ["fhána"], "Cipher", 283)
    assert decide(t, [cand("b", "Cipher.", ["fhana"], "Cipher", 283)]).status == Status.AUTO


def test_combined_credit_on_youtube_music():
    t = track("動く、動く", ["チト(CV:水瀬いのり)", "ユーリ(CV:久保ユリカ)"], seconds=277)
    c = cand("a", "動く、動く", ["チト（CV：水瀬いのり）、ユーリ（CV：久保ユリカ）"], seconds=275)
    assert decide(t, [c]).status == Status.AUTO


def test_tilde_and_angle_bracket_subtitles():
    t = track("CRAZY NOISY BIZARRE TOWN - EDM ARRANGE VER.", ["THE DU"], seconds=188)
    c = cand("a", "CRAZY NOISY BIZARRE TOWN ～EDM ARRANGE VER.～", ["THE DU"], seconds=188)
    plain = cand("b", "CRAZY NOISY BIZARRE TOWN", ["THE DU"], seconds=188)
    d = decide(t, [plain, c])
    assert d.status == Status.AUTO and d.selected.candidate.video_id == "a"


def test_chorus_version_inline_suffix():
    t = track("YELL", ["いきものがかり"], seconds=357)
    c = cand("a", "YELL-合唱付-", ["いきものがかり"], seconds=360)
    c.titles.append("YELL - Yell (With Chorus)")
    assert decide(t, [c]).status != Status.AUTO


def test_misspelled_instrumental():
    t = track("あなたがここにいて抱きしめることができるなら", ["miwa"], seconds=340)
    c = cand("i", "あなたがここにいて抱きしめることができるなら 〜instrumentnal〜", ["miwa"], seconds=340)
    assert decide(t, [c]).status != Status.AUTO


def test_combined_x_credit_and_extra_guest_version():
    t = track("ようこそジャパリパークへ", ["どうぶつビスケッツ", "PPP"], "ようこそジャパリパークへ", 204)
    guest = cand("g", "ようこそジャパリパークへ（with かばん）", ["どうぶつビスケッツ", "かばん", "PPP"], "Japari Cafe", 205)
    orig = cand("o", "ようこそジャパリパークへ", ["どうぶつビスケッツ×PPP"], "ようこそジャパリパークへ", 204)
    d = decide(t, [guest, orig])
    assert d.status == Status.AUTO and d.selected.candidate.video_id == "o"


def test_feat_guest_listed_on_spotify_is_fine():
    t = track("IKIJIBIKI", ["RADWIMPS", "Taka"], "ANTI ANTI GENERATION", 231)
    c = cand("r", "IKIJIBIKI（feat. Taka）", ["RADWIMPS"], "ANTI ANTI GENERATION", 232)
    assert decide(t, [c]).status == Status.AUTO


MV = "MUSIC_VIDEO_TYPE_OMV"


def test_music_video_pick_requires_album_and_length():
    from ytm_import.matcher import music_video_pick

    t = track("誰か", ["いきものがかり"], "〇", 317)
    mv = cand("mv", "誰か", ["いきものがかり"], "〇", 318, video_type=MV)
    assert decide(t, [mv]).status == Status.REVIEW  # never automatic
    assert music_video_pick(t, [mv]).candidate.video_id == "mv"
    assert music_video_pick(t, [cand("x", "誰か", ["いきものがかり"], "別アルバム", 318, video_type=MV)]) is None
    assert music_video_pick(t, [cand("y", "誰か", ["いきものがかり"], "〇", 330, video_type=MV)]) is None
    live = cand("z", "誰か (Live)", ["いきものがかり"], "〇", 318, video_type=MV)
    assert music_video_pick(t, [live]) is None


def test_music_video_pick_skips_conflicting_videos():
    from ytm_import.matcher import music_video_pick

    t = track("誰か", ["いきものがかり"], "〇", 317)
    a = cand("a", "誰か", ["いきものがかり"], "〇", 315, video_type=MV)
    b = cand("b", "誰か", ["いきものがかり"], "〇", 320, video_type=MV)
    assert music_video_pick(t, [a, b]) is None


def test_feat_in_other_script_is_not_an_extra_guest():
    """Spotify credits 初音ミク; the right candidate says "feat. HATSUNE MIKU"."""
    t = track("ray", ["BUMP OF CHICKEN", "初音ミク"], "ray", 300)
    plain = cand("plain", "ray", ["BUMP OF CHICKEN"], "RAY", 301)
    miku = cand("miku", "ray（feat. HATSUNE MIKU）", ["BUMP OF CHICKEN"], "ray", 301, isrc_hit=True)
    d = decide(t, [plain, miku])
    assert d.status == Status.AUTO and d.selected.candidate.video_id == "miku"


def test_fuller_credit_preferred_and_combined_feat_credit():
    t = track("Pipo Password", ["TeddyLoid", "ボンジュール鈴木"], "Pipo Password", 265)
    solo = cand("solo", "Pipo Password", ["ボンジュール鈴木"], "THE BEST OF BONJOUR SUZUKI", 265)
    full = cand("full", "Pipo Password", ["TeddyLoid feat.ボンジュール鈴木"], "Pipo Password", 265, isrc_hit=True)
    d = decide(t, [solo, full])
    assert d.status == Status.AUTO and d.selected.candidate.video_id == "full"


def test_rival_with_more_singers_sends_to_review():
    t = track("Song", ["A(CV:x)", "B(CV:y)", "C(CV:z)"], "Single", 300)
    solo = cand("solo", "Song", ["A(CV:x)"], "Single", 300)
    full = cand("full", "Song", ["A(CV:x)", "B(CV:y)", "C(CV:z)"], "Single", 300, video_type="MUSIC_VIDEO_TYPE_OMV")
    d = decide(t, [solo, full])
    assert d.status == Status.REVIEW


def test_emoji_variation_selector():
    t = track("⚡️", ["ハンブレッダーズ"], "⚡️", 256)
    assert decide(t, [cand("e", "⚡", ["ハンブレッダーズ"], "⚡", 256)]).status == Status.AUTO


def test_starring_and_slash_credits():
    t = track("Find the way", ["96猫"], "パリピ孔明 VOCAL COLLECTION", 243)
    assert decide(t, [cand("a", "Find the way", ["EIKO starring 96猫"], seconds=243)]).status == Status.AUTO
    t = track("星の旅人", ["渡辺さらさ(CV.千本木彩花)/奈良田 愛(CV.花守ゆみり)"], seconds=241)
    c = cand("b", "星の旅人", ["渡辺さらさ(CV.千本木彩花)", "奈良田愛(CV.花守ゆみり)"], seconds=242)
    assert decide(t, [c]).status == Status.AUTO


def test_alias_credit_accepted_only_with_strong_evidence():
    t = track("アンババババランス", ["サカグチアミ"], "アンババババランス", 224)
    strong = cand("s", "アンババババランス", ["坂口　有望"], "アンババババランス", 224, isrc_hit=True)
    assert decide(t, [strong]).status == Status.AUTO
    no_isrc = cand("n", "アンババババランス", ["坂口　有望"], "アンババババランス", 224)
    assert decide(t, [no_isrc]).status == Status.REVIEW
    other_album = cand("o", "アンババババランス", ["坂口　有望"], "別", 224, isrc_hit=True)
    assert decide(t, [other_album]).status == Status.REVIEW


def test_soundtrack_japanese_version_album_label_ignored():
    t = track("Try Everything", ["シャキーラ"], "Zootopia (Original Motion Picture Soundtrack/Japanese Version)", 197)
    c = cand("t", "Try Everything", ["シャキーラ"], "ズートピア", 197)
    assert decide(t, [c]).status == Status.AUTO


def test_premium_policy():
    t = track("桃", ["スピッツ"], "さざなみCD", 236)
    premium = cand("p", "桃", ["スピッツ"], "さざなみCD", 236)
    premium.premium_only = True
    assert decide(t, [premium]).status == Status.AUTO
    d = decide(t, [premium], premium="review")
    assert d.status == Status.REVIEW and d.selected is None
    assert decide(t, [premium], premium="exclude").status == Status.SKIPPED
    # A non-Premium audio track still wins under any policy
    free = cand("f", "桃", ["スピッツ"], "さざなみCD", 236)
    for policy in ("include", "review", "exclude"):
        d = decide(t, [premium, free], premium=policy)
        assert d.status == Status.AUTO and d.selected.candidate.video_id in ("f", "p")
    assert decide(t, [premium, free], premium="exclude").selected.candidate.video_id == "f"
