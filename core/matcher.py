"""Lightweight fuzzy & trigram similarity engine for Vercel (0 MB native deps, <2 ms latency).

Solves changed/noisy Roblox metadata & user typos without requiring a multi-GB LLM on Vercel.
"""

from difflib import SequenceMatcher
from typing import Any
from core.text_utils import (
    expand_number_tokens,
    normalize_basic,
    phonetic_skeleton,
    translit_variants,
)


def _char_ngrams(text: str, n: int = 3) -> set[str]:
    padded = f"  {text}  "
    if len(padded) < n:
        return {padded}
    return {padded[i : i + n] for i in range(len(padded) - n + 1)}


def trigram_dice(a: str, b: str) -> float:
    """Sørensen–Dice coefficient over character 3-grams."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ga = _char_ngrams(a, 3)
    gb = _char_ngrams(b, 3)
    overlap = len(ga & gb)
    if overlap == 0:
        return 0.0
    return (2.0 * overlap) / (len(ga) + len(gb))


def token_similarity(q_tok: str, cand_tok: str) -> float:
    if q_tok == cand_tok:
        return 1.0
    if len(q_tok) >= 3 and cand_tok.startswith(q_tok):
        return 0.93
    if len(cand_tok) >= 4 and q_tok.startswith(cand_tok):
        return 0.88
    if phonetic_skeleton(q_tok) and phonetic_skeleton(q_tok) == phonetic_skeleton(cand_tok):
        return 0.95
    seq = SequenceMatcher(None, q_tok, cand_tok).ratio()
    tri = trigram_dice(q_tok, cand_tok)
    return max(seq, tri)


def score_song_match(query: str, song: dict[str, Any]) -> float:
    """Returns similarity score in [0.0, 1.0] between a user query and a song record."""
    q_norm = normalize_basic(query)
    if not q_norm:
        return 0.0

    # Direct numeric Asset ID search
    if q_norm.isdigit() and str(song.get("asset_id", "")) == q_norm:
        return 1.0

    artist = str(song.get("artist") or "")
    title = str(song.get("title") or "")
    search_index = str(song.get("search_index") or "")
    if not search_index:
        search_index = translit_variants(f"{artist} {title} {song.get('roblox_name', '')}")

    cand_tokens = search_index.split()
    if not cand_tokens:
        return 0.0

    # Build query token groups: each original query token gets its translit & number variants
    raw_q_tokens = expand_number_tokens(q_norm.split())[:8]
    if not raw_q_tokens:
        return 0.0

    token_scores: list[float] = []
    for raw_tok in raw_q_tokens:
        q_variants = translit_variants(raw_tok).split()
        best_for_tok = 0.0
        for qv in q_variants:
            for ct in cand_tokens:
                sim = token_similarity(qv, ct)
                if sim > best_for_tok:
                    best_for_tok = sim
                    if best_for_tok >= 0.99:
                        break
            if best_for_tok >= 0.99:
                break
        token_scores.append(best_for_tok)

    avg_tok_score = sum(token_scores) / len(token_scores)
    min_tok_score = min(token_scores)

    # Full-phrase trigram bonus against canonical "Artist - Title" variants
    canonical_variants = translit_variants(f"{artist} {title}")
    q_all_variants = translit_variants(q_norm)
    phrase_tri = trigram_dice(q_all_variants, canonical_variants)

    # Substring bonus if normalized query appears directly inside search_index
    substr_bonus = 0.08 if q_norm in search_index else 0.0

    # Weighted blend favoring coverage of all user query words + fuzzy tolerance
    blended = (0.62 * avg_tok_score) + (0.20 * min_tok_score) + (0.18 * phrase_tri) + substr_bonus
    return min(1.0, round(blended, 4))


def rank_songs(
    query: str,
    candidates: list[dict[str, Any]],
    min_score: float = 0.44,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Scores, filters, and sorts candidate songs by similarity descending."""
    scored: list[tuple[float, float, dict[str, Any]]] = []
    for song in candidates:
        sc = score_song_match(query, song)
        if sc >= min_score:
            conf = float(song.get("confidence") or 1.0)
            scored.append((sc, conf, {**song, "match_score": sc}))

    scored.sort(key=lambda item: (item[0], item[1], int(item[2].get("updated_at") or 0)), reverse=True)
    return [item[2] for item in scored[:limit]]
