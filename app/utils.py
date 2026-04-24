import hashlib
import os
import re
from datetime import datetime
from typing import Any, Optional

from flask import current_app
from sqlalchemy import select

from .extensions import db


def normalize_win_number(raw: Optional[str]) -> str:
    """Только цифры и латинские буквы A–Z (ввод приводится к верхнему регистру)."""
    if raw is None:
        return ""
    s = re.sub(r"[^0-9A-Za-z]", "", str(raw)).upper()
    return s[:32]


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D+", "", (raw or "").strip())
    if not digits:
        return ""
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if digits.startswith("7") and len(digits) == 11:
        return f"+{digits}"
    if digits.startswith("9") and len(digits) == 10:
        return f"+7{digits}"
    return f"+{digits}" if not digits.startswith("+") else digits


def car_make_key(raw: str) -> str:
    value = (raw or "").strip().casefold()
    value = value.replace("ё", "е")
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^0-9a-zа-я]+", "", value)
    return value


def normalize_car_make(raw: str) -> str:
    value = re.sub(r"\s+", " ", (raw or "").strip())
    if not value:
        return ""
    value = value.replace("Ё", "Е").replace("ё", "е")
    value = re.sub(r"\s*-\s*", "-", value)

    upper = {"BMW", "KIA", "GMC", "RAM", "UAZ", "GAZ", "JAC", "GAC", "BYD", "DS", "MG", "OMODA", "JAECOO"}
    parts = re.split(r"([ -])", value)
    out: list[str] = []
    for p in parts:
        if p in {" ", "-"}:
            out.append(p)
            continue
        token = p.strip()
        if not token:
            continue
        if token.upper() in upper:
            out.append(token.upper())
        else:
            out.append(token[:1].upper() + token[1:].lower())
    return "".join(out)


def work_title_key(raw: str) -> str:
    value = (raw or "").strip().casefold()
    value = value.replace("ё", "е")
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^0-9a-zа-я]+", "", value)
    return value


def normalize_work_title(raw: str) -> str:
    value = re.sub(r"\s+", " ", (raw or "").strip())
    value = value.replace("Ё", "Е").replace("ё", "е")
    return value


def recalculate_work_order_total(order) -> int:
    """Полный пересчёт суммы заказ-наряда: работы, доп. работы сторонних, запчасти/детали, материалы."""
    total = 0
    for item in order.items or []:
        total += item.price or 0
    for aw in getattr(order, "additional_works", None) or []:
        total += int(aw.price or 0)
    for part in order.parts or []:
        total += int((part.price or 0) * (part.quantity or 0))
    for detail in order.details or []:
        total += int((detail.price or 0) * (detail.quantity or 0))
    for material in order.materials or []:
        total += int((material.price or 0) * (material.quantity or 0))
    order.total_amount = int(total)
    return int(total)


def merged_work_order_inventory_rows(order) -> list[tuple[str, Any]]:
    """Запчасти (старые WorkOrderPart) + детали в одном списке, по дате создания."""
    rows: list[tuple[str, Any]] = []
    for p in order.parts or []:
        rows.append(("part", p))
    for d in order.details or []:
        rows.append(("detail", d))
    rows.sort(key=lambda r: getattr(r[1], "created_at", None) or datetime.min)
    return rows


def work_order_has_positive_cashflow(work_order_id: int | None) -> bool:
    """Оплачен ли заказ-наряд по книге: суммарный баланс > 0.

    Важно: проверяем сумму, а не факт наличия одной положительной записи,
    чтобы отмена оплаты (отрицательной проводкой) корректно снимала флаг оплаты.
    """
    if not work_order_id:
        return False
    from .models import CashFlow

    from sqlalchemy import func

    total = db.session.execute(
        select(func.coalesce(func.sum(CashFlow.amount), 0)).where(CashFlow.work_order_id == int(work_order_id))
    ).scalar_one()
    return int(total or 0) > 0


def sync_work_order_is_paid_from_cashflow(order) -> bool:
    """Флаг is_paid всегда отражает наличие прихода в книге (не «желание» закрыть заказ)."""
    order.is_paid = work_order_has_positive_cashflow(getattr(order, "id", None))
    return bool(order.is_paid)


