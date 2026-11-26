from typing import Any
import httpx
from core.config import get_bot_token

_CACHED_BOT_USERNAME: str = ""


class TelegramClient:
    def __init__(self, token: str | None = None) -> None:
        self.token = token or get_bot_token()
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в переменных окружения.")
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    async def api_call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{self.base_url}/{method}", json=payload)
            data = resp.json()
            return data

    async def get_bot_username(self) -> str:
        global _CACHED_BOT_USERNAME
        if _CACHED_BOT_USERNAME:
            return _CACHED_BOT_USERNAME
        res = await self.api_call("getMe", {})
        if res.get("ok"):
            _CACHED_BOT_USERNAME = res["result"].get("username", "")
        return _CACHED_BOT_USERNAME

    async def send_message(
        self,
        chat_id: int,
        text: str,
        inline_keyboard: list[list[dict[str, Any]]] | None = None,
        disable_preview: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }
        if inline_keyboard:
            payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
        res = await self.api_call("sendMessage", payload)
        # Fallback if a legacy client/API rejects copy_text button
        if not res.get("ok") and inline_keyboard and "copy_text" in str(inline_keyboard):
            fallback_kb = _strip_copy_buttons(inline_keyboard)
            payload["reply_markup"] = {"inline_keyboard": fallback_kb}
            res = await self.api_call("sendMessage", payload)
        return res

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        inline_keyboard: list[list[dict[str, Any]]] | None = None,
        disable_preview: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }
        if inline_keyboard:
            payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
        res = await self.api_call("editMessageText", payload)
        if not res.get("ok"):
            desc = str(res.get("description", "")).lower()
            if "message is not modified" in desc:
                return res
            if inline_keyboard and "copy_text" in str(inline_keyboard):
                payload["reply_markup"] = {"inline_keyboard": _strip_copy_buttons(inline_keyboard)}
                res = await self.api_call("editMessageText", payload)
        return res

    async def answer_callback(
        self,
        callback_query_id: str,
        text: str = "",
        show_alert: bool = False,
    ) -> None:
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text:
            payload["text"] = text[:195]
        await self.api_call("answerCallbackQuery", payload)

    async def send_star_invoice(
        self,
        chat_id: int,
        title: str,
        description: str,
        payload_tag: str,
        stars: int,
    ) -> dict[str, Any]:
        return await self.api_call(
            "sendInvoice",
            {
                "chat_id": chat_id,
                "title": title,
                "description": description,
                "payload": payload_tag,
                "provider_token": "",
                "currency": "XTR",
                "prices": [{"label": title, "amount": int(stars)}],
            },
        )

    async def answer_pre_checkout(
        self,
        pre_checkout_query_id: str,
        ok: bool = True,
        error_message: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "pre_checkout_query_id": pre_checkout_query_id,
            "ok": ok,
        }
        if not ok and error_message:
            payload["error_message"] = error_message
        return await self.api_call("answerPreCheckoutQuery", payload)

    async def set_webhook(self, webhook_url: str, secret_token: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "url": webhook_url,
            "allowed_updates": ["message", "callback_query", "pre_checkout_query"],
            "drop_pending_updates": True,
        }
        if secret_token:
            payload["secret_token"] = secret_token
        await self.api_call(
            "setMyCommands",
            {
                "commands": [
                    {"command": "start", "description": "🏠 Главное меню и поиск песен"},
                    {"command": "vip", "description": "⭐ Безлимитный поиск за Звёзды"},
                    {"command": "profile", "description": "👤 Мой профиль и лимиты"},
                ]
            },
        )
        return await self.api_call("setWebhook", payload)


def _strip_copy_buttons(kb: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    cleaned: list[list[dict[str, Any]]] = []
    for row in kb:
        new_row = [btn for btn in row if "copy_text" not in btn]
        if new_row:
            cleaned.append(new_row)
    return cleaned
