import hmac
import logging
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from fastapi import FastAPI, Header, HTTPException, Query, Request
from core.bot_handlers import handle_telegram_update
from core.config import get_webhook_secret
from core.db import Database
from core.telegram import TelegramClient

logger = logging.getLogger("roblox_audio_bot")


class VercelPathRestoreMiddleware:
    """Restores original request path on Vercel serverless rewrites before FastAPI routing."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            raw_qs = scope.get("query_string", b"").decode("utf-8", errors="ignore")
            cur_path = scope.get("path", "")
            method = scope.get("method", "GET").upper()

            if "__path=" in raw_qs:
                parsed = parse_qs(raw_qs, keep_blank_values=True)
                path_vals = parsed.pop("__path", None)
                if path_vals:
                    restored = "/" + path_vals[0].lstrip("/")
                    scope = dict(scope)
                    scope["path"] = restored
                    scope["raw_path"] = restored.encode("utf-8")
                    scope["query_string"] = urlencode(parsed, doseq=True).encode("utf-8")
            elif cur_path in ("/api/index", "/index", "/api/index.py"):
                scope = dict(scope)
                if method == "POST":
                    restored = "/api/webhook"
                elif "secret=" in raw_qs:
                    restored = "/api/setup"
                else:
                    restored = "/api/health"
                scope["path"] = restored
                scope["raw_path"] = restored.encode("utf-8")

        await self.app(scope, receive, send)


app = FastAPI(title="Roblox Audio Finder Bot", docs_url=None, redoc_url=None)
app.add_middleware(VercelPathRestoreMiddleware)


@app.get("/")
@app.get("/health")
@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "roblox-audio-bot"}


@app.get("/setup")
@app.get("/api/setup")
async def setup_webhook(
    request: Request,
    secret: str = Query(default="", description="Must match WEBHOOK_SECRET env var"),
) -> dict[str, Any]:
    expected = get_webhook_secret()
    if not expected:
        raise HTTPException(
            status_code=500,
            detail="WEBHOOK_SECRET не задан в Environment Variables на Vercel.",
        )
    if not secret or not hmac.compare_digest(secret, expected):
        raise HTTPException(
            status_code=403,
            detail="Неверный параметр ?secret=... (должен совпадать с WEBHOOK_SECRET на Vercel).",
        )

    db = Database()
    await db.init_schema()

    tg = TelegramClient()
    forwarded_proto = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    webhook_url = f"{forwarded_proto}://{host}/api/webhook"
    tg_res = await tg.set_webhook(webhook_url=webhook_url, secret_token=expected)

    return {
        "ok": bool(tg_res.get("ok")),
        "database_initialized": True,
        "webhook_url": webhook_url,
        "telegram_response": tg_res,
    }


@app.post("/webhook")
@app.post("/api/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    expected = get_webhook_secret()
    if expected:
        incoming = x_telegram_bot_api_secret_token or ""
        if not hmac.compare_digest(incoming, expected):
            raise HTTPException(status_code=403, detail="Invalid webhook token")

    try:
        update = await request.json()
        db = Database()
        tg = TelegramClient()
        await handle_telegram_update(update, db, tg)
    except Exception as exc:
        logger.exception("Error processing Telegram update: %s", exc)

    return {"ok": True}
