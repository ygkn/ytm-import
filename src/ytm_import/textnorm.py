"""Text normalization and title/artist analysis used by the matcher."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from rapidfuzz import fuzz

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_TILDES = dict.fromkeys(map(ord, "〜～"), "~")
_QUOTES = {ord("’"): "'", ord("‘"): "'", ord("“"): '"', ord("”"): '"', ord("`"): "'"}


def normalize(text: str) -> str:
    """NFKC + casefold + unify look-alike punctuation + collapse whitespace."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.translate(_DASHES).translate(_TILDES).translate(_QUOTES)
    text = text.replace("　", " ").replace("\ufe0f", "").replace("\ufe0e", "").casefold()  # emoji variation selectors
    return re.sub(r"\s+", " ", text).strip()


_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)


_KANA_MARKS = {"\u3099", "\u309a"}


def strip_accents(text: str) -> str:
    """"fhána" -> "fhana", keeping Japanese dakuten/handakuten intact."""
    decomposed = unicodedata.normalize("NFD", text)
    kept = "".join(ch for ch in decomposed if not unicodedata.combining(ch) or ch in _KANA_MARKS)
    return unicodedata.normalize("NFC", kept)


def compact(text: str) -> str:
    """Normalized text with whitespace, punctuation and accents removed (for fuzzy keys)."""
    return _PUNCT_RE.sub("", strip_accents(normalize(text)))


def similarity(a: str, b: str) -> float:
    """0..1 similarity of two strings after compaction."""
    ca, cb = compact(a), compact(b)
    if not ca or not cb:
        # Titles made only of symbols (e.g. "!!!") – fall back to normalized text.
        ca, cb = normalize(a), normalize(b)
        if not ca or not cb:
            return 0.0
    return fuzz.ratio(ca, cb) / 100


# --- feat. handling ---------------------------------------------------------

_FEAT_WORD = r"(?:feat\.?|ft\.?|featuring)"
_FEAT_BRACKET_RE = re.compile(
    rf"[(\[]\s*(?:{_FEAT_WORD}|with)\s+((?:[^()\[\]]|\([^()]*\))*)[)\]]", re.IGNORECASE
)
_FEAT_TAIL_RE = re.compile(rf"\s+{_FEAT_WORD}\s+(.+)$", re.IGNORECASE)


def split_artist_list(text: str) -> list[str]:
    parts = re.split(r"\s*(?:,|、|&|＆| and | x | × |/)\s*", text)
    return [p.strip() for p in parts if p.strip()]


def strip_feat(title: str) -> tuple[str, list[str]]:
    """Remove "(feat. X)" parts from a normalized title and return the featured names."""
    names: list[str] = []

    def take(m: re.Match[str]) -> str:
        names.extend(split_artist_list(m.group(1)))
        return " "

    title = _FEAT_BRACKET_RE.sub(take, title)
    title = _FEAT_TAIL_RE.sub(take, title)
    return re.sub(r"\s+", " ", title).strip(), names


# --- version tags -----------------------------------------------------------

# Each tag marks a *different recording or edit* of a song. If the Spotify
# title and the YouTube Music title disagree on any of these, they are not
# treated as the same track.
VERSION_TAGS: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern, re.IGNORECASE)
    for name, pattern in {
        "live": r"\blive\b|ライブ|ライヴ|ﾗｲﾌﾞ|in concert|\blive at\b",
        # One-take / session re-recordings (often titled "Song - From THE FIRST TAKE")
        "session": r"first take|home take|\bone take\b|\bsession\b|studio live|unplugged|\bmtv\b",
        "remix": r"remix|\bmix\b|\brmx\b|リミックス|\brework\b|\bbootleg\b",
        "instrumental": r"instrument\w*|\binst\b|off\s?vocal|オフボーカル|karaoke|カラオケ|インスト|backing track|without vocals?|ガイド(無し|なし|あり|有り)",
        "tv_size": r"\btv\s?(size|ver|edit|version|サイズ)|tvサイズ|tv-size|movie\s?size|game\s?size|short\s?(ver|version|size|edit)|ショート(ver|バージョン)?",
        "acoustic": r"acoustic|アコースティック|unplugged|弾き語り|アンプラグド",
        "piano_box": r"piano\s?(ver|version|arrange)|ピアノ(ver|バージョン|アレンジ)|オルゴール|music\s?box|orgel|8\s?bit|lo-?fi",
        "cover": r"\bcover(ed)?\b|カバー|originally performed|原曲歌手|歌ってみた|\btribute\b|self\s?cover",
        "ringtone": r"着信音|着メロ|ringtone",
        "a_cappella": r"a\s?cappella|acapella|アカペラ",
        "demo": r"\bdemo\b|デモ",
        "edit": r"\bradio edit\b|\bedit\b|\bextended\b",
        "speed": r"sped up|speed up|slowed|nightcore|reverb",
        "orchestra": r"orchestra|オーケストラ|symphonic|シンフォニック",
        "chorus": r"合唱|with chorus|choir|クワイア",
        "solo": r"\bsolo\b|ソロ",
        "language": r"english\s?(ver|version)|eng\s?ver|japanese\s?(ver|version)|日本語(ver|バージョン)|英語(ver|バージョン)|korean\s?ver|chinese\s?ver",
    }.items()
}

