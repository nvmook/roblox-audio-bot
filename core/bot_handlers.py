from typing import Any
from core.config import (
    DEFAULT_DAILY_FREE_LIMIT,
    REFERRAL_BONUS_SEARCHES,
    STARS_PLANS,
    SUBMIT_SONG_BONUS_SEARCHES,
    is_admin,
)
from core.db import Database
from core.music_resolver import resolve_canonical_track
from core.roblox_api import check_roblox_audio_id
from core.telegram import TelegramClient
from core.text_utils import esc, format_song_display


def _main_menu_kb(user_id: int) -> list[list[dict[str, Any]]]:
    kb: list[list[dict[str, Any]]] = [
        [{"text": "🔎 Найти песню", "callback_data": "nav:search_prompt"}],
        [{"text": "⭐ Купить безлимит (Stars)", "callback_data": "nav:shop"}],
        [
            {"text": "👤 Профиль", "callback_data": "nav:profile"},
            {"text": "🎁 +Поиски за друга", "callback_data": "nav:ref"},
        ],
        [{"text": f"🎵 Сдать рабочий ID (+{SUBMIT_SONG_BONUS_SEARCHES} поиска)", "callback_data": "nav:submit"}],
    ]
    if is_admin(user_id):
        kb.append([{"text": "🛠 Админ-панель", "callback_data": "adm:menu"}])
    return kb


def _render_welcome_text(user: dict[str, Any], db: Database) -> str:
    quota = db.evaluate_quota(user)
    return (
        "🎧 <b>Поиск рабочих Roblox Audio ID</b>\n\n"
        "Просто напиши <b>исполнителя</b> или <b>название трека</b> прямо в чат "
        "(можно с опечатками или русскими буквами, например: <code>капсайз семнадцать ножевых</code>).\n\n"
        f"📊 Твой статус: <b>{esc(quota['status_label'])}</b>\n"
        "<i>💡 Если по запросу ничего не найдено — попытка поиска НЕ списывается!</i>"
    )


def _render_song_card(
    song: dict[str, Any],
    idx: int,
    total: int,
    quota: dict[str, Any],
) -> tuple[str, list[list[dict[str, Any]]]]:
    display_name = format_song_display(str(song.get("artist") or ""), str(song.get("title") or ""))
    asset_id = str(song["asset_id"])
    dur = float(song.get("duration_sec") or 0)
    dur_line = ""
    if dur > 0:
        total_sec = max(1, int(round(dur)))
        mins = total_sec // 60
        secs = total_sec % 60
        dur_line = f"\n⏱ Длина в Roblox: {mins}:{secs:02d}"

    text = (
        f"<b>{esc(display_name)}</b>\n"
        f"ID песни: <code>{esc(asset_id)}</code>"
        f"{dur_line}\n\n"
        f"<i>{esc(quota['status_label'])}</i>"
    )

    kb: list[list[dict[str, Any]]] = [
        [
            {
                "text": f"📋 Скопировать ID ({asset_id})",
                "copy_text": {"text": asset_id},
            }
        ]
    ]

    if total > 1:
        prev_idx = (idx - 1) % total
        next_idx = (idx + 1) % total
        kb.append(
            [
                {"text": "⬅️", "callback_data": f"pg:{prev_idx}"},
                {"text": f"{idx + 1} из {total}", "callback_data": "noop"},
                {"text": "➡️", "callback_data": f"pg:{next_idx}"},
            ]
        )

    bottom_row: list[dict[str, Any]] = [{"text": "🏠 Меню", "callback_data": "nav:home"}]
    if not quota["unlimited"]:
        bottom_row.append({"text": "⭐ Безлимит", "callback_data": "nav:shop"})
    kb.append(bottom_row)
    return text, kb


def _shop_screen(quota: dict[str, Any]) -> tuple[str, list[list[dict[str, Any]]]]:
    text = (
        "⭐ <b>Магазин безлимитного поиска</b>\n\n"
        f"Бесплатно даётся <b>{DEFAULT_DAILY_FREE_LIMIT} успешных поисков в сутки</b> "
        "(обновление в 00:00 МСК).\n"
        "Хочешь искать треки для бумбокса без ограничений? Оплата в 1 клик через <b>Telegram Stars</b>:\n\n"
        f"Сейчас у тебя: <b>{esc(quota['status_label'])}</b>"
    )
    kb: list[list[dict[str, Any]]] = []
    for plan_id, plan in STARS_PLANS.items():
        kb.append([{"text": plan["short_btn"], "callback_data": f"buy:{plan_id}"}])
    kb.append([{"text": "🔙 Назад в меню", "callback_data": "nav:home"}])
    return text, kb


