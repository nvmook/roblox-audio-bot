"""Audio recognizer & metadata resolver for Roblox audio assets.

1. If a public audio preview URL is available, listens to it via Shazam (`shazamio`)
   to recognize the exact real song even when the Roblox title is disguised.
2. Otherwise (or as fallback), resolves noisy/transliterated Roblox metadata against
   iTunes & Deezer public music catalogs + fuzzy trigram matching.
"""

import os
import tempfile
from typing import Any
import httpx
from core.music_resolver import resolve_canonical_track

try:
    from shazamio import Shazam  # type: ignore
except ImportError:
    Shazam = None  # Graceful fallback if shazamio is not installed in lightweight environments


async def recognize_by_preview_url(
    preview_url: str,
    client: httpx.AsyncClient,
) -> dict[str, Any] | None:
    """Downloads up to 1.5 MB of a public preview stream and identifies the track via Shazam."""
    if not preview_url or Shazam is None:
        return None
    if not preview_url.startswith(("https://", "http://")):
        return None

    tmp_path = ""
    try:
        resp = await client.get(preview_url, timeout=12.0)
        if resp.status_code != 200 or not resp.content:
            return None

        audio_bytes = resp.content[: 1_500_000]
        if len(audio_bytes) < 4096:
            return None

        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp_file:
            tmp_file.write(audio_bytes)
            tmp_path = tmp_file.name

        shazam = Shazam()
        out = await shazam.recognize(tmp_path)
        track = out.get("track") if isinstance(out, dict) else None
        if not track:
            return None

        artist = str(track.get("subtitle") or "").strip()
        title = str(track.get("title") or "").strip()
        if not title:
            return None

        return {
            "artist": artist,
            "title": title,
            "duration_sec": 0.0,
            "confidence": 0.98,
        }
    except Exception:
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


async def identify_roblox_audio(
    roblox_meta: dict[str, Any],
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Identifies artist & title for a verified Roblox audio asset."""
    preview_url = str(roblox_meta.get("preview_url") or "").strip()
    if preview_url:
        acoustic = await recognize_by_preview_url(preview_url, client)
        if acoustic:
            return acoustic

    return await resolve_canonical_track(
        raw_roblox_name=str(roblox_meta.get("name") or ""),
        roblox_creator=str(roblox_meta.get("creator") or ""),
        client=client,
    )
