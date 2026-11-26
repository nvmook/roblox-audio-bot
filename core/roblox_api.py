"""Official & public Roblox metadata helpers for checking Audio Asset existence & status."""

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


async def check_roblox_audio_id(
    asset_id: int,
    client: httpx.AsyncClient | None = None,
    api_key: str | None = None,
) -> dict[str, Any] | None:
    """Checks if numeric asset_id exists in Roblox AND is a live Audio asset.

    Returns normalized dict:
      {
        "asset_id": int,
        "name": str,
        "description": str,
        "creator": str,
        "is_public": bool,
        "duration_sec": float,
        "preview_url": str,
      }
    or None if nonexistent, not an audio asset, or [ Content Deleted ].
    """
    key = api_key if api_key is not None else get_roblox_api_key()
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=10.0, follow_redirects=True)

    try:
        # 1. Try Official Roblox Open Cloud Assets v1 if API key is provided
        if key:
            oc_resp = await http.get(
                f"https://apis.roblox.com/assets/v1/assets/{asset_id}",
                headers={"x-api-key": key, "Accept": "application/json"},
            )
            if oc_resp.status_code == 200:
                oc = oc_resp.json()
                atype = str(oc.get("assetType") or "").lower()
                if "audio" not in atype and atype != "3":
                    return None
                display_name = str(oc.get("displayName") or "").strip()
                if is_deleted_name(display_name):
                    return None
                moderation = str(
                    (oc.get("moderationResult") or {}).get("moderationState") or ""
                ).lower()
                if "rejected" in moderation:
                    return None
                return {
                    "asset_id": int(asset_id),
                    "name": display_name,
                    "description": str(oc.get("description") or "").strip(),
                    "creator": str((oc.get("creationContext") or {}).get("creator", {}).get("userId") or ""),
                    "is_public": True,
                    "duration_sec": 0.0,
                    "preview_url": str(oc.get("previewUrl") or ""),
                }
            if oc_resp.status_code == 404:
                return None

        # 2. Public Marketplace Details endpoint (economy.roblox.com)
        for attempt in range(3):
            resp = await http.get(
                f"https://economy.roblox.com/v2/assets/{asset_id}/details",
                headers={"Accept": "application/json", "User-Agent": "RobloxAudioBot/1.0"},
            )
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "2.0"))
                await asyncio.sleep(min(10.0, max(1.0, retry_after * (attempt + 1))))
                continue
            if resp.status_code != 200:
                return None

            data = resp.json()
            if int(data.get("AssetTypeId") or 0) != AUDIO_ASSET_TYPE_ID:
                return None

            name = str(data.get("Name") or "").strip()
            if is_deleted_name(name):
                return None

            creator_obj = data.get("Creator") or {}
            creator_name = str(creator_obj.get("Name") or "").strip()
            is_public = bool(data.get("IsPublicDomain") or data.get("IsForSale"))

            return {
                "asset_id": int(asset_id),
                "name": name,
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
