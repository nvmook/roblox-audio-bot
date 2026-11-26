"""GitHub Actions Roblox Audio Scanner & Catalog Indexer.

Features:
1. Numeric ID generator & existence checker (scans up to TARGET_IDS_PER_RUN=150,000 IDs per run,
   resuming automatically from Turso `scanner_state`).
2. Creator Store public Audio search across popular CIS/Phonk/Sped-Up keywords so playable hits
   populate immediately.
3. Acoustic (`shazamio`) + Music Catalog (`Deezer` / `iTunes`) song identification.
4. Graceful time-budget checkpointing before GitHub Actions runner timeout.
"""

import asyncio
from datetime import datetime, timezone
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any
import httpx

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.config import get_roblox_api_key
from core.db import Database
from core.roblox_api import check_roblox_audio_id, is_deleted_name
from scanner.identifier import identify_roblox_audio

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("roblox_scanner")

# Curated keywords to discover public playable Roblox audio tracks quickly via Creator Store
CATALOG_KEYWORDS: list[str] = [
    "cupsize", "капсайз", "три дня дождя", "кишлак", "shadowraze", "шадоурейз",
    "пошлая молли", "автостопом по фазе сна", "апфс", "полматери", "dvrst",
    "kordhell", "phonk", "sped up", "russian phonk", "madk1d", "tёмный принц",
    "скр скр", "моргенштерн", "kizaru", "big baby tape", "og buda", "платина",
    "фараон", "лсп", "нервы", "король и шут", "цой", "инстасамка", "дора",
    "мейби бейби", "scally milano", "uglystephan", "kai angel", "9mice",
    "viperr", "toxi$", "macan", "баста", "скриптонит", "miyagi", "эндшпиль",
    "crystal castles", "mareux", "pastel ghost", "funk", "brazilian phonk",
]


async def scan_creator_store_keywords(
    db: Database,
    http: httpx.AsyncClient,
    api_key: str,
    max_keywords: int = 12,
) -> int:
    """Queries public Roblox Creator Store Audio search for popular artists/genres."""
    cursor_raw = await db.get_scanner_value("keyword_index", "0")
    kw_idx = int(cursor_raw) if cursor_raw.isdigit() else 0
    saved_total = 0

    for offset in range(max_keywords):
        keyword = CATALOG_KEYWORDS[(kw_idx + offset) % len(CATALOG_KEYWORDS)]
        headers = {"Accept": "application/json", "User-Agent": "RobloxAudioBot/1.0"}
        if api_key:
            headers["x-api-key"] = api_key

        found_ids: list[int] = []
        try:
            # Creator Store Toolbox Audio category (3 = Audio)
            resp = await http.get(
                "https://apis.roblox.com/toolbox-service/v1/marketplace/3",
                params={"keyword": keyword, "limit": 30, "pageNumber": 0},
                headers=headers,
            )
            if resp.status_code == 200:
                items = resp.json().get("data") or []
                for item in items:
                    aid = item.get("id")
                    if isinstance(aid, int) or (isinstance(aid, str) and aid.isdigit()):
                        found_ids.append(int(aid))
        except httpx.HTTPError as exc:
            logger.warning("Creator Store query failed for '%s': %s", keyword, exc)

        batch_to_save: list[dict[str, Any]] = []
        for aid in found_ids[:25]:
            meta = await check_roblox_audio_id(aid, client=http, api_key=api_key)
            if not meta:
                continue
            ident = await identify_roblox_audio(meta, http)
            batch_to_save.append(
                {
                    "asset_id": aid,
                    "artist": ident["artist"],
                    "title": ident["title"],
                    "roblox_name": meta["name"],
                    "roblox_creator": meta.get("creator", ""),
                    "duration_sec": ident.get("duration_sec", 0.0),
                    "confidence": ident.get("confidence", 0.8),
                    "source": "catalog",
                    "is_active": True,
                }
            )
            await asyncio.sleep(0.12)

        if batch_to_save:
            saved_total += await db.upsert_songs_batch(batch_to_save)
            logger.info("Keyword '%s': saved %d audio tracks", keyword, len(batch_to_save))

    next_kw_idx = (kw_idx + max_keywords) % len(CATALOG_KEYWORDS)
    await db.set_scanner_values({"keyword_index": next_kw_idx})
    return saved_total