async def handle_telegram_update(
    update: dict[str, Any],
    db: Database,
    tg: TelegramClient,
) -> None:
    # 1. Telegram Stars Pre-Checkout Query (must answer within 10 seconds)
    if "pre_checkout_query" in update:
        pcq = update["pre_checkout_query"]
        payload_tag = str(pcq.get("invoice_payload") or "")
        plan_id = payload_tag.split(":", 1)[0]
        if plan_id in STARS_PLANS:
            await tg.answer_pre_checkout(pcq["id"], ok=True)
        else:
            await tg.answer_pre_checkout(pcq["id"], ok=False, error_message="Тариф не найден.")
        return

    # 2. Inline Button Callback Query
    if "callback_query" in update:
        await _handle_callback(update["callback_query"], db, tg)
        return

    # 3. Message (commands, text search, successful Stars payment)
    if "message" in update:
        await _handle_message(update["message"], db, tg)


async def _handle_message(msg: dict[str, Any], db: Database, tg: TelegramClient) -> None:
    from_user = msg.get("from") or {}
    user_id = int(from_user.get("id") or 0)
    chat_id = int((msg.get("chat") or {}).get("id") or user_id)
    if not user_id:
        return

    username = str(from_user.get("username") or "")
    first_name = str(from_user.get("first_name") or "")

    # Check if this message is a completed Telegram Stars payment
    if "successful_payment" in msg:
        sp = msg["successful_payment"]
        payload_tag = str(sp.get("invoice_payload") or "")
        plan_id = payload_tag.split(":", 1)[0]
        charge_id = str(sp.get("telegram_payment_charge_id") or "")
        stars_paid = int(sp.get("total_amount") or 0)
        applied = await db.apply_star_payment(user_id, plan_id, charge_id, stars_paid)
        user, _ = await db.get_or_create_user(user_id, username, first_name)
        quota = db.evaluate_quota(user)
        plan_title = STARS_PLANS.get(plan_id, {}).get("title", "VIP")
        if applied:
            await tg.send_message(
                chat_id,
                f"🎉 <b>Оплата прошла успешно!</b>\n\n"
                f"Активировано: <b>{esc(plan_title)}</b>\n"
                f"Твой статус: <b>{esc(quota['status_label'])}</b>\n\n"
                "Пиши название любой песни в чат 👇",
                inline_keyboard=_main_menu_kb(user_id),
            )
        return

    text = str(msg.get("text") or "").strip()
    if not text:
        return

    # Parse /start referral payload if present
    referrer_id: int | None = None
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        if len(parts) > 1 and parts[1].startswith("ref_"):
            ref_raw = parts[1][4:].strip()
            if ref_raw.isdigit():
                referrer_id = int(ref_raw)

    user, ref_granted = await db.get_or_create_user(user_id, username, first_name, referrer_id)
    if ref_granted and referrer_id:
        try:
            await tg.send_message(
                referrer_id,
                f"🎁 По твоей ссылке зашёл друг! Начислено <b>+{REFERRAL_BONUS_SEARCHES} бонусных поиска</b>.",
            )
        except Exception:
            pass

    if text.startswith("/start"):
        await db.set_user_action(user_id, "")
        await tg.send_message(
            chat_id,
            _render_welcome_text(user, db),
            inline_keyboard=_main_menu_kb(user_id),
        )
        return

    if text.startswith("/vip"):
        quota = db.evaluate_quota(user)
        shop_txt, shop_kb = _shop_screen(quota)
        await tg.send_message(chat_id, shop_txt, inline_keyboard=shop_kb)
        return

    if text.startswith("/profile"):
        await _send_profile(chat_id, user, db, tg, edit_msg_id=None)
        return

    # Check stateful actions (user song submission / admin flows)
    pending = str(user.get("pending_action") or "")
    if pending == "await_submit_id" and not text.startswith("/"):
        await _process_user_song_submission(chat_id, user, text, db, tg)
        return

    if is_admin(user_id):
        if pending == "adm_add_song" and not text.startswith("/"):
            await _process_admin_add_song(chat_id, user_id, text, db, tg)
            return
        if pending == "adm_grant_vip" and not text.startswith("/"):
            await _process_admin_grant_vip(chat_id, user_id, text, db, tg)
            return

    # Default: Song Search!
    await _execute_user_search(chat_id, user, text, db, tg)


