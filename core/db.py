import time
from typing import Any
from core.config import (
    DEFAULT_DAILY_FREE_LIMIT,
    MAX_DAILY_SUBMIT_REWARDS,
    REFERRAL_BONUS_SEARCHES,
    STARS_PLANS,
    SUBMIT_SONG_BONUS_SEARCHES,
    is_admin,
    today_msk,
)
from core.matcher import rank_songs
from core.text_utils import (
    build_song_search_index,
    expand_number_tokens,
    normalize_basic,
    translit_variants,
)
from core.turso import TursoClient

SCHEMA_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS songs (
        asset_id INTEGER PRIMARY KEY,
        artist TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL,
        roblox_name TEXT NOT NULL DEFAULT '',
        roblox_creator TEXT NOT NULL DEFAULT '',
        duration_sec REAL NOT NULL DEFAULT 0,
        search_index TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 1.0,
        is_active INTEGER NOT NULL DEFAULT 1,
        source TEXT NOT NULL DEFAULT 'scanner',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_songs_active_updated ON songs(is_active, updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT NOT NULL DEFAULT '',
        first_name TEXT NOT NULL DEFAULT '',
        searches_today INTEGER NOT NULL DEFAULT 0,
        usage_date TEXT NOT NULL DEFAULT '',
        bonus_searches INTEGER NOT NULL DEFAULT 0,
        vip_until INTEGER NOT NULL DEFAULT 0,
        vip_forever INTEGER NOT NULL DEFAULT 0,
        referred_by INTEGER,
        submits_today INTEGER NOT NULL DEFAULT 0,
        submit_date TEXT NOT NULL DEFAULT '',
        pending_action TEXT NOT NULL DEFAULT '',
        last_query TEXT NOT NULL DEFAULT '',
        total_searches INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        charge_id TEXT UNIQUE NOT NULL,
        user_id INTEGER NOT NULL,
        plan_id TEXT NOT NULL,
        stars_amount INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scanner_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
]


class Database:
    def __init__(self, client: TursoClient | None = None) -> None:
        self.turso = client or TursoClient()

    async def init_schema(self) -> None:
        batch = [(sql.strip(), []) for sql in SCHEMA_STATEMENTS]
        await self.turso.execute_batch(batch)

    # -------------------- USERS & QUOTAS --------------------

    async def get_or_create_user(
        self,
        user_id: int,
        username: str = "",
        first_name: str = "",
        referrer_id: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Returns (user_dict, is_newly_referred_bonus_granted)."""
        now = int(time.time())
        today = today_msk()
        rows = await self.turso.execute("SELECT * FROM users WHERE user_id = ?", [user_id])

        if rows:
            user = rows[0]
            # Normalize daily counters in memory if date rolled over in MSK
            if user.get("usage_date") != today:
                user["searches_today"] = 0
                user["usage_date"] = today
            if user.get("submit_date") != today:
                user["submits_today"] = 0
                user["submit_date"] = today
            if (username and username != user.get("username")) or (
                first_name and first_name != user.get("first_name")
            ):
                await self.turso.execute(
                    "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                    [username or "", first_name or "", user_id],
                )
                user["username"] = username or ""
                user["first_name"] = first_name or ""
            return user, False

        valid_ref: int | None = None
        ref_granted = False
        if referrer_id and referrer_id != user_id:
            ref_rows = await self.turso.execute(
                "SELECT user_id FROM users WHERE user_id = ?",
                [referrer_id],
            )
            if ref_rows:
                valid_ref = referrer_id

        stmts: list[tuple[str, list[Any]]] = [
            (
                """
                INSERT OR IGNORE INTO users (
                    user_id, username, first_name, searches_today, usage_date,
                    bonus_searches, vip_until, vip_forever, referred_by,
                    submits_today, submit_date, pending_action, last_query,
                    total_searches, created_at
                ) VALUES (?, ?, ?, 0, ?, 0, 0, 0, ?, 0, ?, '', '', 0, ?)
                """,
                [user_id, username or "", first_name or "", today, valid_ref, today, now],
            )
        ]
        if valid_ref:
            stmts.append(
                (
                    "UPDATE users SET bonus_searches = bonus_searches + ? WHERE user_id = ?",
                    [REFERRAL_BONUS_SEARCHES, valid_ref],
                )
            )
            ref_granted = True

        await self.turso.execute_batch(stmts)
        created_rows = await self.turso.execute("SELECT * FROM users WHERE user_id = ?", [user_id])
        user_out = created_rows[0] if created_rows else {
            "user_id": user_id,
            "username": username,
            "first_name": first_name,
            "searches_today": 0,
            "usage_date": today,
            "bonus_searches": 0,
            "vip_until": 0,
            "vip_forever": 0,
            "referred_by": valid_ref,
            "submits_today": 0,
            "submit_date": today,
            "pending_action": "",
            "last_query": "",
            "total_searches": 0,
            "created_at": now,
        }
        return user_out, ref_granted

    def evaluate_quota(self, user: dict[str, Any]) -> dict[str, Any]:
        uid = int(user["user_id"])
        now = int(time.time())
        today = today_msk()

        if is_admin(uid):
            return {
                "can_search": True,
                "unlimited": True,
                "status_label": "👑 Админ (иммунитет к лимитам)",
                "free_left": DEFAULT_DAILY_FREE_LIMIT,
                "bonus_left": int(user.get("bonus_searches") or 0),
            }

        if int(user.get("vip_forever") or 0) == 1:
            return {
                "can_search": True,
                "unlimited": True,
                "status_label": "👑 VIP Безлимит НАВСЕГДА",
                "free_left": DEFAULT_DAILY_FREE_LIMIT,
                "bonus_left": int(user.get("bonus_searches") or 0),
            }

        vip_until = int(user.get("vip_until") or 0)
        if vip_until > now:
            days_left = max(1, (vip_until - now + 86399) // 86400)
            return {
                "can_search": True,
                "unlimited": True,
                "status_label": f"💎 VIP Безлимит (ещё {days_left} дн.)",
                "free_left": DEFAULT_DAILY_FREE_LIMIT,
                "bonus_left": int(user.get("bonus_searches") or 0),
            }

        used_today = int(user.get("searches_today") or 0) if user.get("usage_date") == today else 0
        free_left = max(0, DEFAULT_DAILY_FREE_LIMIT - used_today)
        bonus_left = max(0, int(user.get("bonus_searches") or 0))
        can_search = (free_left + bonus_left) > 0

        status = f"🔎 Бесплатно сегодня: {free_left}/{DEFAULT_DAILY_FREE_LIMIT}"
        if bonus_left > 0:
            status += f" (+{bonus_left} бонусных)"

        return {
            "can_search": can_search,
            "unlimited": False,
            "status_label": status,
            "free_left": free_left,
            "bonus_left": bonus_left,
        }

    async def record_successful_search(self, user: dict[str, Any], query: str) -> dict[str, Any]:
        """Deducts 1 search ONLY when songs were actually found. Admins & VIPs are immune."""
        uid = int(user["user_id"])
        today = today_msk()
        quota = self.evaluate_quota(user)

        if quota["unlimited"]:
            await self.turso.execute(
                "UPDATE users SET last_query = ?, pending_action = '', total_searches = total_searches + 1 WHERE user_id = ?",
                [query, uid],
            )
            user["last_query"] = query
            user["pending_action"] = ""
            return self.evaluate_quota(user)

        used_today = int(user.get("searches_today") or 0) if user.get("usage_date") == today else 0
        bonus_left = max(0, int(user.get("bonus_searches") or 0))

        if used_today < DEFAULT_DAILY_FREE_LIMIT:
            new_used = used_today + 1
            await self.turso.execute(
                """
                UPDATE users
                SET searches_today = ?, usage_date = ?, last_query = ?, pending_action = '',
                    total_searches = total_searches + 1
                WHERE user_id = ?
                """,
                [new_used, today, query, uid],
            )
            user["searches_today"] = new_used
            user["usage_date"] = today
        elif bonus_left > 0:
            new_bonus = bonus_left - 1
            await self.turso.execute(
                """
                UPDATE users
                SET bonus_searches = ?, usage_date = ?, searches_today = ?, last_query = ?,
                    pending_action = '', total_searches = total_searches + 1
                WHERE user_id = ?
                """,
                [new_bonus, today, used_today, query, uid],
            )
            user["bonus_searches"] = new_bonus
            user["usage_date"] = today
            user["searches_today"] = used_today

        user["last_query"] = query
        user["pending_action"] = ""
        return self.evaluate_quota(user)

    async def set_user_action(self, user_id: int, action: str) -> None:
        await self.turso.execute(
            "UPDATE users SET pending_action = ? WHERE user_id = ?",
            [action, user_id],
        )

    async def apply_star_payment(
        self,
        user_id: int,
        plan_id: str,
        charge_id: str,
        stars_amount: int,
    ) -> bool:
        """Idempotently records Telegram Stars payment and grants VIP or bonus searches."""
        existing = await self.turso.execute(
            "SELECT id FROM payments WHERE charge_id = ?",
            [charge_id],
        )
        if existing:
            return False

        plan = STARS_PLANS.get(plan_id)
        if not plan:
            return False

        now = int(time.time())
        await self.get_or_create_user(user_id)
        rows = await self.turso.execute("SELECT * FROM users WHERE user_id = ?", [user_id])
        current_vip_until = int(rows[0].get("vip_until") or 0) if rows else 0

        stmts: list[tuple[str, list[Any]]] = [
            (
                "INSERT INTO payments (charge_id, user_id, plan_id, stars_amount, created_at) VALUES (?, ?, ?, ?, ?)",
                [charge_id, user_id, plan_id, stars_amount, now],
            )
        ]

        if plan["forever"]:
            stmts.append(("UPDATE users SET vip_forever = 1 WHERE user_id = ?", [user_id]))
        elif plan["vip_days"] > 0:
            base_ts = max(now, current_vip_until)
            new_until = base_ts + plan["vip_days"] * 86400
            stmts.append(("UPDATE users SET vip_until = ? WHERE user_id = ?", [new_until, user_id]))
        elif plan["bonus_searches"] > 0:
            stmts.append(
                (
                    "UPDATE users SET bonus_searches = bonus_searches + ? WHERE user_id = ?",
                    [plan["bonus_searches"], user_id],
                )
            )

        await self.turso.execute_batch(stmts)
        return True

    async def grant_vip_admin(self, target_user_id: int, days: int = 30, forever: bool = False) -> None:
        await self.get_or_create_user(target_user_id)
        now = int(time.time())
        if forever:
            await self.turso.execute(
                "UPDATE users SET vip_forever = 1 WHERE user_id = ?",
                [target_user_id],
            )
        else:
            await self.turso.execute(
                "UPDATE users SET vip_until = ? WHERE user_id = ?",
                [now + days * 86400, target_user_id],
            )

    # -------------------- SONGS & FUZZY SEARCH --------------------

    async def upsert_song(
        self,
        asset_id: int,
        artist: str,
        title: str,
        roblox_name: str = "",
        roblox_creator: str = "",
        duration_sec: float = 0.0,
        confidence: float = 1.0,
        source: str = "scanner",
        is_active: bool = True,
    ) -> None:
        now = int(time.time())
        idx = build_song_search_index(artist, title, f"{roblox_name} {roblox_creator} {asset_id}")
        await self.turso.execute(
            """
            INSERT INTO songs (
                asset_id, artist, title, roblox_name, roblox_creator,
                duration_sec, search_index, confidence, is_active, source,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                artist = excluded.artist,
                title = excluded.title,
                roblox_name = CASE WHEN excluded.roblox_name != '' THEN excluded.roblox_name ELSE songs.roblox_name END,
                roblox_creator = CASE WHEN excluded.roblox_creator != '' THEN excluded.roblox_creator ELSE songs.roblox_creator END,
                duration_sec = CASE WHEN excluded.duration_sec > 0 THEN excluded.duration_sec ELSE songs.duration_sec END,
                search_index = excluded.search_index,
                confidence = excluded.confidence,
                is_active = excluded.is_active,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            [
                int(asset_id),
                artist.strip(),
                title.strip(),
                roblox_name.strip(),
                roblox_creator.strip(),
                float(duration_sec),
                idx,
                float(confidence),
                1 if is_active else 0,
                source,
                now,
                now,
            ],
        )

    async def upsert_songs_batch(self, songs: list[dict[str, Any]]) -> int:
        if not songs:
            return 0
        now = int(time.time())
        stmts: list[tuple[str, list[Any]]] = []
        sql = """
            INSERT INTO songs (
                asset_id, artist, title, roblox_name, roblox_creator,
                duration_sec, search_index, confidence, is_active, source,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                artist = excluded.artist,
                title = excluded.title,
                roblox_name = CASE WHEN excluded.roblox_name != '' THEN excluded.roblox_name ELSE songs.roblox_name END,
                roblox_creator = CASE WHEN excluded.roblox_creator != '' THEN excluded.roblox_creator ELSE songs.roblox_creator END,
                duration_sec = CASE WHEN excluded.duration_sec > 0 THEN excluded.duration_sec ELSE songs.duration_sec END,
                search_index = excluded.search_index,
                confidence = excluded.confidence,
                is_active = excluded.is_active,
                source = excluded.source,
                updated_at = excluded.updated_at
        """
        for s in songs:
            aid = int(s["asset_id"])
            artist = str(s.get("artist") or "").strip()
            title = str(s.get("title") or "").strip()
            r_name = str(s.get("roblox_name") or "").strip()
            r_creator = str(s.get("roblox_creator") or "").strip()
            idx = build_song_search_index(artist, title, f"{r_name} {r_creator} {aid}")
            stmts.append(
                (
                    sql,
                    [
                        aid,
                        artist,
                        title,
                        r_name,
                        r_creator,
                        float(s.get("duration_sec") or 0.0),
                        idx,
                        float(s.get("confidence") or 1.0),
                        1 if s.get("is_active", True) else 0,
                        str(s.get("source") or "scanner"),
                        now,
                        now,
                    ],
                )
            )
        await self.turso.execute_batch(stmts)
        return len(stmts)

    async def reward_user_song_submit(self, user_id: int) -> bool:
        """Grants bonus searches for submitting a new verified Roblox Audio ID (capped daily)."""
        today = today_msk()
        rows = await self.turso.execute(
            "SELECT submits_today, submit_date FROM users WHERE user_id = ?",
            [user_id],
        )
        if not rows:
            return False
        submits = int(rows[0].get("submits_today") or 0) if rows[0].get("submit_date") == today else 0
        if submits >= MAX_DAILY_SUBMIT_REWARDS:
            return False
        await self.turso.execute(
            """
            UPDATE users
            SET bonus_searches = bonus_searches + ?, submits_today = ?, submit_date = ?, pending_action = ''
            WHERE user_id = ?
            """,
            [SUBMIT_SONG_BONUS_SEARCHES, submits + 1, today, user_id],
        )
        return True

    async def search_songs(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        q_norm = normalize_basic(query)
        if not q_norm:
            return []

        # 1. Exact Asset ID lookup if query is numeric
        if q_norm.isdigit() and len(q_norm) >= 5:
            exact = await self.turso.execute(
                "SELECT * FROM songs WHERE asset_id = ? AND is_active = 1",
                [int(q_norm)],
            )
            if exact:
                return [{**exact[0], "match_score": 1.0}]

        # 2. Build broad SQL candidate filter using translit tokens + 3-char root prefixes (for typo tolerance)
        variant_tokens = translit_variants(q_norm).split()
        expanded_nums = expand_number_tokens(q_norm.split())
        probe_terms: list[str] = []

        for tok in [*expanded_nums, *variant_tokens]:
            if len(tok) >= 2 and tok not in probe_terms:
                probe_terms.append(tok)
            if len(tok) >= 4:
                prefix = tok[:3]
                if prefix not in probe_terms:
                    probe_terms.append(prefix)
            if len(probe_terms) >= 14:
                break

        candidates: list[dict[str, Any]] = []
        if probe_terms:
            like_clauses = " OR ".join("search_index LIKE ?" for _ in probe_terms)
            like_args: list[Any] = [f"%{t}%" for t in probe_terms]
            sql = f"SELECT * FROM songs WHERE is_active = 1 AND ({like_clauses}) ORDER BY updated_at DESC LIMIT 250"
            candidates = await self.turso.execute(sql, like_args)

        # Fallback for severe first-letter typos on compact catalogs
        if len(candidates) < 10:
            extra = await self.turso.execute(
                "SELECT * FROM songs WHERE is_active = 1 ORDER BY updated_at DESC LIMIT 200"
            )
            seen_ids = {int(c["asset_id"]) for c in candidates}
            for row in extra:
                if int(row["asset_id"]) not in seen_ids:
                    candidates.append(row)

        return rank_songs(query, candidates, min_score=0.44, limit=limit)

    # -------------------- SCANNER STATE & STATS --------------------

    async def get_scanner_value(self, key: str, default: str = "") -> str:
        rows = await self.turso.execute("SELECT value FROM scanner_state WHERE key = ?", [key])
        return str(rows[0]["value"]) if rows else default

    async def set_scanner_values(self, mapping: dict[str, str | int]) -> None:
        now = int(time.time())
        stmts = [
            (
                """
                INSERT INTO scanner_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                [k, str(v), now],
            )
            for k, v in mapping.items()
        ]
        if stmts:
            await self.turso.execute_batch(stmts)

    async def get_admin_stats(self) -> dict[str, Any]:
        batch_res = await self.turso.execute_batch(
            [
                ("SELECT COUNT(*) AS cnt FROM songs WHERE is_active = 1", []),
                ("SELECT COUNT(*) AS cnt FROM users", []),
                ("SELECT COUNT(*) AS cnt FROM users WHERE vip_forever = 1 OR vip_until > ?", [int(time.time())]),
                ("SELECT COALESCE(SUM(stars_amount), 0) AS stars, COUNT(*) AS cnt FROM payments", []),
                ("SELECT key, value FROM scanner_state", []),
            ]
        )
        songs_cnt = int(batch_res[0][0]["cnt"]) if batch_res[0] else 0
        users_cnt = int(batch_res[1][0]["cnt"]) if batch_res[1] else 0
        vip_cnt = int(batch_res[2][0]["cnt"]) if batch_res[2] else 0
        stars_sum = int(batch_res[3][0]["stars"]) if batch_res[3] else 0
        pay_cnt = int(batch_res[3][0]["cnt"]) if batch_res[3] else 0
        state_map = {r["key"]: r["value"] for r in batch_res[4]} if len(batch_res) > 4 else {}

        return {
            "songs_count": songs_cnt,
            "users_count": users_cnt,
            "vip_count": vip_cnt,
            "stars_earned": stars_sum,
            "payments_count": pay_cnt,
            "last_asset_id": state_map.get("last_asset_id", "—"),
            "total_scanned": state_map.get("total_scanned", "0"),
            "last_run_at": state_map.get("last_run_at", "—"),
        }
