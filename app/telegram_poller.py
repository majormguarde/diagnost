"""
Фоновый long polling к Telegram Bot API — запускается вместе с приложением,
если TELEGRAM_UPDATES_MODE=polling (удобно без публичного HTTPS для webhook).

При первом успешном getUpdates вызывается deleteWebhook, чтобы не конфликтовать
с ранее настроенным вебхуком (см. TELEGRAM_BOT.md).
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

_started = False
_start_lock = threading.Lock()


def _telegram_api(token: str, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=65) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _poller_loop(app) -> None:
    from .telegram_bot import get_telegram_bot_token
    from .telegram_handlers import process_telegram_update

    offset = 0
    webhook_cleared = False

    while True:
        with app.app_context():
            token = get_telegram_bot_token()
            if not token:
                app.logger.info("Telegram poller: токен не задан, пауза 15 с.")
                time.sleep(15)
                continue

            if not webhook_cleared:
                try:
                    _telegram_api(token, "deleteWebhook", {"drop_pending_updates": False})
                    app.logger.info("Telegram: выполнен deleteWebhook (режим long polling).")
                except Exception:
                    app.logger.exception("Telegram deleteWebhook")
                webhook_cleared = True

            try:
                res = _telegram_api(
                    token,
                    "getUpdates",
                    {"offset": offset, "timeout": 50, "allowed_updates": ["message", "edited_message"]},
                )
                if not res.get("ok"):
                    app.logger.warning("Telegram getUpdates: %s", res)
                    time.sleep(5)
                    continue
                for upd in res.get("result", []):
                    offset = int(upd["update_id"]) + 1
                    try:
                        process_telegram_update(upd)
                    except Exception:
                        app.logger.exception("Telegram: ошибка обработки update_id=%s", upd.get("update_id"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                app.logger.warning("Telegram getUpdates HTTP %s: %s", e.code, body[:500])
                time.sleep(5)
            except Exception:
                app.logger.exception("Telegram poller")
                time.sleep(5)
        time.sleep(0.02)


def start_telegram_poller(app) -> None:
    """Один раз при старте приложения поднимает daemon-thread с getUpdates."""
    global _started
    # При Flask debug=True reloader поднимает два процесса — poller только в дочернем.
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return
    mode = (app.config.get("TELEGRAM_UPDATES_MODE") or "polling").strip().lower()
    if mode != "polling":
        return
    with _start_lock:
        if _started:
            return
        _started = True

    def _run() -> None:
        time.sleep(0.3)
        _poller_loop(app)

    threading.Thread(target=_run, name="telegram-poller", daemon=True).start()
    app.logger.info("Telegram: фоновый long polling включён (TELEGRAM_UPDATES_MODE=polling).")