async def _execute_user_search(
    chat_id: int,
    user: dict[str, Any],
    query: str,
    db: Database,
    tg: TelegramClient,
) -> None:
    uid = int(user["user_id"])
    quota = db.evaluate_quota(user)

    if not quota["can_search"]:
        await tg.send_message(
            chat_id,
            "🔒 <b>Дневной лимит поисков исчерпан (5 из 5)!</b>\n\n"
            "Бесплатные поиски обновятся в <b>00:00 МСК</b>.\n"
            "Чтобы продолжить прямо сейчас — открой безлимит за <b>Telegram Stars ⭐</b> "
            "или пригласи друга!",
            inline_keyboard=[
                [{"text": p["short_btn"], "callback_data": f"buy:{pid}"}]
                for pid, p in STARS_PLANS.items()
            ]
            + [
                [{"text": "🎁 Получить +3 поиска за друга", "callback_data": "nav:ref"}],
                [{"text": "🏠 Главное меню", "callback_data": "nav:home"}],
            ],
        )
        return

    results = await db.search_songs(query, limit=15)

    # IMPORTANT RULE: If nothing found, DO NOT deduct search quota!
    if not results:
        await tg.send_message(
            chat_id,
            f"😔 По запросу «<b>{esc(query[:80])}</b>» пока ничего не найдено.\n\n"
            f"✅ <b>Попытка НЕ списана!</b> ({esc(quota['status_label'])})\n"
            "Попробуй написать название проще или только имя артиста.",
            inline_keyboard=[
                [{"text": "🏠 Главное меню", "callback_data": "nav:home"}],
            ],
        )
        return

    # Deduct 1 search ONLY because results > 0 (immune for Admin 5188800998 & VIPs)
    updated_quota = await db.record_successful_search(user, query)
    card_text, card_kb = _render_song_card(results[0], 0, len(results), updated_quota)
    await tg.send_message(chat_id, card_text, inline_keyboard=card_kb)