# Fragments like these describe the same recording and are ignored.
_BENIGN_RE = re.compile(
    r"remaster(ed)?|リマスター|original\s?(mix|version|ver)|^\d{4}$|^from\b|より$|主題歌|挿入歌|オープニング|エンディング|テーマ|op\b|ed\b|bonus track"
    # album editions
    r"|deluxe|expanded|anniversary|edition|special|limited|standard|通常盤|初回|限定|盤$",
    re.IGNORECASE,
)

_BRACKET_RE = re.compile(r"[(\[【〔<]([^()\[\]【】〔〕<>]*)[)\]】〕>]|~([^~]+)~")
_VERSION_WORD_RE = re.compile(r"\bver\b|\bver\.|version|バージョン|ヴァージョン")


@dataclass
class TitleInfo:
    raw: str
    base: str  # title without brackets / dash suffixes / feat.
    full: str  # normalized title without feat.
    tags: frozenset[str]
    labels: list[str] = field(default_factory=list)  # version-bearing fragments
    feat: list[str] = field(default_factory=list)


def _fragment_tags(text: str) -> set[str]:
    return {name for name, rx in VERSION_TAGS.items() if rx.search(text)}


def analyze_title(title: str) -> TitleInfo:
    norm = normalize(title)
    full, feat = strip_feat(norm)

    fragments: list[str] = []

    def take(m: re.Match[str]) -> str:
        fragments.append((m.group(1) or m.group(2) or "").strip())
        return " "

    base = full
    # Repeat to peel nested brackets from the inside out.
    for _ in range(3):
        new = _BRACKET_RE.sub(take, base)
        if new == base:
            break
        base = new
    # Inline style: "YELL-合唱付-", "Song-Live 2021-"
    m = re.match(r"^(.+?\S)-([^-\s][^-]*)-$", base.strip())
    if m:
        fragments.append(m.group(2).strip())
        base = m.group(1)
    # Spotify style: "Song - Live at Budokan", "Song - 2024 ver. - Remix"
    if " - " in base:
        head, *tails = base.split(" - ")
        fragments.extend(t.strip() for t in tails)
        base = head
    base = re.sub(r"\s+", " ", base).strip(" -~")
    if not base:  # title entirely in brackets, e.g. "[Alexandros]"
        base = full

    labels = []
    for frag in fragments:
        if not frag or _BENIGN_RE.search(frag) and not _fragment_tags(frag):
            continue
        if _fragment_tags(frag) or _VERSION_WORD_RE.search(frag):
            labels.append(frag)

    tags = _fragment_tags(full)
    if any(_VERSION_WORD_RE.search(label) for label in labels):
        tags.add("version")
    return TitleInfo(raw=title, base=base, full=full, tags=frozenset(tags), labels=labels, feat=feat)


def label_tags(label: str) -> set[str]:
    tags = _fragment_tags(label)
    if _VERSION_WORD_RE.search(label):
        tags.add("version")
    return tags


_VERSION_STRIP_RE = re.compile(r"\bver\b\.?|\bversion\b|バージョン|ヴァージョン", re.IGNORECASE)


def _label_core(label: str) -> str:
    return compact(_VERSION_STRIP_RE.sub(" ", normalize(label)))


def label_equal(a: str, b: str) -> bool:
    """Strict comparison of version labels.

    The distinguishing part is often tiny ("Ale ver." vs "Sal ver.",
    "純那&ななver." vs "双葉&香子ver."), so the "ver." word is removed and the
    rest must be equal, or nearly equal when it is long enough to allow for
    spelling differences.
    """
    x, y = _label_core(a), _label_core(b)
    if x == y:
        return True
    return min(len(x), len(y)) >= 6 and fuzz.ratio(x, y) >= 90


def labels_match(a: list[str], b: list[str]) -> bool:
    """Every version label on each side has an equal label on the other side."""

    def covered(xs: list[str], ys: list[str]) -> bool:
        return all(any(label_equal(x, y) for y in ys) for x in xs)

    return covered(a, b) and covered(b, a)


def merge_labels(*groups: list[str]) -> list[str]:
    out: list[str] = []
    for group in groups:
        for label in group:
            if not any(label_equal(label, o) for o in out):
                out.append(label)
    return out


# --- artists ----------------------------------------------------------------

_PAREN_RE = re.compile(r"^(.*?)\s*[(\[〔【]((?:[^()\[\]〔〕【】]|\([^()]*\))+)[)\]〕】]\s*$")
_STARRING_RE = re.compile(r"^(.*?)\s+starring\s+(.+)$", re.IGNORECASE)  # "EIKO starring 96猫"
_CV_INLINE_RE = re.compile(r"^(.*?)\s+(?:cv|c\.v)\s*[.:：]?\s*(.+)$", re.IGNORECASE)
_CREDIT_PREFIX_RE = re.compile(r"^(?:cv|c\.v|vo|voice)\s*[.:：]?\s*", re.IGNORECASE)
_INNER_SPLIT_RE = re.compile(r"\s*(?:\bstarring\b|\bfeat\.?|\bcv\b\s*[.:]?|,|、|&|×|/)\s*", re.IGNORECASE)


