import unittest
from core.bot_handlers import _render_song_card
from core.config import DEFAULT_DAILY_FREE_LIMIT, ROOT_ADMIN_ID, is_admin, today_msk
from core.db import Database
from core.matcher import rank_songs, score_song_match
from core.music_resolver import (
    _compute_title_match_score,
    _strip_version_and_part_markers,
    append_version_tag,
    detect_version_tag,
)
from core.text_utils import build_song_search_index, clean_roblox_title


class FakeTurso:
    """Minimal dummy client for testing pure in-memory quota & ranking logic."""

    async def execute(self, sql, args=None):
        return []

    async def execute_batch(self, statements, client=None):
        return []


class CoreBotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(client=FakeTurso())  # type: ignore[arg-type]
        self.sample_songs = [
            {
                "asset_id": 48508580,
                "artist": "CUPSIZE",
                "title": "семнадцать ножевых",
                "roblox_name": "cpsze 17 nozhevih [working]",
                "duration_sec": 6.0,
                "search_index": build_song_search_index(
                    "CUPSIZE",
                    "семнадцать ножевых",
                    "cpsze 17 nozhevih 48508580",
                ),
                "confidence": 0.98,
                "updated_at": 100,
            },
            {
                "asset_id": 99112233,
                "artist": "Shadowraze",
                "title": "astral step",
                "roblox_name": "shadowraze astral step sped up",
                "duration_sec": 7.0,
                "search_index": build_song_search_index(
                    "Shadowraze",
                    "astral step",
                    "shadowraze astral step sped up 99112233",
                ),
                "confidence": 0.95,
                "updated_at": 90,
            },
        ]

    def test_root_admin_immunity(self) -> None:
        self.assertEqual(ROOT_ADMIN_ID, 5188800998)
        self.assertTrue(is_admin(5188800998))
        admin_user = {
            "user_id": 5188800998,
            "searches_today": 999,
            "usage_date": today_msk(),
            "bonus_searches": 0,
            "vip_until": 0,
            "vip_forever": 0,
        }
        quota = self.db.evaluate_quota(admin_user)
        self.assertTrue(quota["can_search"])
        self.assertTrue(quota["unlimited"])

    def test_daily_five_search_limit(self) -> None:
        self.assertEqual(DEFAULT_DAILY_FREE_LIMIT, 5)
        regular_user = {
            "user_id": 111222333,
            "searches_today": 4,
            "usage_date": today_msk(),
            "bonus_searches": 0,
            "vip_until": 0,
            "vip_forever": 0,
        }
        q_ok = self.db.evaluate_quota(regular_user)
        self.assertTrue(q_ok["can_search"])
        self.assertEqual(q_ok["free_left"], 1)

        regular_user["searches_today"] = 5
        q_blocked = self.db.evaluate_quota(regular_user)
        self.assertFalse(q_blocked["can_search"])
        self.assertEqual(q_blocked["free_left"], 0)

    def test_fuzzy_translit_and_number_matching(self) -> None:
        ranked_1 = rank_songs("капсайз 17 ножевых", self.sample_songs)
        self.assertTrue(len(ranked_1) >= 1)
        self.assertEqual(ranked_1[0]["asset_id"], 48508580)

        ranked_2 = rank_songs("капсайс семнацать", self.sample_songs)
        self.assertTrue(len(ranked_2) >= 1)
        self.assertEqual(ranked_2[0]["asset_id"], 48508580)

        score_id = score_song_match("48508580", self.sample_songs[0])
        self.assertEqual(score_id, 1.0)

    def test_song_card_shows_roblox_duration_and_copy_button(self) -> None:
        quota = {
            "can_search": True,
            "unlimited": False,
            "status_label": "🔎 Бесплатно сегодня: 4/5",
            "free_left": 4,
            "bonus_left": 0,
        }
        text, kb = _render_song_card(self.sample_songs[0], 0, 2, quota)
        self.assertIn("CUPSIZE - семнадцать ножевых", text)
        self.assertIn("ID песни: <code>48508580</code>", text)
        self.assertIn("⏱ Длина в Roblox: 0:06", text)
        copy_btn = kb[0][0]
        self.assertEqual(copy_btn["copy_text"]["text"], "48508580")

    def test_no_false_positive_cupsize_1_to_madkid_1_maya(self) -> None:
        # "cupsize 1" is an upload part #1, NOT "madk1d, CUPSIZE - 1 мая"!
        core_q, part_lbl = _strip_version_and_part_markers("cupsize 1")
        self.assertEqual(core_q, "cupsize")
        self.assertEqual(part_lbl, "#1")
        _, title_score = _compute_title_match_score(core_q, "madk1d, CUPSIZE", "1 мая")
        self.assertEqual(title_score, 0.0)

        # Even if "cupsize 1" wasn't stripped, single digit "1" must not match "1 мая"
        _, raw_title_score = _compute_title_match_score("cupsize 1", "madk1d, CUPSIZE", "1 мая")
        self.assertEqual(raw_title_score, 0.0)

        # Real "cupsize - 17 nozhevih (sped up)" MUST match "CUPSIZE - семнадцать ножевых" with (sped up)
        tag = detect_version_tag("cupsize - 17 nozhevih (sped up)")
        self.assertEqual(tag, "(sped up)")
        core_real, _ = _strip_version_and_part_markers("cupsize - 17 nozhevih (sped up)")
        _, real_title_sc = _compute_title_match_score(core_real, "CUPSIZE", "семнадцать ножевых")
        self.assertGreaterEqual(real_title_sc, 0.75)
        self.assertEqual(
            append_version_tag("семнадцать ножевых", tag),
            "семнадцать ножевых (sped up)",
        )

    def test_clean_roblox_title(self) -> None:
        cleaned = clean_roblox_title("[WORKING] CUPSIZE_семнадцать_ножевых (BYPASSED) .mp3")
        self.assertIn("CUPSIZE", cleaned)
        self.assertNotIn("WORKING", cleaned.upper())


if __name__ == "__main__":
    unittest.main()
