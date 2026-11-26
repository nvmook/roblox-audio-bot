"""Audio recognizer & metadata resolver for Roblox audio assets.

Key improvements:
1. Measures & preserves the REAL Roblox audio playback duration (`ffprobe` or Roblox `asset.duration`),
   never substituting original studio track duration from iTunes/Deezer.
2. Multi-speed Shazam recognition (`1.0x`, `0.80x`, `0.72x`, `1.22x` via `ffmpeg`) so sped-up / nightcore
   Roblox tracks (like sped-up 'CUPSIZE - семнадцать ножевых') are pitch-restored and recognized accurately
   instead of misidentifying as higher-BPM songs like 'madk1d, CUPSIZE - 1 мая'.
"""

import os
import subprocess
import tempfile
from typing import Any
import httpx
from core.music_resolver import (
    append_version_tag,
    detect_version_tag,
    resolve_canonical_track,
)
from core.roblox_api import fetch_open_cloud_audio_location
from core.text_utils import normalize_basic, translit_variants

try:
    from shazamio import Shazam  # type: ignore
except ImportError:
    Shazam = None


def _probe_audio_duration(file_path: str) -> float:
    """Reads exact audio duration in seconds via ffprobe."""
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=6,
            check=False,
        )
        val = (proc.stdout or "").strip()
        if val:
            dur = float(val)
            if 0.1 <= dur <= 3600:
                return round(dur, 1)
    except Exception:
        pass
    return 0.0


def _render_speed_adjusted_clip(src_path: str, rate: float, pitch_locked: bool = False) -> str:
    """Creates a temporary speed/pitch-restored audio clip via ffmpeg for Shazam."""
    fd, out_path = tempfile.mkstemp(suffix=".ogg")
    os.close(fd)
    if pitch_locked:
        af_filter = f"atempo={rate:.3f}"
    else:
        # Reverses classic Roblox/TikTok varispeed (pitch + tempo shifted together)
        af_filter = f"asetrate=44100*{rate:.3f},aresample=44100"

    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                src_path,
                "-vn",
                "-af",
                af_filter,
                "-t",
                "15",
                out_path,
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
            return out_path
    except Exception:
        pass

    if os.path.exists(out_path):
        try:
            os.remove(out_path)
        except OSError:
            pass
    return ""


async def _shazam_single_file(shazam: Any, path: str) -> tuple[str, str] | None:
    try:
        out = await shazam.recognize(path)
        track = out.get("track") if isinstance(out, dict) else None
        if not track:
            return None
        artist = str(track.get("subtitle") or "").strip()
        title = str(track.get("title") or "").strip()
        if title:
            return artist, title
    except Exception:
        pass
    return None


def _matches_hint_words(raw_roblox_name: str, artist: str, title: str) -> bool:
    """Checks if recognized track shares meaningful hint words with the Roblox upload name."""
    norm_r = normalize_basic(raw_roblox_name)
    if not norm_r:
        return True
    cand_vars = set(translit_variants(f"{artist} {title}").split())
    r_vars = [w for w in translit_variants(norm_r).split() if len(w) >= 3]
    if not r_vars:
        return True
    return any(w in cand_vars for w in r_vars)


