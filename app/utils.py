import hashlib
import os
import re
from datetime import datetime
from typing import Any, Optional

from flask import current_app


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