async def _handle_callback(cb: dict[str, Any], db: Database, tg: TelegramClient) -> None:
    cb_id = str(cb["id"])
    from_user = cb.get("from") or {}
    user_id = int(from_user.get("id") or 0)
    msg = cb.get("message") or {}
    chat_id = int((msg.get("chat") or {}).get("id") or user_id)
    msg_id = int(msg.get("message_id") or 0)
    data = str(cb.get("data") or "")

    if data == "noop":
        await tg.answer_callback(cb_id)
        return

    user, _ = await db.get_or_create_user(
        user_id,
        str(from_user.get("username") or ""),
        str(from_user.get("first_name") or ""),
    )

    if data == "nav:home":
        await db.set_user_action(user_id, "")
        await tg.answer_callback(cb_id)
        await tg.edit_message(
            chat_id,
            msg_id,
            _render_welcome_text(user, db),
            inline_keyboard=_main_menu_kb(user_id),
        )
        return

    if data == "nav:search_prompt":
        await db.set_user_action(user_id, "")
        await tg.answer_callback(cb_id)
        await tg.edit_message(
            chat_id,
            msg_id,
            "🔎 <b>Напиши название песни или автора в чат:</b>\n\n"
            "Примеры:\n"
            "• <code>CUPSIZE - семнадцать ножевых</code>\n"
            "• <code>капсайз 17</code>\n"
            "• <code>shadowraze</code>",
            inline_keyboard=[[{"text": "🔙 Назад", "callback_data": "nav:home"}]],
        )
        return

    if data == "nav:shop":
        await tg.answer_callback(cb_id)
        quota = db.evaluate_quota(user)
        shop_txt, shop_kb = _shop_screen(quota)
        await tg.edit_message(chat_id, msg_id, shop_txt, inline_keyboard=shop_kb)
        return

    if data == "nav:profile":
        await tg.answer_callback(cb_id)
        await _send_profile(chat_id, user, db, tg, edit_msg_id=msg_id)
        return

    if data == "nav:ref":
        await tg.answer_callback(cb_id)
        bot_uname = await tg.get_bot_username()
        ref_link = f"https://t.me/{bot_uname}?start=ref_{user_id}" if bot_uname else "Настрой токен бота"
        txt = (
            "🎁 <b>Бесплатные поиски за друзей</b>\n\n"
            f"За каждого друга, который впервые запустит бота по твоей ссылке, "
            f"ты сразу получаешь <b>+{REFERRAL_BONUS_SEARCHES} бонусных поиска</b> (они не сгорают в полночь)!\n\n"
            f"🔗 Твоя ссылка:\n<code>{esc(ref_link)}</code>"
        )
        kb = [
            [{"text": "📋 Скопировать ссылку", "copy_text": {"text": ref_link}}],
            [{"text": "🔙 Назад", "callback_data": "nav:home"}],
        ]
        await tg.edit_message(chat_id, msg_id, txt, inline_keyboard=kb)
        return

    if data == "nav:submit":
        await db.set_user_action(user_id, "await_submit_id")
        await tg.answer_callback(cb_id)
        await tg.edit_message(
            chat_id,
            msg_id,
            "🎵 <b>Сдать рабочий Roblox Audio ID</b>\n\n"
            "Отправь числовой ID песни из Roblox (можно также подписать трек через черту:\n"
            "<code>48508580 | CUPSIZE - семнадцать ножевых</code>).\n\n"
            f"Бот проверит существование аудио в Roblox и начислит <b>+{SUBMIT_SONG_BONUS_SEARCHES} бонусных поиска</b>!",
            inline_keyboard=[[{"text": "❌ Отмена", "callback_data": "nav:home"}]],
        )
        return

    # Pagination across search results (does NOT charge extra searches!)
    if data.startswith("pg:"):
        page_str = data[3:]
        last_q = str(user.get("last_query") or "").strip()
        if not page_str.isdigit() or not last_q:
            await tg.answer_callback(cb_id, "Напиши запрос заново в чат.")
            return
        results = await db.search_songs(last_q, limit=15)
        if not results:
            await tg.answer_callback(cb_id, "Результаты устарели.")
            return
        idx = int(page_str) % len(results)
        quota = db.evaluate_quota(user)
        card_text, card_kb = _render_song_card(results[idx], idx, len(results), quota)
        await tg.answer_callback(cb_id)
        await tg.edit_message(chat_id, msg_id, card_text, inline_keyboard=card_kb)
        return

    # Stars Invoice Purchase
    if data.startswith("buy:"):
        plan_id = data[4:]
        plan = STARS_PLANS.get(plan_id)
        if not plan:
            await tg.answer_callback(cb_id, "Тариф не найден", show_alert=True)
            return
        await tg.answer_callback(cb_id, "Формирую счёт в Telegram Stars ⭐...")
        await tg.send_star_invoice(
            chat_id=chat_id,
            title=plan["title"],
            description=plan["description"],
            payload_tag=f"{plan_id}:{user_id}",
            stars=plan["stars"],
        )
        return

    # Admin Panel (strictly guarded by is_admin)
    if data.startswith("adm:"):
        if not is_admin(user_id):
            await tg.answer_callback(cb_id, "⛔ Доступ только для администратора.", show_alert=True)
            return
        await _handle_admin_callback(cb_id, chat_id, msg_id, user_id, data, db, tg)


async def _send_profile(
    chat_id: int,
    user: dict[str, Any],
    db: Database,
    tg: TelegramClient,
    edit_msg_id: int | None,
) -> None:
    uid = int(user["user_id"])
    quota = db.evaluate_quota(user)
    total_s = int(user.get("total_searches") or 0)
    txt = (
        "👤 <b>Твой профиль</b>\n\n"
        f"🆔 Telegram ID: <code>{uid}</code>\n"
        f"📊 Статус: <b>{esc(quota['status_label'])}</b>\n"
        f"🔎 Всего найдено песен: <b>{total_s}</b>"
    )
    kb = [
        [{"text": "⭐ Купить безлимит", "callback_data": "nav:shop"}],
        [{"text": "🔙 Назад в меню", "callback_data": "nav:home"}],
    ]
    if edit_msg_id:
        await tg.edit_message(chat_id, edit_msg_id, txt, inline_keyboard=kb)
    else:
        await tg.send_message(chat_id, txt, inline_keyboard=kb)


