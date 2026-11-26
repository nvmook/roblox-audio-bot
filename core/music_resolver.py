"""Resolves disguised or messy Roblox audio metadata to canonical Artist & Track names
using free public music catalogs (Deezer & iTunes Search APIs) + fuzzy similarity matching.
"""

from typing import Any
import httpx
from core.matcher import score_song_match
from core.text_utils import (
    build_song_search_index,
    clean_roblox_title,
    split_artist_title,
)


async def resolve_canonical_track(
    raw_roblox_name: str,
    roblox_creator: str = "",
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Takes a noisy/transliterated Roblox audio title and finds the closest real track."""
    cleaned = clean_roblox_title(raw_roblox_name)
    fallback_artist, fallback_title = split_artist_title(cleaned or raw_roblox_name, "")

    if not cleaned or len(cleaned) < 3:
        return {
            "artist": fallback_artist,
            "title": fallback_title or raw_roblox_name.strip() or "Без названия",
            "duration_sec": 0.0,
            "confidence": 0.5,
        }

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=8.0)
    candidates: list[dict[str, Any]] = []

    try:
        # 1. iTunes Search API (RU storefront handles both Russian & Global artists well)
        try:
            it_resp = await http.get(
                "https://itunes.apple.com/search",
                params={"term": cleaned[:80], "media": "music", "entity": "song", "country": "RU", "limit": 5},
            )
            if it_resp.status_code == 200:
                for item in it_resp.json().get("results", []):
                    art = str(item.get("artistName") or "").strip()
                    trk = str(item.get("trackName") or "").strip()
                    dur = float(item.get("trackTimeMillis") or 0) / 1000.0
                    if art and trk:
                        candidates.append(
                            {
                                "artist": art,
                                "title": trk,
                                "duration_sec": round(dur, 1),
                                "search_index": build_song_search_index(art, trk),
                            }
                        )
        except httpx.HTTPError:
            pass

        # 2. Deezer Public Search API (great for phonk, sped-up, and CIS underground)
        try:
            dz_resp = await http.get(
                "https://api.deezer.com/search",
                params={"q": cleaned[:80], "limit": 5},
            )
            if dz_resp.status_code == 200:
                for item in dz_resp.json().get("data", []):
                    art = str((item.get("artist") or {}).get("name") or "").strip()
                    trk = str(item.get("title") or "").strip()
                    dur = float(item.get("duration") or 0)
                    if art and trk:
                        candidates.append(
                            {
                                "artist": art,
                                "title": trk,
                                "duration_sec": round(dur, 1),
                                "search_index": build_song_search_index(art, trk),
                            }
                        )
        except httpx.HTTPError:
            pass
    finally:
        if owns_client:
            await http.aclose()

    best_cand: dict[str, Any] | None = None
    best_score = 0.0
    for cand in candidates:
        sc = score_song_match(cleaned, cand)
        if sc > best_score:
            best_score = sc
            best_cand = cand

    if best_cand and best_score >= 0.64:
        return {
            "artist": best_cand["artist"],
            "title": best_cand["title"],
            "duration_sec": best_cand["duration_sec"],
            "confidence": round(best_score, 3),
        }

    return {
        "artist": fallback_artist,
        "title": fallback_title or cleaned,
        "duration_sec": 0.0,
        "confidence": 0.6,
    }
