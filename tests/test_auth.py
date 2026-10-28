from ytm_import.ytm import clean_headers

RAW = """POST /youtubei/v1/browse?prettyPrint=false HTTP/1.1
:authority: music.youtube.com
Host: music.youtube.com
accept: */*
Content-Encoding: gzip
content-length: 1234
cookie: SID=abc; HSID=def
x-goog-authuser: 0
"""


def test_clean_headers_keeps_only_replayable_headers():
    assert clean_headers(RAW).splitlines() == [
        "accept: */*", "cookie: SID=abc; HSID=def", "x-goog-authuser: 0", "authorization: SAPISIDHASH placeholder"]


def test_clean_headers_keeps_colons_in_values():
    assert clean_headers("referer: https://music.youtube.com/") == "referer: https://music.youtube.com/"


SEPARATE_LINES = """:authority
music.youtube.com
:method
POST
:path
/youtubei/v1/browse?prettyPrint=false
:scheme
https
accept
*/*
content-encoding
gzip
cookie
SID=abc; HSID=def
origin
https://music.youtube.com
x-goog-authuser
0
"""


def test_clean_headers_chrome_separate_lines_format():
    assert clean_headers(SEPARATE_LINES).splitlines() == [
        "accept: */*", "cookie: SID=abc; HSID=def", "origin: https://music.youtube.com", "x-goog-authuser: 0",
        "authorization: SAPISIDHASH placeholder"]


def test_clean_headers_bare_cookie_value_with_authuser():
    assert clean_headers("YSC=x; SAPISID=abc; SID=def\n", authuser="1").splitlines() == [
        "cookie: YSC=x; SAPISID=abc; SID=def", "x-goog-authuser: 1", "authorization: SAPISIDHASH placeholder"]


def test_clean_headers_authuser_overrides_copied_value():
    assert "x-goog-authuser: 2" in clean_headers(RAW, authuser="2").splitlines()


def test_merge_cookie_header_replaces_rotated_values():
    from ytm_import.ytm import merge_cookie_header

    header, changed = merge_cookie_header("SID=a; __Secure-1PSIDTS=old; SAPISID=s",
                                          {"__Secure-1PSIDTS": "new", "SAPISID": "s"})
    assert header == "SID=a; __Secure-1PSIDTS=new; SAPISID=s"
    assert changed == ["__Secure-1PSIDTS"]