async def scan_numeric_id_range(
    db: Database,
    http: httpx.AsyncClient,
    api_key: str,
    target_ids: int,
    concurrency: int,
    deadline_monotonic: float,
) -> tuple[int, int]:
    """Generates numeric Asset IDs and verifies existence & Audio type in Roblox."""
    default_start = int(os.getenv("START_ASSET_ID", "17500000000"))
    last_id_raw = await db.get_scanner_value("last_asset_id", str(default_start))
    current_id = int(last_id_raw) if last_id_raw.isdigit() else default_start

    total_scanned_raw = await db.get_scanner_value("total_scanned", "0")
    historical_scanned = int(total_scanned_raw) if total_scanned_raw.isdigit() else 0

    sem = asyncio.Semaphore(concurrency)
    scanned_this_run = 0
    saved_this_run = 0
    chunk_size = max(100, min(1000, concurrency * 20))

    async def inspect_single_id(aid: int) -> dict[str, Any] | None:
        async with sem:
            meta = await check_roblox_audio_id(aid, client=http, api_key=api_key)
            if not meta:
                return None
            ident = await identify_roblox_audio(meta, http)
            return {
                "asset_id": aid,
                "artist": ident["artist"],
                "title": ident["title"],
                "roblox_name": meta["name"],
                "roblox_creator": meta.get("creator", ""),
                "duration_sec": ident.get("duration_sec", 0.0),
                "confidence": ident.get("confidence", 0.75),
                "source": "scanner",
                "is_active": True,
            }

    logger.info(
        "Starting numeric ID scan from %d (target=%d, concurrency=%d)",
        current_id,
        target_ids,
        concurrency,
    )

    while scanned_this_run < target_ids:
        if time.monotonic() >= deadline_monotonic:
            logger.info("Reached time budget ceiling — saving checkpoint cleanly.")
            break

        remaining = target_ids - scanned_this_run
        step = min(chunk_size, remaining)
        id_batch = [current_id + i for i in range(1, step + 1)]

        results = await asyncio.gather(*(inspect_single_id(aid) for aid in id_batch))
        valid_songs = [r for r in results if r is not None]

        if valid_songs:
            saved_this_run += await db.upsert_songs_batch(valid_songs)
            for s in valid_songs:
                logger.info(
                    "FOUND AUDIO [%d]: %s - %s (Roblox: '%s')",
                    s["asset_id"],
                    s["artist"],
                    s["title"],
                    s["roblox_name"],
                )

        current_id += step
        scanned_this_run += step
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        await db.set_scanner_values(
            {
                "last_asset_id": current_id,
                "total_scanned": historical_scanned + scanned_this_run,
                "last_run_at": now_iso,
            }
        )
        logger.info(
            "Progress: %d / %d IDs checked (cursor=%d, saved=%d)",
            scanned_this_run,
            target_ids,
            current_id,
            saved_this_run,
        )

    return scanned_this_run, saved_this_run


async def revalidate_existing_songs(
    db: Database,
    http: httpx.AsyncClient,
    api_key: str,
    sample_size: int = 30,
) -> int:
    """Checks older indexed songs and disables any newly moderated [ Content Deleted ] assets."""
    rows = await db.turso.execute(
        "SELECT asset_id, roblox_name FROM songs WHERE is_active = 1 ORDER BY updated_at ASC LIMIT ?",
        [sample_size],
    )
    deactivated = 0
    now = int(time.time())
    for row in rows:
        aid = int(row["asset_id"])
        meta = await check_roblox_audio_id(aid, client=http, api_key=api_key)
        if meta is None or is_deleted_name(meta.get("name", "")):
            await db.turso.execute(
                "UPDATE songs SET is_active = 0, updated_at = ? WHERE asset_id = ?",
                [now, aid],
            )
            deactivated += 1
        else:
            await db.turso.execute(
                "UPDATE songs SET updated_at = ? WHERE asset_id = ?",
                [now, aid],
            )
        await asyncio.sleep(0.1)
    return deactivated


async def main() -> None:
    target_ids = int(os.getenv("TARGET_IDS_PER_RUN", "150000"))
    concurrency = int(os.getenv("SCANNER_CONCURRENCY", "25"))
    # GitHub-hosted runners cap jobs at 360 min (6h); self-hosted support 600 min (10h).
    # Default graceful stop at 345 min on hosted runners so progress is always saved cleanly.
    max_run_minutes = float(os.getenv("MAX_RUN_MINUTES", "345"))
    scan_mode = os.getenv("SCAN_MODE", "hybrid").strip().lower()

    deadline = time.monotonic() + (max_run_minutes * 60.0)
    api_key = get_roblox_api_key()

    db = Database()
    await db.init_schema()

    limits = httpx.Limits(max_connections=concurrency + 15, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, limits=limits) as http:
        catalog_saved = 0
        scanned_ids = 0
        range_saved = 0

        if scan_mode in ("hybrid", "catalog"):
            catalog_saved = await scan_creator_store_keywords(db, http, api_key, max_keywords=10)

        if scan_mode in ("hybrid", "id_range") and time.monotonic() < deadline:
            scanned_ids, range_saved = await scan_numeric_id_range(
                db=db,
                http=http,
                api_key=api_key,
                target_ids=target_ids,
                concurrency=concurrency,
                deadline_monotonic=deadline,
            )

        deactivated = 0
        if time.monotonic() + 30 < deadline:
            deactivated = await revalidate_existing_songs(db, http, api_key, sample_size=20)

    logger.info(
        "Run finished! Catalog saved=%d | Numeric scanned=%d | Numeric saved=%d | Deactivated=%d",
        catalog_saved,
        scanned_ids,
        range_saved,
        deactivated,
    )


if __name__ == "__main__":
    asyncio.run(main())