async def _process_user_song_submission(
    chat_id: int,
    user: dict[str, Any],
    text: str,
    db: Database,
    tg: TelegramClient,
) -> None:
    uid = int(user["user_id"])
    raw_id_part = text.split("|", 1)[0].strip()
    custom_label = text.split("|", 1)[1].strip() if "|" in text else ""

    if not raw_id_part.isdigit() or len(raw_id_part) < 5:
        await tg.send_message(
            chat_id,
            "⚠️ Введи корректный числовой ID аудио из Roblox (только цифры, либо <code>ID | Артист - Песня</code>).",
            inline_keyboard=[[{"text": "❌ Отмена", "callback_data": "nav:home"}]],
        )
        return

    asset_id = int(raw_id_part)
    existing = await db.turso.execute("SELECT asset_id FROM songs WHERE asset_id = ?", [asset_id])
    if existing:
        await db.set_user_action(uid, "")
        await tg.send_message(
            chat_id,
            f"ℹ️ Песня с ID <code>{asset_id}</code> уже есть в нашей базе!",
            inline_keyboard=_main_menu_kb(uid),
        )
        return

    roblox_meta = await check_roblox_audio_id(asset_id)
    if not roblox_meta:
        await tg.send_message(
            chat_id,
            f"❌ ID <code>{asset_id}</code> не существует в Roblox, удалён модерацией или это не аудиофайл.",
            inline_keyboard=[[{"text": "❌ Отмена", "callback_data": "nav:home"}]],
        )
        return

    resolved = await resolve_canonical_track(
        custom_label or roblox_meta["name"],
        roblox_meta.get("creator", ""),
    )
    await db.upsert_song(
        asset_id=asset_id,
        artist=resolved["artist"],
        title=resolved["title"],
        roblox_name=roblox_meta.get("raw_name") or roblox_meta["name"],
        roblox_creator=roblox_meta.get("creator", ""),
        duration_sec=float(roblox_meta.get("duration_sec") or 0.0),
        confidence=resolved["confidence"],
        source="user_submit",
    )
    rewarded = await db.reward_user_song_submit(uid)
    await db.set_user_action(uid, "")

    disp = format_song_display(resolved["artist"], resolved["title"])
    bonus_msg = (
        f"\n🎁 Начислено <b>+{SUBMIT_SONG_BONUS_SEARCHES} бонусных поиска</b>!"
        if rewarded
        else "\n(Дневной лимит наград за добавление достигнут, спасибо за вклад!)"
    )
    await tg.send_message(
        chat_id,
        f"✅ Аудио проверено в Roblox и сохранено:\n"
        f"<b>{esc(disp)}</b>\nID песни: <code>{asset_id}</code>{bonus_msg}",
        inline_keyboard=_main_menu_kb(uid),
    )


async def _handle_admin_callback(
    cb_id: str,
    chat_id: int,
    msg_id: int,
    admin_id: int,
    data: str,
    db: Database,
    tg: TelegramClient,
) -> None:
    if data in ("adm:menu", "adm:stats"):
        await db.set_user_action(admin_id, "")
        stats = await db.get_admin_stats()
        await tg.answer_callback(cb_id)
        txt = (
            "🛠 <b>Админ-панель (Иммунитет активен)</b>\n\n"
            f"🎵 Активных песен в базе: <b>{stats['songs_count']}</b>\n"
            f"👥 Пользователей бота: <b>{stats['users_count']}</b>\n"
            f"💎 Активных VIP: <b>{stats['vip_count']}</b>\n"
            f"⭐ Заработано Звёзд: <b>{stats['stars_earned']} ⭐</b> ({stats['payments_count']} оплат)\n\n"
            "🤖 <b>Сканер GitHub Actions:</b>\n"
            f"• Проверено ID всего: <code>{esc(str(stats['total_scanned']))}</code>\n"
            f"• Последний курсор ID: <code>{esc(str(stats['last_asset_id']))}</code>\n"
            f"• Последний ран: <code>{esc(str(stats['last_run_at']))}</code>"
        )
        kb = [
            [{"text": "🔄 Обновить статистику", "callback_data": "adm:stats"}],
            [{"text": "➕ Добавить песню вручную", "callback_data": "adm:add_song"}],
            [{"text": "👑 Выдать вечный VIP по ID", "callback_data": "adm:grant_vip"}],
            [{"text": "🔙 В главное меню", "callback_data": "nav:home"}],
        ]
        await tg.edit_message(chat_id, msg_id, txt, inline_keyboard=kb)
        return

    if data == "adm:add_song":
        await db.set_user_action(admin_id, "adm_add_song")
        await tg.answer_callback(cb_id)
        await tg.edit_message(
            chat_id,
            msg_id,
            "➕ <b>Добавление песни в базу</b>\n\n"
            "Отправь строку в формате:\n"
            "<code>48508580 | CUPSIZE - семнадцать ножевых</code>\n\n"
            "Или просто числовой ID — бот сам проверит его в Roblox и определит трек.",
            inline_keyboard=[[{"text": "🔙 Назад в админку", "callback_data": "adm:menu"}]],
        )
        return

    if data == "adm:grant_vip":
        await db.set_user_action(admin_id, "adm_grant_vip")
        await tg.answer_callback(cb_id)
        await tg.edit_message(
            chat_id,
            msg_id,
            "👑 <b>Выдача вечного VIP</b>\n\nОтправь числовой Telegram ID пользователя:",
            inline_keyboard=[[{"text": "🔙 Назад в админку", "callback_data": "adm:menu"}]],
        )
        return


