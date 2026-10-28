from ytm_import.spotify_csv import load_tracks, split_artists

JA = '''"トラックURI","トラック名","アーティストURI","アーティスト名","アルバムURI","アルバム名","アルバムアーティストURI","アルバムアーティスト名","アルバム発売日","アルバム画像URL","ディスク番号","トラック番号","トラックの長さ（ミリ秒）","トラックプレビューURL","明示的な内容","人気度","ISRC","追加者","追加日時"
"spotify:track:4Lk4YpYTHeADqF8TgOpjyy","キラキラ","spotify:artist:6TDMbiQCWeMClsMr9ORLRK","aiko","spotify:album:x","キラキラ","","aiko","2005-08-03","","1","1","307360","","false","54","jppc00501357","","2026-10-21T18:48:42Z"
"spotify:track:aaa","星降る海","spotify:artist:a,spotify:artist:b,spotify:artist:c","Aqu3ra, 月見ヤチヨ(cv.早見沙織), 超かぐや姫！","spotify:album:y","超かぐや姫！","","","","","1","2","200000","","false","1","JPX","",""
"spotify:track:4Lk4YpYTHeADqF8TgOpjyy","キラキラ","spotify:artist:6TDMbiQCWeMClsMr9ORLRK","aiko","","","","","","","","","307360","","","","","",""
"spotify:local:foo","Local file","","Someone","","","","","","","","","1000","","","","","",""
'''

EN = '''Track URI,Track Name,Artist URI(s),Artist Name(s),Album Name,Track Duration (ms),ISRC,Added At
spotify:track:bbb,"Hello, World",spotify:artist:a,"Tyler, The Creator",Album,180000,USX,2020-01-01T00:00:00Z
'''


def test_load_japanese_headers(tmp_path):
    p = tmp_path / "liked.csv"
    p.write_text(JA, encoding="utf-8")
    tracks = load_tracks(p)
    assert [t.title for t in tracks] == ["キラキラ", "星降る海"]  # duplicate and local file skipped
    assert tracks[0].duration_ms == 307360
    assert tracks[0].isrc == "JPPC00501357"
    assert tracks[1].artists == ["Aqu3ra", "月見ヤチヨ(cv.早見沙織)", "超かぐや姫！"]


def test_load_english_headers_with_comma_in_artist(tmp_path):
    p = tmp_path / "liked.csv"
    p.write_text(EN, encoding="utf-8")
    [t] = load_tracks(p)
    assert t.artists == ["Tyler, The Creator"]
    assert t.title == "Hello, World"


def test_split_artists_without_uris():
    assert split_artists("A, B") == ["A", "B"]
