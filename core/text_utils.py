import html
import re

_NON_WORD_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
_REPEAT_CHAR_RE = re.compile(r"(.)\1+", re.UNICODE)
_ROBLOX_JUNK_RE = re.compile(
    r"\b("
    r"roblox|audio|sound\s*effect|sfx|working|bypassed|bypass|loud|clean|"
    r"reupload|music\s*id|boombox|radio|free\s*audio|full\s*song|"
    r"mp3|ogg|wav|flac|m4a"
    r")\b",
    re.IGNORECASE,
)
_BRACKET_NOISE_RE = re.compile(
    r"[\[\(\{]\s*(working|bypassed|loud|clean|audio|id|new|fixed|202[0-9]|hd|hq)\s*[\]\)\}]",
    re.IGNORECASE,
)

_CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

# English phonetic chunks so "CUPSIZE" -> "капсайз", "shadowraze" -> "шадоурейз", etc.
_EN_PHONETIC_CHUNKS = [
    ("cupsize", "капсайз"),
    ("shadow", "шадоу"),
    ("raze", "рейз"),
    ("phonk", "фонк"),
    ("night", "найт"),
    ("light", "лайт"),
    ("fight", "файт"),
    ("right", "райт"),
    ("sight", "сайт"),
    ("size", "сайз"),
    ("time", "тайм"),
    ("life", "лайф"),
    ("line", "лайн"),
    ("mine", "майн"),
    ("fine", "файн"),
    ("fire", "файр"),
    ("desire", "дизайр"),
    ("mind", "майнд"),
    ("blind", "блайнд"),
    ("eyes", "айз"),
    ("ice", "айс"),
    ("blade", "блейд"),
    ("fade", "фейд"),
    ("play", "плей"),
    ("stay", "стей"),
    ("away", "эвей"),
    ("day", "дей"),
    ("way", "вей"),
    ("heart", "харт"),
    ("blood", "блад"),
    ("love", "лав"),
    ("cup", "кап"),
    ("cut", "кат"),
    ("sun", "сан"),
    ("run", "ран"),
    ("fun", "фан"),
    ("gun", "ган"),
    ("dead", "дед"),
    ("head", "хед"),
    ("ghost", "гост"),
    ("girl", "герл"),
    ("world", "ворлд"),
    ("black", "блэк"),
    ("white", "вайт"),
    ("high", "хай"),
    ("sky", "скай"),
    ("cry", "край"),
    ("fly", "флай"),
    ("die", "дай"),
    ("slowed", "словед"),
    ("sped", "спед"),
]

