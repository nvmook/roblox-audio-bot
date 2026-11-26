from datetime import datetime, timedelta, timezone
import os
from typing import TypedDict

ROOT_ADMIN_ID = 5188800998
DEFAULT_DAILY_FREE_LIMIT = 5
REFERRAL_BONUS_SEARCHES = 3
SUBMIT_SONG_BONUS_SEARCHES = 2
MAX_DAILY_SUBMIT_REWARDS = 5

MSK_TZ = timezone(timedelta(hours=3))


class StarPlan(TypedDict):
    id: str
    title: str
    short_btn: str
    description: str
    stars: int
    bonus_searches: int
    vip_days: int
    forever: bool


STARS_PLANS: dict[str, StarPlan] = {
    "pack_25": {
        "id": "pack_25",
        "title": "⚡ Пакет +25 поисков",
        "short_btn": "⚡ +25 поисков — 19 ⭐",
        "description": "25 дополнительных успешных поисков Roblox ID песен (не сгорают).",
        "stars": 19,
        "bonus_searches": 25,
        "vip_days": 0,
        "forever": False,
    },
    "vip_7d": {
        "id": "vip_7d",
        "title": "🔥 VIP Безлимит на 7 дней",
        "short_btn": "🔥 Безлимит 7 дней — 49 ⭐",
        "description": "Неограниченный поиск рабочих ID песен для Roblox на 7 суток.",
        "stars": 49,
        "bonus_searches": 0,
        "vip_days": 7,
        "forever": False,
    },
    "vip_30d": {
        "id": "vip_30d",
        "title": "💎 VIP Безлимит на 30 дней",
        "short_btn": "💎 Безлимит 30 дней — 99 ⭐",
        "description": "Полный безлимит на поиск любых песен в боте на 30 суток.",
        "stars": 99,
        "bonus_searches": 0,
        "vip_days": 30,
        "forever": False,
    },
    "vip_forever": {
        "id": "vip_forever",
        "title": "👑 VIP Безлимит НАВСЕГДА",
        "short_btn": "👑 Безлимит НАВСЕГДА — 249 ⭐",
        "description": "Пожизненный иммунитет к суточным лимитам поиска песен.",
        "stars": 249,
        "bonus_searches": 0,
        "vip_days": 0,
        "forever": True,
    },
}


def today_msk() -> str:
    return datetime.now(MSK_TZ).strftime("%Y-%m-%d")


def get_admin_ids() -> set[int]:
    admins = {ROOT_ADMIN_ID}
    raw = os.getenv("ADMIN_IDS", "")
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            admins.add(int(part))
    return admins


def is_admin(user_id: int) -> bool:
    return int(user_id) in get_admin_ids()


def get_bot_token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def get_webhook_secret() -> str:
    return os.getenv("WEBHOOK_SECRET", "").strip()


def get_turso_url() -> str:
    url = os.getenv("TURSO_DATABASE_URL", "").strip().rstrip("/")
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://") :]
    elif url.startswith("wss://"):
        url = "https://" + url[len("wss://") :]
    return url


def get_turso_token() -> str:
    return os.getenv("TURSO_AUTH_TOKEN", "").strip()


def get_roblox_api_key() -> str:
    return os.getenv("ROBLOX_API_KEY", "").strip()
