"""GitHub Actions Roblox Audio Scanner & Catalog Indexer.

Features:
1. Automatic DB self-repair (`REPAIR_VERSION`) that updates already-saved tracks with
   true Roblox playback duration (`asset.duration` / `ffprobe`) and strict song identification.
2. Batch Creator Store `items/details` queries (30 Asset IDs per HTTP request) — 30x faster
   and avoids 429 rate-limit walls while scanning up to 150,000 IDs per run.
3. Multi-speed Shazam (`1.0x`, `0.80x` anti-sped-up, `0.72x` anti-nightcore, `1.22x` anti-slowed)
   + strict music catalog verification (`Deezer` / `iTunes`).
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
from core.roblox_api import check_roblox_audio_batch, is_deleted_name
from scanner.identifier import identify_roblox_audio

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
# Silence verbose per-request httpx logs so Actions logs remain clean and readable
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("roblox_scanner")

CURRENT_REPAIR_VERSION = "3"

CATALOG_KEYWORDS: list[str] = [
    "cupsize", "капсайз", "три дня дождя", "кишлак", "shadowraze", "шадоурейз",
    "пошлая молли", "автостопом по фазе сна", "апфс", "полматери", "dvrst",
    "kordhell", "phonk", "sped up", "russian phonk", "madk1d", "тёмный принц",
    "моргенштерн", "kizaru", "big baby tape", "og buda", "платина",
    "фараон", "лсп", "нервы", "король и шут", "цой", "инстасамка", "дора",
    "мейби бейби", "scally milano", "uglystephan", "kai angel", "9mice",
    "viperr", "toxi$", "macan", "баста", "скриптонит", "miyagi", "эндшпиль",
    "crystal castles", "mareux", "pastel ghost", "funk", "brazilian phonk",
]


async def repair_existing_db_songs(
    db: Database,
    http: httpx.AsyncClient,
    api_key: str,
) -> int:
    """One-time migration pass that fixes Roblox durations & misidentified titles on existing rows."""
    done_ver = await db.get_scanner_value("repair_version", "0")
    if done_ver == CURRENT_REPAIR_VERSION:
        return 0

    rows = await db.turso.execute(
        "SELECT asset_id, source FROM songs WHERE is_active = 1 AND source != 'admin' LIMIT 600"
    )
    if not rows:
        await db.set_scanner_values({"repair_version": CURRENT_REPAIR_VERSION})
        return 0

    logger.info("Running DB repair v%s on %d existing songs...", CURRENT_REPAIR_VERSION, len(rows))
    all_ids = [int(r["asset_id"]) for r in rows]
    source_by_id = {int(r["asset_id"]): str(r.get("source") or "catalog") for r in rows}

    meta_map = await check_roblox_audio_batch(all_ids, client=http, api_key=api_key)
    repaired_batch: list[dict[str, Any]] = []

    for aid in all_ids:
        meta = meta_map.get(aid)
        if not meta:
            continue
        ident = await identify_roblox_audio(meta, client=http, api_key=api_key)
        repaired_batch.append(
            {
                "asset_id": aid,
                "artist": ident["artist"],
                "title": ident["title"],
                "roblox_name": meta.get("raw_name") or meta["name"],
                "roblox_creator": meta.get("creator", ""),
                "duration_sec": ident["duration_sec"],
                "confidence": ident["confidence"],
                "source": source_by_id.get(aid, "catalog"),
                "is_active": True,
            }
        )
        if len(repaired_batch) >= 50:
            await db.upsert_songs_batch(repaired_batch)
            repaired_batch.clear()

    if repaired_batch:
        await db.upsert_songs_batch(repaired_batch)

    await db.set_scanner_values({"repair_version": CURRENT_REPAIR_VERSION})
    logger.info("DB repair v%s completed (%d tracks refreshed with real Roblox durations).", CURRENT_REPAIR_VERSION, len(meta_map))
    return len(meta_map)


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

        if not found_ids:
            continue

        meta_map = await check_roblox_audio_batch(found_ids[:30], client=http, api_key=api_key)
        batch_to_save: list[dict[str, Any]] = []
        for aid, meta in meta_map.items():
            ident = await identify_roblox_audio(meta, client=http, api_key=api_key)
            batch_to_save.append(
                {
                    "asset_id": aid,
                    "artist": ident["artist"],
                    "title": ident["title"],
                    "roblox_name": meta.get("raw_name") or meta["name"],
                    "roblox_creator": meta.get("creator", ""),
                    "duration_sec": ident["duration_sec"],
                    "confidence": ident["confidence"],
                    "source": "catalog",
                    "is_active": True,
                }
            )
            await asyncio.sleep(0.08)

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
    """Generates numeric Asset IDs and verifies existence & Audio type in batches of 30 IDs."""
    default_start = int(os.getenv("START_ASSET_ID", "17500000000"))
    last_id_raw = await db.get_scanner_value("last_asset_id", str(default_start))
    current_id = int(last_id_raw) if last_id_raw.isdigit() else default_start

    total_scanned_raw = await db.get_scanner_value("total_scanned", "0")
    historical_scanned = int(total_scanned_raw) if total_scanned_raw.isdigit() else 0

    sem = asyncio.Semaphore(max(1, min(concurrency, 10)))
    scanned_this_run = 0
    saved_this_run = 0
    # Each HTTP batch inspects 30 IDs; 50 batches = 1,500 IDs per checkpoint cycle
    ids_per_cycle = 1500

    async def inspect_batch_of_30(sub_ids: list[int]) -> list[dict[str, Any]]:
        async with sem:
            meta_map = await check_roblox_audio_batch(sub_ids, client=http, api_key=api_key)
            await asyncio.sleep(0.15)
        out: list[dict[str, Any]] = []
        for aid, meta in meta_map.items():
            ident = await identify_roblox_audio(meta, client=http, api_key=api_key)
            out.append(
                {
                    "asset_id": aid,
                    "artist": ident["artist"],
                    "title": ident["title"],
                    "roblox_name": meta.get("raw_name") or meta["name"],
                    "roblox_creator": meta.get("creator", ""),
                    "duration_sec": ident["duration_sec"],
                    "confidence": ident["confidence"],
                    "source": "scanner",
                    "is_active": True,
                }
            )
        return out

    logger.info(
        "Starting batch numeric ID scan from %d (target=%d, batch_concurrency=%d)",
        current_id,
        target_ids,
        min(concurrency, 10),
    )

    while scanned_this_run < target_ids:
        if time.monotonic() >= deadline_monotonic:
            logger.info("Reached time budget ceiling — saving checkpoint cleanly.")
            break

        step = min(ids_per_cycle, target_ids - scanned_this_run)
        cycle_ids = [current_id + i for i in range(1, step + 1)]
        chunks_of_30 = [cycle_ids[i : i + 30] for i in range(0, len(cycle_ids), 30)]

        nested_results = await asyncio.gather(*(inspect_batch_of_30(ch) for ch in chunks_of_30))
        valid_songs = [song for group in nested_results for song in group]

        if valid_songs:
            saved_this_run += await db.upsert_songs_batch(valid_songs)
            for s in valid_songs:
                logger.info(
                    "FOUND AUDIO [%d] (%.0fs): %s - %s (Roblox: '%s')",
                    s["asset_id"],
                    s["duration_sec"],
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
        "SELECT asset_id FROM songs WHERE is_active = 1 ORDER BY updated_at ASC LIMIT ?",
        [sample_size],
    )
    if not rows:
        return 0

    ids = [int(r["asset_id"]) for r in rows]
    meta_map = await check_roblox_audio_batch(ids, client=http, api_key=api_key)
    deactivated = 0
    now = int(time.time())

    for aid in ids:
        meta = meta_map.get(aid)
        if meta is None or is_deleted_name(meta.get("name", "")):
            await db.turso.execute(
                "UPDATE songs SET is_active = 0, updated_at = ? WHERE asset_id = ?",
                [now, aid],
            )
            deactivated += 1
        else:
            await db.turso.execute(
                "UPDATE songs SET duration_sec = ?, updated_at = ? WHERE asset_id = ?",
                [float(meta.get("duration_sec") or 0.0), now, aid],
            )
    return deactivated


async def main() -> None:
    target_ids = int(os.getenv("TARGET_IDS_PER_RUN", "150000"))
    concurrency = int(os.getenv("SCANNER_CONCURRENCY", "8"))
    max_run_minutes = float(os.getenv("MAX_RUN_MINUTES", "345"))
    scan_mode = os.getenv("SCAN_MODE", "hybrid").strip().lower()

    deadline = time.monotonic() + (max_run_minutes * 60.0)
    api_key = get_roblox_api_key()

    db = Database()
    await db.init_schema()

    limits = httpx.Limits(max_connections=30, max_keepalive_connections=15)
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, limits=limits) as http:
        repaired = await repair_existing_db_songs(db, http, api_key)

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
            deactivated = await revalidate_existing_songs(db, http, api_key, sample_size=30)

    logger.info(
        "Run finished! Repaired=%d | Catalog saved=%d | Numeric scanned=%d | Numeric saved=%d | Deactivated=%d",
        repaired,
        catalog_saved,
        scanned_ids,
        range_saved,
        deactivated,
    )


if __name__ == "__main__":
    asyncio.run(main())