def materials_report_groups_for_period(start_date: datetime, end_date: datetime) -> tuple[list[dict[str, Any]], int]:
    """
    Ведомость материалов/деталей: запчасти (legacy), детали и материалы по заказам
    в статусах «Открыт» и «Закрыт», за период по дате создания заказа.
    """
    from .models import WorkOrder, WorkOrderDetail, WorkOrderMaterial, WorkOrderPart

    status_filter = WorkOrder.status.in_(("opened", "closed"))

    def ingest(rows, buckets: dict[tuple[str, str], dict], total_ref: list[int]):
        for row in rows:
            title = (row.title or "").strip()
            if not title:
                continue
            unit = (row.unit or "шт.").strip()
            key = (title.casefold(), unit.casefold())
            qty = float(row.quantity or 0)
            price = int(row.price or 0)
            line_cost = int(price * qty)
            total_ref[0] += line_cost
            if key not in buckets:
                buckets[key] = {"title": title, "unit": unit, "quantity": 0.0, "total_cost": 0, "items": []}
            b = buckets[key]
            b["quantity"] += qty
            b["total_cost"] += line_cost
            b["items"].append(row)

    buckets: dict[tuple[str, str], dict] = {}
    total_ref = [0]

    parts = db.session.execute(
        select(WorkOrderPart)
        .join(WorkOrder)
        .where(status_filter)
        .where(WorkOrder.created_at >= start_date)
        .where(WorkOrder.created_at <= end_date)
    ).scalars().all()
    ingest(parts, buckets, total_ref)

    details = db.session.execute(
        select(WorkOrderDetail)
        .join(WorkOrder)
        .where(status_filter)
        .where(WorkOrder.created_at >= start_date)
        .where(WorkOrder.created_at <= end_date)
    ).scalars().all()
    ingest(details, buckets, total_ref)

    mats = db.session.execute(
        select(WorkOrderMaterial)
        .join(WorkOrder)
        .where(status_filter)
        .where(WorkOrder.created_at >= start_date)
        .where(WorkOrder.created_at <= end_date)
    ).scalars().all()
    ingest(mats, buckets, total_ref)

    groups = sorted(buckets.values(), key=lambda x: (x["title"].casefold(), x["unit"].casefold()))
    return groups, int(total_ref[0])


def problem_description_hash(text: str | None) -> str:
    raw = (text or "").encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def issue_media_fingerprint(media_ids: list[int]) -> str:
    """Хеш набора вложений к описаниям (для опроса кабинета/админки)."""
    if not media_ids:
        return hashlib.sha256(b"").hexdigest()
    return hashlib.sha256(",".join(map(str, sorted(media_ids))).encode()).hexdigest()


def delete_appointment_issue_media_file(m) -> None:
    """Удалить файл с диска для строки AppointmentIssueMedia (запись в БД удаляется отдельно)."""
    base = current_app.config["DOCUMENTS_DIR"]
    path = os.path.join(base, m.storage_path.replace("/", os.sep))
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


def normalize_telegram_username(raw: str | None) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    return s.lstrip("@").strip()[:64]


def normalize_messenger_digits(raw: str | None) -> str | None:
    """Цифры номера для wa.me / t.me без лишнего кода страны (частая ошибка 77…)."""
    if not raw:
        return None
    digits = re.sub(r"\D+", "", raw)
    if not digits:
        return None
    # Дублирование ведущей 7 (например 7 + 79991234567 → 779991234567)
    while len(digits) >= 12 and digits.startswith("77"):
        digits = "7" + digits[2:]
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    # РФ: 10 цифр, мобильный 9XX…
    if len(digits) == 10 and digits[0] == "9":
        digits = "7" + digits
    while len(digits) >= 12 and digits.startswith("77"):
        digits = "7" + digits[2:]
    if len(digits) < 10:
        return None
    return digits


def whatsapp_me_url(phone_or_digits: str | None) -> str | None:
    """Ссылка https://wa.me/<digits> — только цифры, без + в пути."""
    digits = normalize_messenger_digits(phone_or_digits)
    if not digits:
        return None
    return f"https://wa.me/{digits}"


def telegram_me_url(username: str | None) -> str | None:
    """Ссылка на публичный ник t.me/username (справочные цели)."""
    u = normalize_telegram_username(username)
    if len(u) < 4 or len(u) > 32:
        return None
    if not re.match(r"^[a-zA-Z0-9_]+$", u):
        return None
    return f"https://t.me/{u}"


def telegram_phone_url(phone: str | None) -> str | None:
    """Открыть чат/звонок Telegram по номеру телефона: https://t.me/+<код_страны><номер>"""
    digits = normalize_messenger_digits(phone)
    if not digits:
        return None
    return f"https://t.me/+{digits}"


