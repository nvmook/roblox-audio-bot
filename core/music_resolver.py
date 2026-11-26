"""Resolves disguised or messy Roblox audio metadata to canonical Artist & Track names
WITHOUT overwriting real Roblox audio duration and WITHOUT hallucinating random songs
when the Roblox upload only has an artist + part number (e.g. 'cupsize 1').
"""

import re
from typing import Any
import httpx
from core.matcher import token_similarity
from core.text_utils import (
    clean_roblox_title,
    expand_number_tokens,
    normalize_basic,
    split_artist_title,
    translit_variants,
)

_SPED_UP_RE = re.compile(
    r"\b(sped\s*up|speed\s*up|nightcore|ускорен\w*|ускор|спидап|1\.[1-6]x)\b",
    re.IGNORECASE,
)
_SLOWED_RE = re.compile(
    r"\b(slowed|reverb|замедлен\w*|словед)\b",
    re.IGNORECASE,
)
# Matches upload part markers like "cupsize 1", "cupsize #2", "shadowraze pt 1", "трек ч.2"
_PART_SUFFIX_RE = re.compile(
    r"(?:\s+[-–—|]?\s*|\s+)(?:#\s*\d{1,2}|(?:pt|part|ч|часть|ver|v)\.?\s*\d{1,2}|\(\d{1,2}\))$",
    re.IGNORECASE,
)
_BARE_TRAILING_INDEX_RE = re.compile(r"^([a-zа-яё0-9_]{2,20})\s+([1-9])$", re.IGNORECASE)


def detect_version_tag(raw_name: str) -> str:
    if not raw_name:
        return ""
    if _SPED_UP_RE.search(raw_name):
        return "(sped up)"
    if _SLOWED_RE.search(raw_name):
        return "(slowed)"
    return ""


def append_version_tag(title: str, tag: str) -> str:
    if not tag or not title:
        return title
    low = title.lower()
    if "sped up" in low or "nightcore" in low or "slowed" in low or "ускор" in low:
        return title
    return f"{title} {tag}"


def _strip_version_and_part_markers(text: str) -> tuple[str, str]:
    """Returns (core_query, part_label_if_any)."""
    s = _SPED_UP_RE.sub(" ", text)
    s = _SLOWED_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" -–—|()[]{}")

    part_label = ""
    m_part = _PART_SUFFIX_RE.search(s)
    if m_part:
        part_label = m_part.group(0).strip(" -–—|")
        s = s[: m_part.start()].strip(" -–—|")
    else:
        # Handle "cupsize 1", "кишлак 2" -> single artist word + 1..9 upload index
        m_bare = _BARE_TRAILING_INDEX_RE.match(s)
        if m_bare:
            s = m_bare.group(1)
            part_label = f"#{m_bare.group(2)}"

    return s.strip(), part_label


def _compute_title_match_score(
    core_query: str,
    cand_artist: str,
    cand_title: str,
) -> tuple[float, float]:
    """Separately scores Artist similarity and Title-only similarity.

    Prevents 'cupsize 1' or 'CUPSIZE' from matching 'madk1d, CUPSIZE - 1 мая'
    just because 'cupsize' matched the artist and '1' matched '1 мая'.
    """
    q_norm = normalize_basic(core_query)
    q_tokens = expand_number_tokens(q_norm.split())
    if not q_tokens:
        return 0.0, 0.0

    artist_variants = translit_variants(cand_artist).split()
    # Strip any catalog version suffixes like "(feat. ...)" when comparing core title words
    clean_cand_title = re.sub(r"[\(\[].*?[\)\]]", " ", cand_title)
    title_norm_tokens = expand_number_tokens(normalize_basic(clean_cand_title).split())
    title_variants = translit_variants(clean_cand_title).split()

    if not title_variants or not title_norm_tokens:
        return 0.0, 0.0

    # Identify which query tokens DO NOT belong to the artist name -> must describe the SONG TITLE!
    non_artist_q_tokens: list[str] = []
    artist_hit = 0.0
    for qt in q_tokens:
        qt_vars = translit_variants(qt).split()
        best_art = 0.0
        for qv in qt_vars:
            for av in artist_variants:
                sim = token_similarity(qv, av)
                if sim > best_art:
                    best_art = sim
        if best_art >= 0.84:
            artist_hit = max(artist_hit, best_art)
        else:
            non_artist_q_tokens.append(qt)

    # If ZERO non-artist words exist (e.g. Roblox audio was just named "CUPSIZE" or "cupsize 1"),
    # text alone CANNOT know which song it is! Return title_score = 0.0.
    if not non_artist_q_tokens:
        return artist_hit, 0.0

    # Single 1-digit token alone (e.g. "1") cannot match a multi-word title like "1 мая"
    if (
        len(non_artist_q_tokens) == 1
        and non_artist_q_tokens[0].isdigit()
        and len(non_artist_q_tokens[0]) == 1
        and len(title_norm_tokens) > 1
    ):
        return artist_hit, 0.0

    # Measure forward coverage (query title words -> candidate title)
    fwd_scores: list[float] = []
    for qt in non_artist_q_tokens:
        qt_vars = translit_variants(qt).split()
        best = 0.0
        for qv in qt_vars:
            for tv in title_variants:
                sim = token_similarity(qv, tv)
                if sim > best:
                    best = sim
        fwd_scores.append(best)

    # Measure reverse coverage (candidate title words -> query title words)
    # so "1" doesn't claim 100% match on "1 мая" where "мая" is completely absent!
    non_artist_all_vars = translit_variants(" ".join(non_artist_q_tokens)).split()
    rev_scores: list[float] = []
    for ct in title_norm_tokens:
        ct_vars = translit_variants(ct).split()
        best = 0.0
        for cv in ct_vars:
            for qv in non_artist_all_vars:
                sim = token_similarity(cv, qv)
                if sim > best:
                    best = sim
        rev_scores.append(best)

    fwd_avg = sum(fwd_scores) / len(fwd_scores)
    rev_avg = sum(rev_scores) / len(rev_scores)
    title_score = (0.65 * fwd_avg) + (0.35 * rev_avg)

    # Small penalty if candidate is a multi-artist collab (e.g. "madk1d, CUPSIZE")
    # when the query only mentioned one artist
    if ("," in cand_artist or "&" in cand_artist) and artist_hit > 0:
        parts = [p.strip() for p in re.split(r"[,&]", cand_artist) if p.strip()]
        matched_artists = 0
        q_all_vars = translit_variants(q_norm).split()
        for part in parts:
            part_vars = translit_variants(part).split()
            if any(token_similarity(qv, pv) >= 0.82 for qv in q_all_vars for pv in part_vars):
                matched_artists += 1
        if matched_artists < len(parts):
            title_score -= 0.06

    return artist_hit, max(0.0, min(1.0, title_score))