async def _process_admin_add_song(
    chat_id: int,
    admin_id: int,
    text: str,
    db: Database,
    tg: TelegramClient,
) -> None:
    parts = [p.strip() for p in text.split("|", 1)]
    if not parts[0].isdigit():
        await tg.send_message(
            chat_id,
            "⚠️ Первым до <code>|</code> должен идти числовой ID песни.",
            inline_keyboard=[[{"text": "🔙 В админку", "callback_data": "adm:menu"}]],
        )
        return

    asset_id = int(parts[0])
    roblox_dur = 0.0
    if len(parts) > 1 and parts[1]:
        raw_label = parts[1]
        if " - " in raw_label:
            artist, title = [x.strip() for x in raw_label.split(" - ", 1)]
        else:
            artist, title = "", raw_label
        r_name = raw_label
        meta = await check_roblox_audio_id(asset_id)
        if meta:
            roblox_dur = float(meta.get("duration_sec") or 0.0)
    else:
        meta = await check_roblox_audio_id(asset_id)
        if not meta:
            await tg.send_message(
                chat_id,
                f"❌ В Roblox не найдено живого аудио с ID <code>{asset_id}</code>. "
                "Если хочешь добавить принудительно, напиши: <code>ID | Артист - Название</code>.",
                inline_keyboard=[[{"text": "🔙 В админку", "callback_data": "adm:menu"}]],
            )
            return
        resolved = await resolve_canonical_track(meta["name"], meta.get("creator", ""))
        artist, title = resolved["artist"], resolved["title"]
        r_name = meta.get("raw_name") or meta["name"]
        roblox_dur = float(meta.get("duration_sec") or 0.0)

    await db.upsert_song(
        asset_id=asset_id,
        artist=artist,
        title=title,
        roblox_name=r_name,
        duration_sec=roblox_dur,
        source="admin",
    )
    await db.set_user_action(admin_id, "")
    disp = format_song_display(artist, title)
    await tg.send_message(
        chat_id,
        f"✅ Сохранено в базу:\n<b>{esc(disp)}</b>\nID песни: <code>{asset_id}</code>",
        inline_keyboard=[[{"text": "🛠 В админ-панель", "callback_data": "adm:menu"}]],
    )


async def _process_admin_grant_vip(
    chat_id: int,
    admin_id: int,
    text: str,
    db: Database,
    tg: TelegramClient,
) -> None:
    if not text.isdigit():
        await tg.send_message(
            chat_id,
            "⚠️ Введи числовой Telegram ID.",
            inline_keyboard=[[{"text": "🔙 В админку", "callback_data": "adm:menu"}]],
        )
        return
    target_id = int(text)
    await db.grant_vip_admin(target_id, forever=True)
    await db.set_user_action(admin_id, "")
    await tg.send_message(
        chat_id,
        f"👑 Пользователю <code>{target_id}</code> выдан вечный VIP-безлимит!",
        inline_keyboard=[[{"text": "🛠 В админ-панель", "callback_data": "adm:menu"}]],
    )