def client_whatsapp_url(user) -> str | None:
    """WhatsApp: отдельное поле client_whatsapp или логин-телефон."""
    w = (getattr(user, "client_whatsapp", None) or "").strip()
    phone = (getattr(user, "phone", None) or "").strip()
    return whatsapp_me_url(w or phone)


def client_telegram_url(user) -> str | None:
    """Telegram по номеру телефона учётной записи (логин = телефон)."""
    phone = (getattr(user, "phone", None) or "").strip()
    return telegram_phone_url(phone)


def work_order_share_text(order: Any, org_name: str = "", *, max_length: int = 1600) -> str:
    """Краткий текст заказ-наряда для WhatsApp / Telegram (лимит длины URL)."""
    org = (org_name or "").strip() or "Сервис"
    created = getattr(order, "created_at", None)
    date_s = created.strftime("%d.%m.%Y") if created else ""
    total = int(getattr(order, "total_amount", None) or 0)
    lines: list[str] = [
        f"{org}. Заказ-наряд №{getattr(order, 'id', '')} от {date_s}",
        f"Итого: {total:,} руб.".replace(",", " "),
    ]
    appt = getattr(order, "appointment", None)
    if appt is not None:
        mk = (getattr(appt, "car_make", None) or "").strip()
        md = (getattr(appt, "car_model", None) or "").strip()
        num = (getattr(appt, "car_number", None) or "").strip() or "—"
        lines.append(f"Авто: {mk} {md}".strip() + f", {num}")
    master = getattr(order, "master", None)
    if master is not None and (getattr(master, "name", None) or "").strip():
        lines.append(f"Мастер: {master.name.strip()}")
    client = getattr(order, "client", None)
    if client is not None:
        cn = (getattr(client, "name", None) or "").strip()
        if cn:
            lines.append(f"Заказчик: {cn}")
    lines.append("Работы:")
    for i, item in enumerate(getattr(order, "items", None) or [], start=1):
        st = "✓" if getattr(item, "is_done", False) else "○"
        title = ((getattr(item, "title", None) or "").strip())[:72]
        price = int(getattr(item, "price", 0) or 0)
        lines.append(f" {i}. {st} {title} — {price:,} руб.".replace(",", " "))
    for aw in getattr(order, "additional_works", None) or []:
        t = ((getattr(aw, "title", None) or "").strip())[:48]
        p = int(getattr(aw, "price", 0) or 0)
        lines.append(f" +доп: {t} — {p:,} руб.".replace(",", " "))
    for _, inv_row in merged_work_order_inventory_rows(order):
        t = ((getattr(inv_row, "title", None) or "").strip())[:40]
        q = getattr(inv_row, "quantity", 0) or 0
        pr = int(getattr(inv_row, "price", 0) or 0)
        lines.append(f" деталь: {t} ×{q} — {int(pr * float(q)):,} руб.".replace(",", " "))
    for m in getattr(order, "materials", None) or []:
        t = ((getattr(m, "title", None) or "").strip())[:40]
        q = getattr(m, "quantity", 0) or 0
        pr = int(getattr(m, "price", 0) or 0)
        lines.append(f" материал: {t} ×{q} — {int(pr * float(q)):,} руб.".replace(",", " "))
    text = "\n".join(lines)
    if len(text) > max_length:
        return text[: max_length - 3].rstrip() + "..."
    return text


def work_order_messenger_draft_text(order: Any, org_name: str, print_abs: str, *, max_total: int = 3500) -> str:
    """Текст заказ-наряда для вставки в WhatsApp / черновик Telegram (с ссылкой на печать, если влезает)."""
    org = (org_name or "").strip() or "Сервис"
    body = work_order_share_text(order, org, max_length=min(2000, max_total - 80))
    print_abs = (print_abs or "").strip()
    suffix = "\n\nПечатная форма: " + print_abs if print_abs else ""
    combined = body + suffix if print_abs and len(body) + len(suffix) <= max_total else body
    return combined


def work_order_whatsapp_share_href(order: Any, org_name: str, print_abs: str) -> str:
    """
    Ссылка для WhatsApp: wa.me/?text=… (без номера — пользователь выбирает чат).
    Текст в UTF-8, кодируется для query string.
    """
    from urllib.parse import quote

    combined = work_order_messenger_draft_text(order, org_name, print_abs, max_total=1800)
    return f"https://wa.me/?text={quote(combined, safe='')}"