_LAT_CHUNKS_TO_CYR = [
    ("shch", "щ"), ("sch", "щ"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"),
    ("ch", "ч"), ("sh", "ш"), ("yu", "ю"), ("ya", "я"), ("yo", "е"),
    ("ph", "ф"), ("ck", "к"), ("qu", "кв"), ("th", "т"), ("oo", "у"),
    ("ee", "и"), ("ea", "и"),
]

_LAT_TO_CYR = {
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г",
    "h": "х", "i": "и", "j": "дж", "k": "к", "l": "л", "m": "м", "n": "н",
    "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т", "u": "у",
    "v": "в", "w": "в", "x": "кс", "y": "й", "z": "з",
}

# Bidirectional number <-> word aliases ("17" <-> "семнадцать", "3" <-> "три", etc.)
_NUMBER_ALIASES: list[tuple[str, list[str]]] = [
    ("0", ["ноль", "нуль", "zero"]),
    ("1", ["один", "одна", "первый", "one"]),
    ("2", ["два", "две", "второй", "two"]),
    ("3", ["три", "третий", "three"]),
    ("4", ["четыре", "four"]),
    ("5", ["пять", "five"]),
    ("6", ["шесть", "six"]),
    ("7", ["семь", "seven"]),
    ("8", ["восемь", "eight"]),
    ("9", ["девять", "nine"]),
    ("10", ["десять", "ten"]),
    ("11", ["одиннадцать", "eleven"]),
    ("12", ["двенадцать", "twelve"]),
    ("13", ["тринадцать", "thirteen"]),
    ("14", ["четырнадцать", "fourteen"]),
    ("15", ["пятнадцать", "fifteen"]),
    ("16", ["шестнадцать", "sixteen"]),
    ("17", ["семнадцать", "семнацать", "seventeen"]),
    ("18", ["восемнадцать", "eighteen"]),
    ("19", ["девятнадцать", "nineteen"]),
    ("20", ["двадцать", "twenty"]),
    ("24", ["двадцать четыре"]),
    ("30", ["тридцать", "thirty"]),
    ("40", ["сорок", "forty"]),
    ("50", ["пятьдесят", "fifty"]),
    ("90", ["девяносто"]),
    ("100", ["сто", "hundred"]),
    ("200", ["двести"]),
    ("1000", ["тысяча", "thousand"]),
]

_WORD_TO_NUMBER_EQUIV: dict[str, list[str]] = {}
for _num, _words in _NUMBER_ALIASES:
    _group = [_num, *_words]
    for _item in _group:
        if " " not in _item:
            _WORD_TO_NUMBER_EQUIV[_item] = [x for x in _group if x != _item and " " not in x]


def normalize_basic(text: str) -> str:
    """Unicode-safe lowercase + ё->е + punctuation strip (SQLite LOWER is ASCII-only)."""
    if not text:
        return ""
    lowered = text.lower().replace("ё", "е")
    cleaned = _NON_WORD_RE.sub(" ", lowered)
    return _SPACE_RE.sub(" ", cleaned).strip()


def phonetic_skeleton(token: str) -> str:
    """Reduces common Russian/translit spelling mistakes so 'капсайс'/'семнацать' match."""
    t = normalize_basic(token)
    if not t:
        return ""
    t = (
        t.replace("ться", "ца")
        .replace("тся", "ца")
        .replace("дц", "ц")
        .replace("тц", "ц")
        .replace("тс", "ц")
        .replace("щ", "ш")
        .replace("э", "е")
    )
    if t.endswith("з"):
        t = t[:-1] + "с"
    t = _REPEAT_CHAR_RE.sub(r"\1", t)
    return t


def expand_number_tokens(tokens: list[str]) -> list[str]:
    out = list(tokens)
    for tok in tokens:
        for eq in _WORD_TO_NUMBER_EQUIV.get(tok, []):
            out.append(eq)
    return out


def translit_variants(text: str) -> str:
    """Builds extra transliterated & phonetic tokens so 'капсайз 17' matches 'CUPSIZE - семнадцать ножевых'."""
    norm = normalize_basic(text)
    if not norm:
        return ""

    base_tokens = expand_number_tokens(norm.split())
    expanded_norm = " ".join(base_tokens)

    # Cyrillic -> Latin
    lat_str = "".join(_CYR_TO_LAT.get(ch, ch) for ch in expanded_norm)

    # Latin -> Literal Cyrillic
    cyr_literal = expanded_norm
    for src, dst in _LAT_CHUNKS_TO_CYR:
        cyr_literal = cyr_literal.replace(src, dst)
    cyr_literal = "".join(_LAT_TO_CYR.get(ch, ch) for ch in cyr_literal)

    # Latin -> English Phonetic Cyrillic ("cupsize" -> "капсайз")
    cyr_phonetic = expanded_norm
    for src, dst in _EN_PHONETIC_CHUNKS:
        cyr_phonetic = cyr_phonetic.replace(src, dst)
    for src, dst in _LAT_CHUNKS_TO_CYR:
        cyr_phonetic = cyr_phonetic.replace(src, dst)
    cyr_phonetic = "".join(_LAT_TO_CYR.get(ch, ch) for ch in cyr_phonetic)

    combined_tokens = f"{expanded_norm} {lat_str} {cyr_literal} {cyr_phonetic}".split()
    skeletons = [phonetic_skeleton(tok) for tok in combined_tokens if len(tok) >= 3]
    unique_tokens = list(dict.fromkeys([*combined_tokens, *(s for s in skeletons if s)]))
    return " ".join(unique_tokens)


def clean_roblox_title(raw_name: str) -> str:
    """Strips typical Roblox uploader noise while preserving real track/artist words."""
    if not raw_name:
        return ""
    text = raw_name.replace("_", " ")
    text = _BRACKET_NOISE_RE.sub(" ", text)
    text = _ROBLOX_JUNK_RE.sub(" ", text)
    text = re.sub(r"\s*[-–—|]+\s*", " - ", text)
    text = _SPACE_RE.sub(" ", text).strip(" -–—|._")
    return text


def split_artist_title(raw_text: str, fallback_creator: str = "") -> tuple[str, str]:
    """Splits 'Artist - Song Title' cleanly if separator exists."""
    cleaned = clean_roblox_title(raw_text)
    for sep in (" - ", " — ", " – ", " | "):
        if sep in cleaned:
            parts = [p.strip() for p in cleaned.split(sep, 1)]
            if parts[0] and parts[1]:
                return parts[0], parts[1]
    return fallback_creator.strip(), cleaned or raw_text.strip()


def build_song_search_index(artist: str, title: str, extra: str = "") -> str:
    raw = f"{artist} {title} {extra}".strip()
    return translit_variants(raw)


def extract_search_tokens(query: str) -> list[str]:
    norm = normalize_basic(query)
    tokens = [t for t in norm.split() if len(t) >= 1]
    return tokens[:8]


def format_song_display(artist: str, title: str) -> str:
    artist_c = (artist or "").strip()
    title_c = (title or "").strip()
    if artist_c and title_c:
        if artist_c.lower() in title_c.lower():
            return title_c
        return f"{artist_c} - {title_c}"
    return title_c or artist_c or "Неизвестный трек"


def esc(text: str) -> str:
    return html.escape(str(text or ""))
