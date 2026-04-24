"""
Обработка входящих обновлений Telegram (одинаково для webhook и long polling).
Требуется контекст приложения Flask (db, current_app.logger).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from flask import current_app
from werkzeug.security import check_password_hash

from .extensions import db
from .models import TelegramLink, TelegramLinkToken, User, WorkOrder
from .telegram_bot import get_telegram_bot_token, redeem_work_order_code_for_chat, telegram_bot_send_message


def _send_text(chat_id: int | str, text: str) -> None:
    if not get_telegram_bot_token():
        current_app.logger.warning("Telegram: пропуск ответа — нет токена бота.")
        return
    try:
        telegram_bot_send_message(chat_id, text)
    except Exception:
        current_app.logger.exception("Telegram sendMessage failed chat_id=%s", chat_id)


def _link_account(chat_id: int | str, token_str: str) -> None:
    tokens = db.session.execute(
        db.select(TelegramLinkToken).where(TelegramLinkToken.used_at.is_(None))
    ).scalars().all()

    found_token = None
    for t in tokens:
        if check_password_hash(t.token_hash, token_str):
            if t.expires_at > datetime.utcnow():
                found_token = t
                break

    if not found_token:
        _send_text(chat_id, "Неверный или просроченный код привязки.")
        return

    link = db.session.execute(
        db.select(TelegramLink).where(TelegramLink.user_id == found_token.user_id)
    ).scalar_one_or_none()

    if not link:
        link = TelegramLink(user_id=found_token.user_id, telegram_chat_id=str(chat_id))
        db.session.add(link)
    else:
        link.telegram_chat_id = str(chat_id)
        link.is_active = True
        link.linked_at = datetime.utcnow()

    found_token.used_at = datetime.utcnow()
    db.session.commit()

    user = db.session.get(User, found_token.user_id)
    _send_text(
        chat_id,
        f"Аккаунт привязан: {user.name} ({user.phone}). Теперь можно использовать /zakaz КОД.",
    )


def _list_orders(chat_id: int | str) -> None:
    link = db.session.execute(
        db.select(TelegramLink).where(
            TelegramLink.telegram_chat_id == str(chat_id),
            TelegramLink.is_active.is_(True),
        )
    ).scalar_one_or_none()

    if not link:
        _send_text(chat_id, "Аккаунт не привязан. Сначала привяжите Telegram в личном кабинете.")
        return

    orders = db.session.execute(
        db.select(WorkOrder)
        .where(WorkOrder.client_user_id == link.user_id)
        .order_by(WorkOrder.id.desc())
        .limit(5)
    ).scalars().all()

    if not orders:
        _send_text(chat_id, "У вас пока нет заказ-нарядов.")
        return

    reply = "Ваши последние заказы:\n"
    for o in orders:
        status_ru = {"draft": "Черновик", "opened": "Открыт", "closed": "Закрыт", "cancelled": "Отменен"}.get(
            o.status, o.status
        )
        reply += f"№{o.id} от {o.created_at.strftime('%d.%m.%Y')} — {status_ru}. Сумма: {o.total_amount or 0} руб.\n"
        if o.documents:
            reply += "  Документы в личном кабинете.\n"

    _send_text(chat_id, reply)


def process_telegram_update(update: dict[str, Any]) -> None:
    """
    Обработать один объект Update от Telegram.
    https://core.telegram.org/bots/api#update
    """
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        return

    text = (message.get("text") or "").strip()

    if text.startswith("/start"):
        parts = text.split()
        if len(parts) > 1:
            _link_account(chat_id, parts[1])
            return
        _send_text(
            chat_id,
            "Привет! Для привязки аккаунта отправьте код из личного кабинета: /start ВАШ_КОД\n\n"
            "Чтобы получить заказ-наряд: /zakaz КОД_ИЗ_СЕРВИСА",
        )
        return

    if text.startswith("/zakaz"):
        parts = text.split(maxsplit=1)
        code_arg = parts[1].strip() if len(parts) > 1 else ""
        if not code_arg:
            _send_text(chat_id, "Укажите код после команды, например:\n/zakaz A1B2C3D4E5F6")
            return
        _, msg = redeem_work_order_code_for_chat(code_arg, str(chat_id))
        _send_text(chat_id, msg)
        return

    if re.fullmatch(r"[0-9A-F]{12}", text.upper()):
        _, msg = redeem_work_order_code_for_chat(text, str(chat_id))
        _send_text(chat_id, msg)
        return

    if text == "Мои заказы":
        _list_orders(chat_id)
        return

    _send_text(
        chat_id,
        "Команды: /zakaz КОД — получить заказ-наряд по коду из сервиса.\n"
        "«Мои заказы» — список последних нарядов (если аккаунт привязан).",
    )