async def resolve_canonical_track(
    raw_roblox_name: str,
    roblox_creator: str = "",
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Resolves noisy/transliterated Roblox title to canonical Artist & Title.

    Note: NEVER returns studio track duration — duration always comes from Roblox itself.
    """
    ver_tag = detect_version_tag(raw_roblox_name)
    cleaned = clean_roblox_title(raw_roblox_name)
    core_query, part_label = _strip_version_and_part_markers(cleaned or raw_roblox_name)
    fallback_artist, fallback_title = split_artist_title(cleaned or raw_roblox_name, "")

    if not core_query or len(core_query) < 2:
        return {
            "artist": fallback_artist,
            "title": append_version_tag(fallback_title or raw_roblox_name.strip() or "Без названия", ver_tag),
            "confidence": 0.5,
        }

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=8.0)
    candidates: list[dict[str, str]] = []

    try:
        try:
            it_resp = await http.get(
                "https://itunes.apple.com/search",
                params={"term": core_query[:80], "media": "music", "entity": "song", "country": "RU", "limit": 6},
            )
            if it_resp.status_code == 200:
                for item in it_resp.json().get("results", []):
                    art = str(item.get("artistName") or "").strip()
                    trk = str(item.get("trackName") or "").strip()
                    if art and trk:
                        candidates.append({"artist": art, "title": trk})
        except httpx.HTTPError:
            pass

        try:
            dz_resp = await http.get(
                "https://api.deezer.com/search",
                params={"q": core_query[:80], "limit": 6},
            )
            if dz_resp.status_code == 200:
                for item in dz_resp.json().get("data", []):
                    art = str((item.get("artist") or {}).get("name") or "").strip()
                    trk = str(item.get("title") or "").strip()
                    if art and trk:
                        candidates.append({"artist": art, "title": trk})
        except httpx.HTTPError:
            pass
    finally:
        if owns_client:
            await http.aclose()

    best_cand: dict[str, str] | None = None
    best_combined = 0.0
    best_title_sc = 0.0
    canonical_solo_artist = ""

    for cand in candidates:
        art_sc, title_sc = _compute_title_match_score(core_query, cand["artist"], cand["title"])
        # Remember solo canonical artist spelling (e.g. "cupsize" -> "CUPSIZE") even if no song title was given
        if art_sc >= 0.88 and "," not in cand["artist"] and "&" not in cand["artist"] and not canonical_solo_artist:
            canonical_solo_artist = cand["artist"]

        if title_sc < 0.68:
            continue

        combined = (0.72 * title_sc) + (0.28 * max(art_sc, 0.7))
        if combined > best_combined:
            best_combined = combined
            best_title_sc = title_sc
            best_cand = cand

    if best_cand and best_title_sc >= 0.68:
        return {
            "artist": best_cand["artist"],
            "title": append_version_tag(best_cand["title"], ver_tag),
            "confidence": round(best_combined, 3),
        }

    # If the Roblox audio only had an artist name + part index (like "cupsize 1"),
    # preserve the real artist casing ("CUPSIZE") and honest clip label instead of guessing "1 мая"!
    if canonical_solo_artist and not fallback_artist:
        honest_title = f"{canonical_solo_artist} {part_label}".strip() if part_label else (cleaned or raw_roblox_name)
        return {
            "artist": canonical_solo_artist,
            "title": append_version_tag(honest_title, ver_tag),
            "confidence": 0.6,
        }

    return {
        "artist": fallback_artist,
        "title": append_version_tag(fallback_title or cleaned or raw_roblox_name.strip(), ver_tag),
        "confidence": 0.55,
    }
