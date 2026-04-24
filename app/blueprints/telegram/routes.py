from flask import Blueprint, current_app, jsonify, request

from ...extensions import csrf
from ...telegram_bot import get_telegram_bot_token, get_telegram_bot_username
from ...telegram_handlers import process_telegram_update

bp = Blueprint("telegram", __name__)


@bp.get("/status")
def status():
    return jsonify(
        {
            "ok": True,
            "bot_name": get_telegram_bot_username(),
            "bot_configured": bool(get_telegram_bot_token()),
            "updates_mode": (current_app.config.get("TELEGRAM_UPDATES_MODE") or "polling"),
        }
    )


@bp.post("/webhook")
@csrf.exempt
def webhook():
    """Режим webhook (TELEGRAM_UPDATES_MODE=webhook). Тело запроса — объект Update."""
    data = request.get_json(silent=True) or {}
    try:
        process_telegram_update(data)
    except Exception:
        current_app.logger.exception("Telegram webhook")
    return jsonify({"ok": True})