async def recognize_by_audio_url(
    audio_url: str,
    raw_roblox_name: str,
    client: httpx.AsyncClient,
) -> tuple[dict[str, Any] | None, float]:
    """Downloads Roblox audio stream, probes exact Roblox duration, and runs multi-speed Shazam.

    Returns `(recognized_track_or_None, probed_roblox_duration_sec)`.
    """
    if not audio_url or not audio_url.startswith(("https://", "http://")):
        return None, 0.0

    tmp_files: list[str] = []
    probed_duration = 0.0

    try:
        resp = await client.get(audio_url, timeout=14.0)
        if resp.status_code != 200 or not resp.content or len(resp.content) < 2048:
            return None, 0.0

        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp_file:
            tmp_file.write(resp.content[: 4_000_000])
            orig_path = tmp_file.name
            tmp_files.append(orig_path)

        probed_duration = _probe_audio_duration(orig_path)
        if Shazam is None:
            return None, probed_duration

        shazam = Shazam()
        explicit_tag = detect_version_tag(raw_roblox_name)

        # Speed profiles: (rate, pitch_locked, inferred_tag)
        # rate < 1.0 slows a sped-up Roblox clip back down to 1.0x studio pitch/tempo!
        if explicit_tag == "(sped up)":
            passes = [
                (0.80, False, "(sped up)"),
                (0.72, False, "(sped up)"),
                (0.80, True, "(sped up)"),
                (1.00, False, "(sped up)"),
            ]
        elif explicit_tag == "(slowed)":
            passes = [
                (1.22, False, "(slowed)"),
                (1.00, False, "(slowed)"),
            ]
        else:
            passes = [
                (1.00, False, ""),
                (0.80, False, "(sped up)"),
                (0.72, False, "(sped up)"),
                (0.80, True, "(sped up)"),
                (1.22, False, "(slowed)"),
            ]

        fallback_1x: tuple[str, str] | None = None

        for rate, pitch_locked, inferred_tag in passes:
            if rate == 1.00:
                hit = await _shazam_single_file(shazam, orig_path)
                if hit:
                    art, trk = hit
                    # On short Roblox clips (<= 14s), pitched-up audio at 1.0x can false-match a faster song
                    # (e.g. sped-up 'семнадцать ножевых' misidentified as 'madk1d, CUPSIZE - 1 мая').
                    # Verify 0.80x de-sped clip as well before trusting a questionable 1.0x hit!
                    is_short = 0 < probed_duration <= 14.0
                    if not is_short and _matches_hint_words(raw_roblox_name, art, trk):
                        return {
                            "artist": art,
                            "title": append_version_tag(trk, inferred_tag or explicit_tag),
                            "confidence": 0.98,
                        }, probed_duration
                    fallback_1x = hit
                continue

            shifted_path = _render_speed_adjusted_clip(orig_path, rate=rate, pitch_locked=pitch_locked)
            if not shifted_path:
                continue
            tmp_files.append(shifted_path)

            hit = await _shazam_single_file(shazam, shifted_path)
            if hit:
                art, trk = hit
                return {
                    "artist": art,
                    "title": append_version_tag(trk, inferred_tag or explicit_tag),
                    "confidence": 0.97,
                }, probed_duration

        if fallback_1x:
            art, trk = fallback_1x
            return {
                "artist": art,
                "title": append_version_tag(trk, explicit_tag),
                "confidence": 0.90,
            }, probed_duration

        return None, probed_duration
    except Exception:
        return None, probed_duration
    finally:
        for p in tmp_files:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


async def identify_roblox_audio(
    roblox_meta: dict[str, Any],
    client: httpx.AsyncClient,
    api_key: str = "",
) -> dict[str, Any]:
    """Identifies artist & title AND returns the true Roblox audio duration."""
    roblox_dur = float(roblox_meta.get("duration_sec") or 0.0)
    raw_name = str(roblox_meta.get("raw_name") or roblox_meta.get("name") or "")
    effective_name = str(roblox_meta.get("name") or raw_name)

    audio_url = str(roblox_meta.get("preview_url") or "").strip()
    if not audio_url and api_key:
        audio_url = await fetch_open_cloud_audio_location(
            int(roblox_meta["asset_id"]),
            client=client,
            api_key=api_key,
        )

    if audio_url:
        acoustic, probed_dur = await recognize_by_audio_url(audio_url, raw_name, client)
        if probed_dur > 0:
            roblox_dur = probed_dur
        if acoustic:
            return {
                "artist": acoustic["artist"],
                "title": acoustic["title"],
                "duration_sec": roblox_dur,
                "confidence": acoustic["confidence"],
            }

    resolved = await resolve_canonical_track(
        raw_roblox_name=effective_name,
        roblox_creator=str(roblox_meta.get("creator") or ""),
        client=client,
    )
    return {
        "artist": resolved["artist"],
        "title": resolved["title"],
        "duration_sec": roblox_dur,
        "confidence": resolved["confidence"],
    }
