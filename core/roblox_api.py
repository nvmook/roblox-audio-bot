"""Official & public Roblox metadata helpers for checking Audio Asset existence,
real Roblox playback duration, batch Creator Store details, and Open Cloud delivery URLs.
"""

import asyncio
from typing import Any
import httpx
from core.config import get_roblox_api_key

AUDIO_ASSET_TYPE_ID = 3

DELETED_MARKERS = (
    "[ content deleted ]",
    "( content deleted )",
    "content deleted",
    "[content deleted]",
)


def is_deleted_name(name: str) -> bool:
    low = (name or "").strip().lower()
    if not low:
        return True
    return any(marker in low for marker in DELETED_MARKERS)


async def check_roblox_audio_batch(
    asset_ids: list[int],
    client: httpx.AsyncClient | None = None,
    api_key: str | None = None,
) -> dict[int, dict[str, Any]]:
    """Checks up to 30 asset IDs per request via Creator Store `items/details`.

    Returns `{asset_id: normalized_meta}` for valid, non-deleted Audio assets (`typeId == 3`),
    including the REAL Roblox audio `duration_sec` (`asset.duration`).
    """
    if not asset_ids:
        return {}

    key = api_key if api_key is not None else get_roblox_api_key()
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=12.0, follow_redirects=True)
    found: dict[int, dict[str, Any]] = {}

    headers = {"Accept": "application/json", "User-Agent": "RobloxAudioBot/1.0"}
    if key:
        headers["x-api-key"] = key

    try:
        # Process in chunks of 30 IDs per HTTP call
        for start in range(0, len(asset_ids), 30):
            chunk = asset_ids[start : start + 30]
            ids_param = ",".join(str(int(x)) for x in chunk)

            for attempt in range(3):
                resp = await http.get(
                    "https://apis.roblox.com/toolbox-service/v1/items/details",
                    params={"assetIds": ids_param},
                    headers=headers,
                )
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", "2.0"))
                    await asyncio.sleep(min(12.0, max(1.0, retry_after * (attempt + 1))))
                    continue
                if resp.status_code != 200:
                    break

                items = resp.json().get("data") or []
                for item in items:
                    asset = item.get("asset") or {}
                    if int(asset.get("typeId") or 0) != AUDIO_ASSET_TYPE_ID:
                        continue
                    aid = int(asset.get("id") or 0)
                    name = str(asset.get("name") or "").strip()
                    if not aid or is_deleted_name(name):
                        continue

                    audio_details = asset.get("audioDetails") or {}
                    creator_obj = item.get("creator") or {}
                    fiat = item.get("fiatProduct") or {}

                    # Real playback duration inside Roblox (in seconds)!
                    roblox_dur = float(asset.get("duration") or 0.0)
                    creator_name = str(creator_obj.get("name") or "").strip()
                    audio_artist = str(audio_details.get("artist") or "").strip()
                    audio_title = str(audio_details.get("title") or "").strip()

                    # If Roblox audioDetails carries structured artist != uploader username, use it
                    effective_name = name
                    if audio_artist and audio_title and audio_artist.lower() != creator_name.lower():
                        if audio_artist.lower() not in audio_title.lower():
                            effective_name = f"{audio_artist} - {audio_title}"
                        else:
                            effective_name = audio_title

                    found[aid] = {
                        "asset_id": aid,
                        "name": effective_name,
                        "raw_name": name,
                        "description": str(asset.get("description") or "").strip(),
                        "creator": creator_name,
                        "is_public": bool(fiat.get("isFree", True) or fiat.get("published", True)),
                        "duration_sec": roblox_dur,
                        "preview_url": "",
                    }
                break
        return found
    except httpx.HTTPError:
        return found
    finally:
        if owns_client:
            await http.aclose()


async def check_roblox_audio_id(
    asset_id: int,
    client: httpx.AsyncClient | None = None,
    api_key: str | None = None,
) -> dict[str, Any] | None:
    """Checks a single numeric asset_id and returns real Roblox metadata + Roblox duration."""
    key = api_key if api_key is not None else get_roblox_api_key()
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=10.0, follow_redirects=True)

    try:
        # 1. Creator Store details first (gives exact Roblox `duration`!)
        batch_res = await check_roblox_audio_batch([int(asset_id)], client=http, api_key=key)
        if int(asset_id) in batch_res:
            return batch_res[int(asset_id)]

        # 2. Fallback to economy metadata for older valid audio assets not indexed in Creator Store
        for attempt in range(2):
            resp = await http.get(
                f"https://economy.roblox.com/v2/assets/{asset_id}/details",
                headers={"Accept": "application/json", "User-Agent": "RobloxAudioBot/1.0"},
            )
            if resp.status_code == 429:
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            if resp.status_code != 200:
                return None

            data = resp.json()
            if int(data.get("AssetTypeId") or 0) != AUDIO_ASSET_TYPE_ID:
                return None

            name = str(data.get("Name") or "").strip()
            if is_deleted_name(name):
                return None

            creator_name = str((data.get("Creator") or {}).get("Name") or "").strip()
            is_public = bool(data.get("IsPublicDomain") or data.get("IsForSale"))
            if not is_public:
                return None

            return {
                "asset_id": int(asset_id),
                "name": name,
                "raw_name": name,
                "description": str(data.get("Description") or "").strip(),
                "creator": creator_name,
                "is_public": is_public,
                "duration_sec": 0.0,
                "preview_url": "",
            }
        return None
    except httpx.HTTPError:
        return None
    finally:
        if owns_client:
            await http.aclose()


async def fetch_open_cloud_audio_location(
    asset_id: int,
    client: httpx.AsyncClient,
    api_key: str,
) -> str:
    """Fetches official CDN audio URL via Roblox Open Cloud Asset Delivery v1 if key has scope."""
    if not api_key:
        return ""
    try:
        resp = await client.get(
            f"https://apis.roblox.com/asset-delivery-api/v1/assetId/{asset_id}",
            headers={"x-api-key": api_key, "Accept": "application/json"},
            follow_redirects=False,
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            return str(resp.headers.get("Location") or "")
        if resp.status_code == 200:
            ctype = resp.headers.get("content-type", "").lower()
            if "json" in ctype:
                body = resp.json()
                return str(body.get("location") or "")
    except httpx.HTTPError:
        pass
    return ""