@dataclass(frozen=True)
class ArtistName:
    """An artist credit split into "outer (inner)".

    "天王寺璃奈 (CV.田中ちえ美)"          -> outer 天王寺璃奈, inner 田中ちえ美
    "ryo (supercell)"                     -> outer ryo, inner supercell
    "AZALEA（久遠七海 starring Lezel）" -> outer azalea, inner 久遠七海 starring lezel
    """

    full: str
    outer: str
    inner: str
    inner_parts: frozenset[str]


def parse_artist(name: str) -> ArtistName:
    norm = normalize(name)
    m = _PAREN_RE.match(norm) or _STARRING_RE.match(norm) or _CV_INLINE_RE.match(norm)
    if not m or not m.group(1).strip():
        return ArtistName(compact(norm), compact(norm), "", frozenset())
    outer, inner = m.group(1).strip(), _CREDIT_PREFIX_RE.sub("", m.group(2).strip())
    parts = {compact(p) for p in _INNER_SPLIT_RE.split(inner) if compact(p)}
    return ArtistName(compact(norm), compact(outer), compact(inner), frozenset(parts))


def _close(x: str, y: str, threshold: float) -> bool:
    if not x or not y:
        return False
    if x == y:
        return True
    return min(len(x), len(y)) >= 3 and fuzz.ratio(x, y) / 100 >= threshold


def artists_match(a: str, b: str, threshold: float = 0.88) -> bool:
    """Whether two credits refer to the same performer.

    When *both* credits carry a parenthesised part, both parts must agree:
    "ヒプノシスマイク -D.R.B- (麻天狼)" is not "ヒプノシスマイク -D.R.B- (どついたれ本舗)",
    and the same voice actor singing as a different character is a different
    credit. When only one side has it, the plain name may match either part
    ("ryo" ~ "ryo (supercell)", "Lezel" ~ "AZALEA（久遠七海 starring Lezel）").
    """
    x, y = parse_artist(a), parse_artist(b)
    if _close(x.full, y.full, threshold):
        return True
    if x.inner and y.inner:
        return _close(x.outer, y.outer, threshold) and _close(x.inner, y.inner, threshold)
    plain, rich = (x, y) if not x.inner else (y, x)
    if not rich.inner:
        return False
    return any(_close(plain.full, v, threshold) for v in (rich.outer, rich.inner, *rich.inner_parts))


@dataclass
class ArtistOverlap:
    recall: float  # share of Spotify artists found on the candidate
    precision: float  # share of candidate artists found on Spotify
    primary: bool  # Spotify's first artist is on the candidate

    @property
    def f1(self) -> float:
        if self.recall + self.precision == 0:
            return 0.0
        return 2 * self.recall * self.precision / (self.recall + self.precision)


_CREDIT_SPLIT_RE = re.compile(
    r"\s*(?:、|,|×|&| x |\s+feat\.?\s*|(?<=\S)feat\.\s*)\s*(?![^()（）]*[)）])", re.IGNORECASE
)


def expand_credit(spellings: list[str]) -> list[str]:
    """YouTube Music sometimes lumps several performers into one credit:
    "チト（CV：水瀬いのり）、ユーリ（CV：久保ユリカ）". Also try each part
    (commas inside parentheses are left alone)."""
    out = list(spellings)
    for sp in spellings:
        parts = [p for p in _CREDIT_SPLIT_RE.split(normalize(sp)) if p]
        if len(parts) > 1:
            out.extend(parts)
    return out


def artist_overlap(spotify: list[str], candidate: list[list[str]]) -> ArtistOverlap:
    """Compare Spotify artist names with candidate credits (each a list of spellings)."""
    if not spotify or not candidate:
        return ArtistOverlap(0.0, 0.0, False)

    def hit(name: str, spellings: list[str]) -> bool:
        return any(artists_match(name, s) for s in expand_credit(spellings))

    def names(name: str) -> list[str]:
        """A Spotify credit and, if it lumps several people ("A/B"), each of them."""
        parts = [p for p in re.split(r"\s*/\s*", name) if p]
        return [name, *parts] if len(parts) > 1 else [name]

    def spotify_hit(name: str) -> bool:
        if any(hit(name, c) for c in candidate):
            return True
        parts = names(name)[1:]
        return bool(parts) and all(any(hit(p, c) for c in candidate) for p in parts)

    hit_s = [spotify_hit(s) for s in spotify]
    hit_c = [any(hit(n, c) for s in spotify for n in names(s)) for c in candidate]
    return ArtistOverlap(sum(hit_s) / len(hit_s), sum(hit_c) / len(hit_c), hit_s[0])
