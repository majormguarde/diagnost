"""Вызовы Telegram Bot API (sendMessage) и логика кодов заказ-наряда для бота."""
from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any

from flask import current_app
from sqlalchemy.exc import IntegrityError

from .extensions import db
from .models import OrganizationSettings, TelegramLink, WorkOrder, WorkOrderTelegramCode
from .utils import merged_work_order_inventory_rows, recalculate_work_order_total, work_order_messenger_draft_text


def get_telegram_bot_token() -> str:
    """Токен бота: из настроек организации (раздел «Связь»), иначе из TELEGRAM_BOT_TOKEN в .env."""
    s = OrganizationSettings.get_settings()
    t = (getattr(s, "telegram_bot_token", None) or "").strip()
    if t:
        return t
    return (current_app.config.get("TELEGRAM_BOT_TOKEN") or "").strip()


def get_telegram_bot_username() -> str:
    """Имя бота без @: настройки организации, иначе TELEGRAM_BOT_NAME в .env."""
    s = OrganizationSettings.get_settings()
    u = (getattr(s, "telegram_bot_username", None) or "").strip().lstrip("@")
    if u:
        return u
    return (current_app.config.get("TELEGRAM_BOT_NAME") or "AutoDiagBot").strip().lstrip("@")


def telegram_bot_send_message(chat_id: int | str, text: str, *, disable_preview: bool = True) -> dict[str, Any]:
    """POST sendMessage. Нужен токен бота (настройки «Связь» или .env)."""
    token = get_telegram_bot_token()
    if not token:
        raise RuntimeError("Не задан токен Telegram-бота (раздел «Связь» или переменная TELEGRAM_BOT_TOKEN).")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks: list[str] = []
    rest = text or ""
    limit = 4090
    while rest:
        chunks.append(rest[:limit])
        rest = rest[limit:]
    last: dict[str, Any] = {}
    for part in chunks:
        body = json.dumps(
            {
                "chat_id": chat_id,
                "text": part,
                "disable_web_page_preview": disable_preview,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                last = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram API HTTP {e.code}: {err_body}") from e
    if not last.get("ok"):
        raise RuntimeError(str(last))
    return last


def build_zakaz_delivery_message(*, order: WorkOrder, code: str, bot_username: str) -> str:
    """Текст, который получает клиент (бот + команда с кодом)."""
    u = (bot_username or "").strip().lstrip("@")
    return (
        f"Заказ-наряд №{order.id}\n\n"
        f"Откройте бота: https://t.me/{u}\n"
        f"Отправьте боту команду (скопируйте целиком):\n"
        f"/zakaz {code}\n\n"
        f"Код действует 7 суток."
    )


def issue_work_order_telegram_code(work_order_id: int, *, ttl_days: int = 7) -> str:
    """
    Создаёт новый код для заказ-наряда (старые неиспользованные по этому наряду удаляются).
    Возвращает открытый код.
    """
    db.session.execute(
        db.delete(WorkOrderTelegramCode).where(
            WorkOrderTelegramCode.work_order_id == int(work_order_id),
            WorkOrderTelegramCode.used_at.is_(None),
        )
    )
    for _ in range(12):
        code = secrets.token_hex(6).upper()
        row = WorkOrderTelegramCode(
            work_order_id=int(work_order_id),
            code=code,
            expires_at=datetime.utcnow() + timedelta(days=ttl_days),
        )
        db.session.add(row)
        try:
            db.session.commit()
            return code
        except IntegrityError:
            db.session.rollback()
    raise RuntimeError("Не удалось сгенерировать уникальный код")


def work_order_full_text_for_bot(order: WorkOrder, settings: Any | None) -> str:
    """Текст наряда для ответа бота (без URL печати — нужна авторизация в браузере)."""
    org = (getattr(settings, "name", None) or "").strip() if settings else ""
    base = work_order_messenger_draft_text(order, org or "Сервис", "", max_total=8000)
    lines = [base, "", f"Наряд №{order.id} · итого {int(order.total_amount or 0)} ₽"]
    inv = merged_work_order_inventory_rows(order)
    if inv:
        lines.append("")
        lines.append("Детали:")
        for _, r in inv[:40]:
            lines.append(f" · {(r.title or '')[:60]} ×{r.quantity} — {int((r.price or 0) * (r.quantity or 0))} ₽")
    mats = list(order.materials or [])[:30]
    if mats:
        lines.append("")
        lines.append("Материалы:")
        for m in mats:
            lines.append(f" · {(m.title or '')[:60]} ×{m.quantity} — {int((m.price or 0) * (m.quantity or 0))} ₽")
    if order.inspection_results:
        lines.extend(["", "Осмотр:", (order.inspection_results or "")[:1500]])
    return "\n".join(lines)


def redeem_work_order_code_for_chat(code_raw: str, telegram_chat_id: str) -> tuple[bool, str]:
    """
    Проверяет код, привязку Telegram к клиенту наряда, отмечает использование.
    Возвращает (ok, message_for_user).
    """
    code = (code_raw or "").strip().upper().replace(" ", "")
    if len(code) < 8:
        return False, "Формат: /zakaz КОД"

    row = db.session.execute(
        db.select(WorkOrderTelegramCode).where(WorkOrderTelegramCode.code == code)
    ).scalar_one_or_none()
    if not row:
        return False, "Код не найден. Проверьте раскладку и срок действия."

    if row.used_at is not None:
        return False, "Этот код уже использован. Запросите новый у сервиса."

    if row.expires_at < datetime.utcnow():
        return False, "Срок действия кода истёк. Запросите новый."

    order = db.session.get(WorkOrder, row.work_order_id)
    if not order:
        return False, "Заказ не найден."

    link = db.session.execute(
        db.select(TelegramLink).where(
            TelegramLink.telegram_chat_id == str(telegram_chat_id),
            TelegramLink.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if not link:
        return False, "Сначала привяжите Telegram в личном кабинете на сайте, затем отправьте код снова."

    if int(order.client_user_id) != int(link.user_id):
        return False, "Этот заказ-наряд выдан другому клиенту."

    recalculate_work_order_total(order)
    row.used_at = datetime.utcnow()
    db.session.commit()

    settings = OrganizationSettings.get_settings()
    body = work_order_full_text_for_bot(order, settings)
    return True, body
