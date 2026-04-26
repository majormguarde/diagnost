import calendar as pycalendar
from collections import defaultdict
from itertools import groupby
from functools import wraps
from datetime import date, datetime, time, timedelta
import json
import os
import re
import smtplib

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, session, url_for, current_app, send_from_directory
from flask_login import current_user, login_required
from sqlalchemy import delete, exists, or_, and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from werkzeug.utils import secure_filename

from ...extensions import csrf, db
from ...mail import MailConfigurationError, send_organization_email
from ...telegram_bot import (
    build_zakaz_delivery_message,
    get_telegram_bot_token,
    get_telegram_bot_username,
    issue_work_order_telegram_code,
    telegram_bot_send_message,
)
from ...models import (
    Master, Work, WorkCategory, TimeSlot, Appointment, WorkOrder,
    WorkOrderDocument, AppointmentSlot, OrganizationSettings, User,
    Banner, Review, WorkOrderItem, WorkOrderPart, WorkOrderDetail, WorkOrderMaterial,
    WorkOrderComplaintItem, WorkOrderAdditionalWork, AppointmentItem, Competency, CashFlow, MasterCompetency, AppointmentIssueMedia,
    TelegramLink,
    TelegramLinkToken,
    AppointmentDocument,
    AiModel,
    AiPromptTemplate,
    AiRequestLog,
    AppointmentAiQuestion,
)
from ...utils import (
    client_telegram_url,
    client_whatsapp_url,
    delete_appointment_issue_media_file,
    issue_media_fingerprint,
    materials_report_groups_for_period,
    merged_work_order_inventory_rows,
    normalize_phone,
    normalize_telegram_username,
    normalize_win_number,
    normalize_work_title,
    problem_description_hash,
    recalculate_work_order_total,
    sync_work_order_is_paid_from_cashflow,
    work_order_has_positive_cashflow,
    work_order_share_text,
    work_order_whatsapp_share_href,
    work_title_key,
)
from ...ai import AiError, openai_chat_completion
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from .forms import (
    MasterForm, WorkForm, CategoryForm, AppointmentForm, WorkOrderForm,
    DocumentUploadForm, OrganizationSettingsForm, AdminCredentialsForm,
    BannerForm, ReviewForm, WorkOrderItemForm,
    AppointmentItemForm, CompetencyForm, ClientForm, ClientCreateForm, ContactSettingsForm,
    AiAssistantSettingsForm,
    WorkOrderDetailForm, WorkOrderMaterialForm, WorkOrderAdditionalWorkForm,
)

bp = Blueprint("admin", __name__)


def _find_duplicate_inventory_line(order, title: str, unit: str):
    """Позиция с тем же наименованием и ед. изм. в деталях или старых запчастях."""
    tk = work_title_key(title or "")
    uk = work_title_key((unit or "шт.").strip())
    for d in order.details or []:
        if work_title_key(d.title or "") == tk and work_title_key(d.unit or "") == uk:
            return d, "detail"
    for p in order.parts or []:
        if work_title_key(p.title or "") == tk and work_title_key(p.unit or "") == uk:
            return p, "part"
    return None, None


def _admin_issue_media_bundle(appointment: Appointment) -> tuple[list[list[dict]], str]:
    """Вложения к пунктам описания (для админки и JSON)."""
    items_text = appointment.problem_items()
    n = len(items_text)
    media_rows = db.session.execute(
        db.select(AppointmentIssueMedia)
        .where(AppointmentIssueMedia.appointment_id == appointment.id)
        .order_by(
            AppointmentIssueMedia.issue_slot.asc(),
            AppointmentIssueMedia.sort_order.asc(),
            AppointmentIssueMedia.id.asc(),
        )
    ).scalars().all()
    issue_media_slots: list[list[dict]] = [[] for _ in range(n)]
    media_ids: list[int] = []
    for m in media_rows:
        media_ids.append(m.id)
        if 0 <= m.issue_slot < n:
            issue_media_slots[m.issue_slot].append(
                {
                    "id": m.id,
                    "mime": m.mime,
                    "url": url_for(
                        "admin.appointment_issue_media_file",
                        appointment_id=appointment.id,
                        media_id=m.id,
                    ),
                }
            )
    return issue_media_slots, issue_media_fingerprint(media_ids)


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if getattr(current_user, "role", None) != "admin":
            abort(403)
        return fn(*args, **kwargs)

    return wrapper


class _SafeFormatDict(dict):
    def __missing__(self, key):
        return "{" + str(key) + "}"


def _render_prompt_md(template_md: str, ctx: dict) -> str:
    t = str(template_md or "")
    if not t.strip():
        return ""
    try:
        return t.format_map(_SafeFormatDict(ctx or {}))
    except Exception:
        # If template contains braces not meant for formatting
        return t


_MUSTACHE_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
_JSON_NUMERIC_KEYS = frozenset({"YEAR", "MILEAGE", "MILEAGE_KM"})


def _render_prompt_mustache(template_md: str, ctx: dict) -> str:
    """Replace {{KEY}} placeholders. YEAR/MILEAGE are numeric (no quotes -> number/null)."""
    t = str(template_md or "")
    if not t.strip():
        return ""
    ctx = ctx or {}

    def repl(m):
        key = m.group(1)
        val = ctx.get(key, "")
        if key in _JSON_NUMERIC_KEYS:
            try:
                if val is None:
                    return "null"
                s = str(val).strip()
                if not s:
                    return "null"
                return str(int(float(s)))
            except Exception:
                return "null"
        s = "" if val is None else str(val)
        s = s.replace("\\", "\\\\").replace('"', '\\"')
        return s

    return _MUSTACHE_RE.sub(repl, t)


def _extract_questions_json(answer_text: str) -> list[dict]:
    """Try to extract JSON block with clarifying questions from AI answer."""
    s = str(answer_text or "")
    # prefer fenced json
    m = re.search(r"```json\s*([\s\S]*?)\s*```", s, flags=re.IGNORECASE)
    candidates = []
    if m:
        candidates.append(m.group(1))
    # fallback: first {...} block
    m2 = re.search(r"(\{[\s\S]{20,}\})", s)
    if m2:
        candidates.append(m2.group(1))
    for raw in candidates:
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        rows = obj.get("clarifying_questions") or obj.get("questions") or obj.get("clarifying") or []
        if not isinstance(rows, list):
            continue
        out = []
        for r in rows[:20]:
            if not isinstance(r, dict):
                continue
            q = str(r.get("q") or r.get("question") or "").strip()
            opts = r.get("options") or r.get("answers") or []
            if not q:
                continue
            if not isinstance(opts, list):
                opts = []
            opts2 = [str(x).strip() for x in opts if str(x or "").strip()][:10]
            if not opts2:
                # still allow saving the question with default options
                opts2 = ["Да", "Нет", "Не знаю"]
            out.append({"question": q, "options": opts2})
        if out:
            return out
    return []


def _appt_prompt_context(appointment: Appointment) -> dict:
    car_make = (appointment.car_make or "").strip()
    car_model = (appointment.car_model or "").strip()
    car_year = str(appointment.car_year or "").strip()
    win = (appointment.win_number or "").strip()
    car_number = (appointment.car_number or "").strip()
    issues_text = (appointment.problem_description or "").strip()
    client_name = (appointment.client.name if appointment.client else "") or ""
    master_name = (appointment.master.name if getattr(appointment, "master", None) else "") or ""
    symptoms = appointment.problem_items()[:10]

    engine_type = (appointment.engine_type or "").strip().lower()
    has_turbo = appointment.has_turbo if appointment.has_turbo is not None else None
    engine_volume_l = appointment.engine_volume_l if appointment.engine_volume_l is not None else None
    transmission_type = (appointment.transmission_type or "").strip().lower()
    mileage_km = appointment.mileage_km if appointment.mileage_km is not None else None

    engine_type_label = "бензин" if engine_type == "petrol" else ("дизель" if engine_type == "diesel" else "")
    turbo_label = "да" if has_turbo is True else ("нет" if has_turbo is False else "")
    tr_map = {"manual": "механика", "auto": "автомат", "robot": "робот", "cvt": "вариатор", "other": "другое"}
    tr_label = tr_map.get(transmission_type, transmission_type) if transmission_type else ""
    engine_parts: list[str] = []
    if engine_type_label:
        engine_parts.append(engine_type_label)
    if engine_volume_l:
        try:
            engine_parts.append(f"{float(engine_volume_l):.1f} л")
        except (TypeError, ValueError):
            pass
    if turbo_label:
        engine_parts.append(f"турбо: {turbo_label}")
    engine_summary = " · ".join([p for p in engine_parts if p])
    ctx = {
        # legacy keys
        "APPOINTMENT_ID": str(appointment.id),
        "STATUS": (appointment.status or "").strip(),
        "CLIENT_NAME": client_name,
        "MASTER_NAME": master_name,
        "CAR_MAKE": car_make,
        "CAR_MODEL": car_model,
        "CAR_YEAR": car_year,
        "CAR_NUMBER": car_number,
        "VIN": win,
        "WIN": win,
        "ISSUES": issues_text or "(не указано)",
        "ENGINE_TYPE": engine_type,
        "ENGINE_TYPE_LABEL": engine_type_label,
        "HAS_TURBO": "yes" if has_turbo is True else "no" if has_turbo is False else "",
        "TURBO": turbo_label,
        "ENGINE_VOLUME_L": (str(engine_volume_l) if engine_volume_l is not None else ""),
        "TRANSMISSION_TYPE": transmission_type,
        "TRANSMISSION_LABEL": tr_label,
        "MILEAGE_KM": mileage_km,
        # json-block keys ({{...}})
        "MARCA": car_make,
        "MODEL": car_model,
        "YEAR": car_year,
        "ENGINE": engine_summary,
        "TRANSMISSION": tr_label,
        "MILEAGE": mileage_km,
        "REGION": "RU",
        "TIMING": "",
        "PREV_CHECKS": "",
        "WORK_1": "",
        "LAMP_1": "",
    }
    for i in range(10):
        ctx[f"SYMPTOM_{i+1}"] = symptoms[i] if i < len(symptoms) else ""
    return ctx


def _ctx_to_input_md(ctx: dict) -> str:
    """Human-readable input data block for UI."""
    if not isinstance(ctx, dict):
        return ""
    # stable order for UI
    keys = [
        "APPOINTMENT_ID",
        "WORK_ORDER_ID",
        "STATUS",
        "CLIENT_NAME",
        "MASTER_NAME",
        "CAR_MAKE",
        "CAR_MODEL",
        "CAR_YEAR",
        "CAR_NUMBER",
        "VIN",
        "ISSUES",
        "TOTAL_AMOUNT",
    ]
    lines = []
    for k in keys:
        if k not in ctx:
            continue
        v = str(ctx.get(k) or "").strip()
        if not v:
            continue
        lines.append(f"- {k}: {v}")
    return "\n".join(lines).strip()


def _wo_prompt_context(order: WorkOrder) -> dict:
    appt = order.appointment
    car_make = (appt.car_make or "").strip() if appt else ""
    car_model = (appt.car_model or "").strip() if appt else ""
    car_year = str(appt.car_year or "").strip() if appt and appt.car_year is not None else ""
    win = (appt.win_number or "").strip() if appt else ""
    car_number = (appt.car_number or "").strip() if appt else ""
    client_name = (order.client.name if order.client else "") or ""
    master_name = (order.master.name if order.master else "") or ""
    issues = ""
    if order.complaints:
        issues = "\n".join(f"- {c.description}" for c in (order.complaints or []) if (c.description or "").strip())
    elif appt:
        issues = (appt.problem_description or "").strip()
    symptoms: list[str] = []
    if order.complaints:
        for c in (order.complaints or []):
            s = (getattr(c, "description", None) or "").strip()
            if s:
                symptoms.append(s)
    elif appt:
        symptoms = appt.problem_items()
    symptoms = (symptoms or [])[:10]

    engine_type = (appt.engine_type or "").strip().lower() if appt else ""
    has_turbo = appt.has_turbo if appt and appt.has_turbo is not None else None
    engine_volume_l = appt.engine_volume_l if appt and appt.engine_volume_l is not None else None
    transmission_type = (appt.transmission_type or "").strip().lower() if appt else ""
    mileage_km = appt.mileage_km if appt and appt.mileage_km is not None else None

    engine_type_label = "бензин" if engine_type == "petrol" else ("дизель" if engine_type == "diesel" else "")
    turbo_label = "да" if has_turbo is True else ("нет" if has_turbo is False else "")
    tr_map = {"manual": "механика", "auto": "автомат", "robot": "робот", "cvt": "вариатор", "other": "другое"}
    tr_label = tr_map.get(transmission_type, transmission_type) if transmission_type else ""
    engine_parts: list[str] = []
    if engine_type_label:
        engine_parts.append(engine_type_label)
    if engine_volume_l:
        try:
            engine_parts.append(f"{float(engine_volume_l):.1f} л")
        except (TypeError, ValueError):
            pass
    if turbo_label:
        engine_parts.append(f"турбо: {turbo_label}")
    engine_summary = " · ".join([p for p in engine_parts if p])

    ctx = {
        "WORK_ORDER_ID": str(order.id),
        "APPOINTMENT_ID": str(order.appointment_id or ""),
        "STATUS": (order.status or "").strip(),
        "CLIENT_NAME": client_name,
        "MASTER_NAME": master_name,
        "TOTAL_AMOUNT": str(int(order.total_amount or 0)),
        "CAR_MAKE": car_make,
        "CAR_MODEL": car_model,
        "CAR_YEAR": car_year,
        "CAR_NUMBER": car_number,
        "VIN": win,
        "WIN": win,
        "ISSUES": issues.strip() or "(не указано)",
        "ENGINE_TYPE": engine_type,
        "ENGINE_TYPE_LABEL": engine_type_label,
        "HAS_TURBO": "yes" if has_turbo is True else "no" if has_turbo is False else "",
        "TURBO": turbo_label,
        "ENGINE_VOLUME_L": (str(engine_volume_l) if engine_volume_l is not None else ""),
        "TRANSMISSION_TYPE": transmission_type,
        "TRANSMISSION_LABEL": tr_label,
        "MILEAGE_KM": mileage_km,
        "MARCA": car_make,
        "MODEL": car_model,
        "YEAR": car_year,
        "ENGINE": engine_summary,
        "TRANSMISSION": tr_label,
        "MILEAGE": mileage_km,
        "REGION": "RU",
        "TIMING": "",
        "PREV_CHECKS": "",
        "WORK_1": "",
        "LAMP_1": "",
    }
    for i in range(10):
        ctx[f"SYMPTOM_{i+1}"] = symptoms[i] if i < len(symptoms) else ""
    return ctx


@bp.post("/ai-prompt-templates/create-json")
@admin_required
def ai_prompt_template_create_json():
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title") or "").strip()[:160]
    body_md = str(payload.get("body_md") or "").strip()
    if not title or not body_md:
        return jsonify({"ok": False, "message": "Заполните название и текст."}), 400
    row = AiPromptTemplate(title=title, body_md=body_md, is_active=True)
    db.session.add(row)
    db.session.commit()
    rows = db.session.execute(
        db.select(AiPromptTemplate)
        .where(AiPromptTemplate.is_active.is_(True))
        .order_by(AiPromptTemplate.title.asc())
    ).scalars().all()
    return jsonify(
        {
            "ok": True,
            "templates": [{"id": int(r.id), "title": r.title, "body_md": r.body_md or ""} for r in rows],
        }
    )


@bp.post("/ai-default-template-json")
@admin_required
def ai_default_template_json():
    payload = request.get_json(silent=True) or {}
    kind = str(payload.get("kind") or "").strip().lower()
    tpl_id = payload.get("template_id")
    try:
        tpl_id_int = int(tpl_id) if tpl_id is not None and str(tpl_id).strip() else None
    except ValueError:
        tpl_id_int = None

    settings_obj = OrganizationSettings.get_settings()
    if kind == "appointment":
        settings_obj.ai_default_prompt_template_id_appt = tpl_id_int
    elif kind == "work_order":
        settings_obj.ai_default_prompt_template_id_wo = tpl_id_int
    else:
        return jsonify({"ok": False, "message": "kind"}), 400
    db.session.commit()
    return jsonify({"ok": True, "template_id": tpl_id_int})


def _parse_work_hours_range(work_hours: str | None) -> tuple[time, time] | None:
    if not work_hours:
        return None
    matches = re.findall(r"(\d{1,2})(?::(\d{2}))?", work_hours)
    if len(matches) < 2:
        return None
    start_hour, start_min = matches[0]
    end_hour, end_min = matches[1]
    return (
        time(int(start_hour), int(start_min or 0)),
        time(int(end_hour), int(end_min or 0)),
    )


def _resequence(items) -> None:
    for index, item in enumerate(items, start=1):
        item.sort_order = index


def _shift_calendar_month(year: int, month: int, delta: int) -> tuple[int, int]:
    m = month + delta
    y = year
    while m < 1:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return y, m


def _work_order_calendar_weeks(
    year: int,
    month: int,
    wo_counts_by_day: dict[int, int],
    appt_counts_by_day: dict[int, int],
    today: date,
) -> list[list[dict]]:
    """Сетка календаря (пн–вс): наряды opened + заявки (все статусы) по дате создания."""
    first_wd, n_days = pycalendar.monthrange(year, month)
    cells: list[int | None] = [None] * first_wd + list(range(1, n_days + 1))
    while len(cells) % 7 != 0:
        cells.append(None)
    weeks: list[list[dict]] = []
    for i in range(0, len(cells), 7):
        row = []
        for d in cells[i : i + 7]:
            if d is None:
                row.append(
                    {
                        "day": None,
                        "wo_count": 0,
                        "appt_count": 0,
                        "is_today": False,
                        "in_month": False,
                        "has_activity": False,
                    }
                )
            else:
                wo_cnt = int(wo_counts_by_day.get(d, 0))
                ap_cnt = int(appt_counts_by_day.get(d, 0))
                row.append(
                    {
                        "day": d,
                        "wo_count": wo_cnt,
                        "appt_count": ap_cnt,
                        "is_today": bool(today.year == year and today.month == month and today.day == d),
                        "in_month": True,
                        "has_activity": (wo_cnt > 0) or (ap_cnt > 0),
                    }
                )
        weeks.append(row)
    return weeks


@bp.get("/")
@admin_required
def index():
    appointments_count = db.session.query(Appointment).count()
    active_masters_count = db.session.query(Master).filter_by(is_active=True).count()
    clients_count = db.session.query(User).filter_by(role="client").count()
    work_orders_count = db.session.query(WorkOrder).count()

    appointment_status_counts = {
        "new": db.session.query(Appointment).filter_by(status="new").count(),
        "confirmed": db.session.query(Appointment).filter_by(status="confirmed").count(),
        "in_progress": db.session.query(Appointment).filter_by(status="in_progress").count(),
        "done": db.session.query(Appointment).filter_by(status="done").count(),
    }

    active_masters = db.session.execute(
        db.select(Master).where(Master.is_active.is_(True)).order_by(Master.name.asc())
    ).scalars().all()
    today = datetime.now().date()
    dashboard_days = [today + timedelta(days=offset) for offset in range(7)]
    dashboard_days_set = set(dashboard_days)
    range_start = datetime.combine(today, time.min)
    range_end = datetime.combine(dashboard_days[-1], time.max)

    upcoming_appointments = db.session.execute(
        db.select(Appointment)
        .where(Appointment.start_at >= datetime.combine(today, time.min))
        .order_by(Appointment.start_at.asc())
        .limit(8)
        .options(
            selectinload(Appointment.slots).selectinload(AppointmentSlot.slot),
            selectinload(Appointment.client),
            selectinload(Appointment.master),
        )
    ).scalars().all()

    master_ids = [m.id for m in active_masters]
    appointments_by_master_day: dict[tuple[int, datetime.date], int] = defaultdict(int)
    if master_ids:
        slot_touches_window = exists(
            db.select(1)
            .select_from(AppointmentSlot)
            .join(TimeSlot, TimeSlot.id == AppointmentSlot.slot_id)
            .where(AppointmentSlot.appointment_id == Appointment.id)
            .where(TimeSlot.start_at >= range_start)
            .where(TimeSlot.start_at <= range_end)
        )
        start_in_window = and_(
            Appointment.start_at >= range_start,
            Appointment.start_at <= range_end,
        )
        heatmap_appointments = db.session.execute(
            db.select(Appointment)
            .where(Appointment.master_id.in_(master_ids))
            .where(or_(slot_touches_window, start_in_window))
            .options(selectinload(Appointment.slots).selectinload(AppointmentSlot.slot))
        ).unique().scalars().all()
        for appt in heatmap_appointments:
            days: set[date] = set()
            ap_slots = [x for x in appt.slots if x.slot]
            if ap_slots:
                for x in ap_slots:
                    d = x.slot.start_at.date()
                    if d in dashboard_days_set:
                        days.add(d)
            elif appt.start_at:
                d = appt.start_at.date()
                if d in dashboard_days_set:
                    days.add(d)
            for d in days:
                appointments_by_master_day[(appt.master_id, d)] += 1

    master_load_rows = []
    for master in active_masters:
        cells = []
        weekly_total = 0
        for day in dashboard_days:
            count = appointments_by_master_day.get((master.id, day), 0)
            weekly_total += count
            if count == 0:
                level = "free"
            elif count == 1:
                level = "low"
            elif count == 2:
                level = "medium"
            else:
                level = "high"
            cells.append({"date": day, "count": count, "level": level})
        master_load_rows.append({"master": master, "cells": cells, "weekly_total": weekly_total})

    # --- Календарь заказ-нарядов «в работе» (статус opened) ---
    today_d = datetime.now().date()
    try:
        wo_cal_year = int(request.args.get("wo_year", today_d.year))
        wo_cal_month = int(request.args.get("wo_month", today_d.month))
    except (TypeError, ValueError):
        wo_cal_year, wo_cal_month = today_d.year, today_d.month
    wo_cal_month = max(1, min(12, wo_cal_month))
    wo_cal_year = max(2000, min(2100, wo_cal_year))
    wo_prev_y, wo_prev_m = _shift_calendar_month(wo_cal_year, wo_cal_month, -1)
    wo_next_y, wo_next_m = _shift_calendar_month(wo_cal_year, wo_cal_month, 1)

    opened_orders = db.session.execute(
        db.select(WorkOrder)
        .where(WorkOrder.status == "opened")
        .order_by(WorkOrder.created_at.desc())
        .options(selectinload(WorkOrder.client), selectinload(WorkOrder.master))
    ).scalars().all()
    opened_count = len(opened_orders)
    opened_total_amount = sum(int(o.total_amount or 0) for o in opened_orders)

    counts_by_day: dict[int, int] = defaultdict(int)
    for o in opened_orders:
        cdt = o.created_at
        if cdt and cdt.year == wo_cal_year and cdt.month == wo_cal_month:
            counts_by_day[cdt.day] += 1

    appt_counts_by_day: dict[int, int] = defaultdict(int)
    month_appts = db.session.execute(
        db.select(Appointment.created_at)
        .where(Appointment.created_at >= datetime(wo_cal_year, wo_cal_month, 1))
        .where(Appointment.created_at <= datetime.combine(date(wo_cal_year, wo_cal_month, pycalendar.monthrange(wo_cal_year, wo_cal_month)[1]), time.max))
    ).scalars().all()
    for cdt in month_appts:
        if cdt and cdt.year == wo_cal_year and cdt.month == wo_cal_month:
            appt_counts_by_day[int(cdt.day)] += 1

    wo_cal_weeks = _work_order_calendar_weeks(wo_cal_year, wo_cal_month, counts_by_day, appt_counts_by_day, today_d)
    month_names = (
        "",
        "Январь",
        "Февраль",
        "Март",
        "Апрель",
        "Май",
        "Июнь",
        "Июль",
        "Август",
        "Сентябрь",
        "Октябрь",
        "Ноябрь",
        "Декабрь",
    )

    return render_template(
        "admin/index.html",
        appointments_count=appointments_count,
        active_masters_count=active_masters_count,
        clients_count=clients_count,
        work_orders_count=work_orders_count,
        appointment_status_counts=appointment_status_counts,
        dashboard_days=dashboard_days,
        master_load_rows=master_load_rows,
        upcoming_appointments=upcoming_appointments,
        wo_cal_year=wo_cal_year,
        wo_cal_month=wo_cal_month,
        wo_cal_month_name=month_names[wo_cal_month],
        wo_prev_y=wo_prev_y,
        wo_prev_m=wo_prev_m,
        wo_next_y=wo_next_y,
        wo_next_m=wo_next_m,
        wo_cal_weeks=wo_cal_weeks,
        opened_orders=opened_orders,
        opened_count=opened_count,
        opened_total_amount=opened_total_amount,
    )


@bp.get("/dashboard/day-details")
@admin_required
def dashboard_day_details():
    """HTML-фрагмент для модалки: заявки и заказ-наряды за выбранный день."""
    s = (request.args.get("date") or "").strip()
    try:
        day = datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        abort(400)

    day_start = datetime.combine(day, time.min)
    day_end = datetime.combine(day, time.max)

    appointments = (
        db.session.execute(
            db.select(Appointment)
            .where(Appointment.created_at >= day_start)
            .where(Appointment.created_at <= day_end)
            .order_by(Appointment.created_at.asc())
            .options(
                selectinload(Appointment.slots).selectinload(AppointmentSlot.slot),
                selectinload(Appointment.client),
                selectinload(Appointment.master),
            )
        )
        .scalars()
        .all()
    )
    # В модалке сортируем по времени визита (слоты), но фильтр остаётся по created_at.
    def _visit_sort_key(a: Appointment) -> tuple[datetime, int]:
        slot_starts = [x.slot.start_at for x in (a.slots or []) if getattr(x, "slot", None) and x.slot.start_at]
        slot_starts.sort()
        visit_dt = slot_starts[0] if slot_starts else (a.start_at or a.created_at or day_start)
        return (visit_dt, int(a.id or 0))

    appointments.sort(key=_visit_sort_key)

    work_orders = (
        db.session.execute(
            db.select(WorkOrder)
            .where(WorkOrder.created_at >= day_start)
            .where(WorkOrder.created_at <= day_end)
            .order_by(WorkOrder.created_at.asc())
            .options(selectinload(WorkOrder.client), selectinload(WorkOrder.master))
        )
        .scalars()
        .all()
    )

    return render_template(
        "admin/_dashboard_day_details.html",
        day=day,
        appointments=appointments,
        work_orders=work_orders,
    )

# --- Клиенты ---

@bp.get("/clients")
@admin_required
def clients():
    all_clients = db.session.execute(
        db.select(User).where(User.role == "client").order_by(User.created_at.desc())
    ).scalars().all()
    return render_template("admin/clients/index.html", clients=all_clients)


@bp.route("/clients/new", methods=["GET", "POST"])
@admin_required
def client_create():
    form = ClientCreateForm()
    if form.validate_on_submit():
        phone = normalize_phone(form.phone.data)
        if not phone:
            flash("Некорректный телефон", "danger")
            return render_template("admin/clients/new.html", form=form)

        existing = db.session.execute(db.select(User).where(User.phone == phone)).scalar_one_or_none()
        if existing:
            flash("Пользователь с таким телефоном уже существует", "warning")
            return render_template("admin/clients/new.html", form=form)

        user = User(phone=phone, name=form.name.data.strip(), role="client", is_active=bool(form.is_active.data))
        user.set_password(form.password.data)
        wa = (form.client_whatsapp.data or "").strip()
        user.client_whatsapp = wa or None
        tg = normalize_telegram_username(form.client_telegram.data)
        user.client_telegram = tg or None
        em = (form.client_email.data or "").strip()
        user.client_email = em or None
        db.session.add(user)
        db.session.commit()
        flash("Клиент создан", "success")
        return redirect(url_for("admin.clients"))

    return render_template("admin/clients/new.html", form=form)


@bp.post("/clients/delete/<int:user_id>")
@admin_required
def client_delete(user_id):
    client = db.session.get(User, user_id)
    if not client or client.role != "client":
        abort(404)

    n_appt = (
        db.session.scalar(
            db.select(db.func.count()).select_from(Appointment).where(Appointment.client_user_id == client.id)
        )
        or 0
    )
    n_wo = (
        db.session.scalar(
            db.select(db.func.count()).select_from(WorkOrder).where(WorkOrder.client_user_id == client.id)
        )
        or 0
    )
    if n_appt or n_wo:
        flash(
            "Нельзя удалить клиента: есть связанные записи (заявки: "
            f"{n_appt}, заказ-наряды: {n_wo}). Сначала архивируйте или переназначьте данные.",
            "danger",
        )
        return redirect(url_for("admin.clients"))

    db.session.execute(delete(TelegramLinkToken).where(TelegramLinkToken.user_id == client.id))
    db.session.delete(client)
    db.session.commit()
    flash("Клиент удалён", "info")
    return redirect(url_for("admin.clients"))


@bp.route("/clients/edit/<int:user_id>", methods=["GET", "POST"])
@admin_required
def client_edit(user_id):
    client = db.session.get(User, user_id)
    if not client or client.role != "client":
        abort(404)

    if request.method == "GET":
        form = ClientForm(obj=client)
        form.password.data = ""
        form.password_confirm.data = ""
    else:
        form = ClientForm(obj=client)

    if form.validate_on_submit():
        phone = normalize_phone(form.phone.data)
        if not phone:
            flash("Некорректный телефон", "danger")
            return render_template("admin/clients/edit.html", form=form, client=client)

        existing = db.session.execute(
            db.select(User).where(User.phone == phone, User.id != client.id)
        ).scalar_one_or_none()
        if existing:
            flash("Пользователь с таким телефоном уже существует", "warning")
            return render_template("admin/clients/edit.html", form=form, client=client)

        client.name = form.name.data.strip()
        client.phone = phone
        client.is_active = bool(form.is_active.data)
        wa = (form.client_whatsapp.data or "").strip()
        client.client_whatsapp = wa or None
        client.client_telegram = normalize_telegram_username(form.client_telegram.data) or None
        client.client_email = (form.client_email.data or "").strip() or None
        new_pw = (form.password.data or "").strip()
        if new_pw:
            client.set_password(new_pw)
        db.session.commit()
        flash("Данные клиента сохранены", "success")
        return redirect(url_for("admin.clients"))

    if request.method == "POST" and form.errors:
        parts = []
        for fname, errs in form.errors.items():
            for err in errs:
                parts.append(f"{fname}: {err}")
        flash("Изменения не сохранены: " + "; ".join(parts), "danger")

    tg_link = db.session.execute(
        db.select(TelegramLink).where(TelegramLink.user_id == client.id)
    ).scalar_one_or_none()
    return render_template("admin/clients/edit.html", form=form, client=client, tg_link=tg_link)


@bp.post("/clients/edit/<int:user_id>/unlink-telegram")
@admin_required
def client_unlink_telegram(user_id: int):
    client = db.session.get(User, user_id)
    if not client or client.role != "client":
        abort(404)
    link = db.session.execute(
        db.select(TelegramLink).where(TelegramLink.user_id == client.id)
    ).scalar_one_or_none()
    if link:
        db.session.delete(link)
        db.session.commit()
        flash(f"Telegram-привязка клиента {client.name} удалена.", "success")
    else:
        flash("Привязка Telegram не найдена.", "warning")
    return redirect(url_for("admin.client_edit", user_id=user_id))


# --- Мастера ---

@bp.get("/masters")
@admin_required
def masters():
    all_masters = db.session.execute(db.select(Master).order_by(Master.name)).scalars().all()
    return render_template("admin/masters/index.html", masters=all_masters)

@bp.route("/masters/add", methods=["GET", "POST"])
@bp.route("/masters/edit/<int:master_id>", methods=["GET", "POST"])
@admin_required
def master_edit(master_id=None):
    master = db.session.get(Master, master_id) if master_id else Master()
    form = MasterForm(obj=master)
    
    competencies = db.session.execute(db.select(Competency).order_by(Competency.title)).scalars().all()
    form.competency_ids.choices = [(c.id, c.title) for c in competencies]
    
    if request.method == "GET" and master_id:
        form.competency_ids.data = [c.id for c in master.competencies]

    if form.validate_on_submit():
        master.name = form.name.data
        master.position = form.position.data
        master.description = form.description.data
        master.is_active = form.is_active.data
        master.payout_percent = form.payout_percent.data
        
        # Обновляем компетенции
        selected_competencies = db.session.execute(db.select(Competency).where(Competency.id.in_(form.competency_ids.data))).scalars().all()
        master.competencies = list(selected_competencies)
        
        if not master_id:
            db.session.add(master)
        
        db.session.commit()
        flash("Мастер сохранен", "success")
        return redirect(url_for("admin.masters"))
    
    return render_template("admin/masters/edit.html", form=form, master=master)

# --- Компетенции ---

@bp.get("/competencies")
@admin_required
def competencies():
    all_competencies = db.session.execute(
        db.select(Competency).order_by(Competency.sort_order.asc(), Competency.title.asc())
    ).scalars().all()
    return render_template("admin/competencies/index.html", competencies=all_competencies)

@bp.route("/competencies/add", methods=["GET", "POST"])
@bp.route("/competencies/edit/<int:competency_id>", methods=["GET", "POST"])
@admin_required
def competency_edit(competency_id=None):
    competency = db.session.get(Competency, competency_id) if competency_id else Competency()
    next_url = request.args.get("next") or request.form.get("next")
    form = CompetencyForm(obj=competency)
    if form.validate_on_submit():
        form.populate_obj(competency)
        if not competency_id:
            db.session.add(competency)
        db.session.commit()
        flash("Участок сохранен", "success")
        return redirect(next_url or url_for("admin.work_tree"))
    return render_template("admin/competencies/edit.html", form=form, competency=competency, next_url=next_url)

@bp.route("/competencies/delete/<int:competency_id>", methods=["POST"])
@admin_required
def competency_delete(competency_id):
    competency = db.session.get(Competency, competency_id)
    if not competency:
        abort(404)
    db.session.delete(competency)
    db.session.commit()
    flash("Участок удален", "info")
    return redirect(url_for("admin.competencies"))

# --- Услуги (Работы) ---

@bp.get("/works")
@admin_required
def works():
    all_works = db.session.execute(
        db.select(Work).join(WorkCategory).order_by(
            WorkCategory.sort_order.asc(), Work.sort_order.asc(), Work.title.asc()
        )
    ).scalars().all()
    return render_template("admin/works/index.html", works=all_works)

@bp.route("/works/add", methods=["GET", "POST"])
@bp.route("/works/edit/<int:work_id>", methods=["GET", "POST"])
@admin_required
def work_edit(work_id=None):
    work = db.session.get(Work, work_id) if work_id else Work()
    next_url = request.args.get("next") or request.form.get("next")
    form = WorkForm(obj=work)
    
    categories = db.session.execute(
        db.select(WorkCategory).order_by(WorkCategory.title)
    ).scalars().all()
    form.category_id.choices = [
        (
            c.id,
            f"{c.competency.title} / {c.title}" if c.competency else c.title,
        )
        for c in categories
    ]

    if request.method == "GET" and not work_id:
        preselected_category_id = request.args.get("category_id", type=int)
        if preselected_category_id:
            form.category_id.data = preselected_category_id
    
    if form.validate_on_submit():
        form.populate_obj(work)
        if not work_id:
            db.session.add(work)
        db.session.commit()
        flash("Операция сохранена", "success")
        return redirect(next_url or url_for("admin.work_tree"))
    
    return render_template("admin/works/edit.html", form=form, work=work, next_url=next_url)

# --- Категории ---

@bp.get("/categories")
@admin_required
def categories():
    all_cats = db.session.execute(
        db.select(WorkCategory).order_by(
            WorkCategory.competency_id.asc(), WorkCategory.sort_order.asc(), WorkCategory.title.asc()
        )
    ).scalars().all()
    return render_template("admin/categories/index.html", categories=all_cats)

@bp.route("/categories/add", methods=["GET", "POST"])
@bp.route("/categories/edit/<int:cat_id>", methods=["GET", "POST"])
@admin_required
def category_edit(cat_id=None):
    cat = db.session.get(WorkCategory, cat_id) if cat_id else WorkCategory()
    next_url = request.args.get("next") or request.form.get("next")
    form = CategoryForm(obj=cat)
    competencies = db.session.execute(db.select(Competency).order_by(Competency.title)).scalars().all()
    form.competency_id.choices = [(c.id, c.title) for c in competencies]

    if request.method == "GET" and not cat_id:
        preselected_competency_id = request.args.get("competency_id", type=int)
        if preselected_competency_id:
            form.competency_id.data = preselected_competency_id

    if form.validate_on_submit():
        form.populate_obj(cat)
        if not cat_id:
            db.session.add(cat)
        db.session.commit()
        flash("Категория операции сохранена", "success")
        return redirect(next_url or url_for("admin.work_tree"))
    return render_template("admin/categories/edit.html", form=form, category=cat, next_url=next_url)


@bp.get("/work-tree")
@admin_required
def work_tree():
    sections = db.session.execute(
        db.select(Competency).order_by(Competency.sort_order.asc(), Competency.title.asc())
    ).scalars().all()
    categories = db.session.execute(
        db.select(WorkCategory).order_by(
            WorkCategory.competency_id.asc(), WorkCategory.sort_order.asc(), WorkCategory.title.asc()
        )
    ).scalars().all()
    operations = db.session.execute(
        db.select(Work).order_by(Work.category_id.asc(), Work.sort_order.asc(), Work.title.asc())
    ).scalars().all()

    categories_by_section: dict[int | None, list[WorkCategory]] = {}
    for category in categories:
        categories_by_section.setdefault(category.competency_id, []).append(category)

    operations_by_category: dict[int, list[Work]] = {}
    for operation in operations:
        operations_by_category.setdefault(operation.category_id, []).append(operation)

    return render_template(
        "admin/work_tree.html",
        sections=sections,
        categories_by_section=categories_by_section,
        operations_by_category=operations_by_category,
    )


@bp.post("/work-tree/reorder")
@admin_required
def work_tree_reorder():
    payload = request.get_json(silent=True) or {}
    drag_type = payload.get("drag_type")
    drag_id = payload.get("drag_id")
    target_type = payload.get("target_type")
    target_id = payload.get("target_id")
    position = payload.get("position", "inside")

    if not all([drag_type, drag_id, target_type, target_id]):
        return jsonify({"ok": False, "message": "Недостаточно данных для перемещения."}), 400

    drag_id = int(drag_id)
    target_id = int(target_id)

    if drag_type == "competency":
        if target_type != "competency" or position not in {"before", "after"}:
            return jsonify({"ok": False, "message": "Участки можно переставлять только между собой."}), 400
        dragged = db.session.get(Competency, drag_id)
        target = db.session.get(Competency, target_id)
        if not dragged or not target:
            return jsonify({"ok": False, "message": "Участок не найден."}), 404

        siblings = db.session.execute(
            db.select(Competency).order_by(Competency.sort_order.asc(), Competency.title.asc())
        ).scalars().all()
        siblings = [item for item in siblings if item.id != dragged.id]
        target_index = next((i for i, item in enumerate(siblings) if item.id == target.id), len(siblings))
        insert_index = target_index if position == "before" else target_index + 1
        siblings.insert(insert_index, dragged)
        _resequence(siblings)

    elif drag_type == "category":
        dragged = db.session.get(WorkCategory, drag_id)
        if not dragged:
            return jsonify({"ok": False, "message": "Категория операции не найдена."}), 404

        if target_type == "competency":
            target = db.session.get(Competency, target_id)
            if not target:
                return jsonify({"ok": False, "message": "Участок не найден."}), 404
            dragged.competency_id = target.id
            siblings = db.session.execute(
                db.select(WorkCategory)
                .where(WorkCategory.competency_id == target.id, WorkCategory.id != dragged.id)
                .order_by(WorkCategory.sort_order.asc(), WorkCategory.title.asc())
            ).scalars().all()
            siblings.append(dragged)
            _resequence(siblings)
        elif target_type == "category" and position in {"before", "after"}:
            target = db.session.get(WorkCategory, target_id)
            if not target:
                return jsonify({"ok": False, "message": "Категория операции не найдена."}), 404
            dragged.competency_id = target.competency_id
            siblings = db.session.execute(
                db.select(WorkCategory)
                .where(WorkCategory.competency_id == target.competency_id, WorkCategory.id != dragged.id)
                .order_by(WorkCategory.sort_order.asc(), WorkCategory.title.asc())
            ).scalars().all()
            target_index = next((i for i, item in enumerate(siblings) if item.id == target.id), len(siblings))
            insert_index = target_index if position == "before" else target_index + 1
            siblings.insert(insert_index, dragged)
            _resequence(siblings)
        else:
            return jsonify({"ok": False, "message": "Категории можно перемещать только в участки или между категориями."}), 400

    elif drag_type == "work":
        dragged = db.session.get(Work, drag_id)
        if not dragged:
            return jsonify({"ok": False, "message": "Операция не найдена."}), 404

        if target_type == "category":
            target = db.session.get(WorkCategory, target_id)
            if not target:
                return jsonify({"ok": False, "message": "Категория операции не найдена."}), 404
            dragged.category_id = target.id
            siblings = db.session.execute(
                db.select(Work)
                .where(Work.category_id == target.id, Work.id != dragged.id)
                .order_by(Work.sort_order.asc(), Work.title.asc())
            ).scalars().all()
            siblings.append(dragged)
            _resequence(siblings)
        elif target_type == "work" and position in {"before", "after"}:
            target = db.session.get(Work, target_id)
            if not target:
                return jsonify({"ok": False, "message": "Операция не найдена."}), 404
            dragged.category_id = target.category_id
            siblings = db.session.execute(
                db.select(Work)
                .where(Work.category_id == target.category_id, Work.id != dragged.id)
                .order_by(Work.sort_order.asc(), Work.title.asc())
            ).scalars().all()
            target_index = next((i for i, item in enumerate(siblings) if item.id == target.id), len(siblings))
            insert_index = target_index if position == "before" else target_index + 1
            siblings.insert(insert_index, dragged)
            _resequence(siblings)
        else:
            return jsonify({"ok": False, "message": "Операции можно перемещать только в категории или между операциями."}), 400
    else:
        return jsonify({"ok": False, "message": "Неизвестный тип узла."}), 400

    db.session.commit()
    return jsonify({"ok": True})


@bp.post("/work-tree/work/<int:work_id>/inline-update")
@admin_required
def work_tree_work_inline_update(work_id):
    work = db.session.get(Work, work_id)
    if not work:
        return jsonify({"ok": False, "message": "Операция не найдена."}), 404

    payload = request.get_json(silent=True) or {}
    field = payload.get("field")
    value = payload.get("value")

    if field not in {"duration_min", "base_price"}:
        return jsonify({"ok": False, "message": "Недопустимое поле."}), 400

    try:
        numeric_value = int(value)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Нужно указать число."}), 400

    if numeric_value < 0:
        return jsonify({"ok": False, "message": "Значение не может быть отрицательным."}), 400

    setattr(work, field, numeric_value)
    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "field": field,
            "value": numeric_value,
            "display": f"{numeric_value} МИН" if field == "duration_min" else f"{numeric_value} РУБ",
        }
    )

@bp.post("/work-tree/delete")
@admin_required
def work_tree_delete():
    payload = request.get_json(silent=True) or {}
    node_type = payload.get("node_type")
    node_id = payload.get("node_id")

    if not node_type or not node_id:
        return jsonify({"ok": False, "message": "Неверные параметры запроса."}), 400

    try:
        if node_type == "competency":
            node = db.session.get(Competency, node_id)
            if not node:
                return jsonify({"ok": False, "message": "Участок не найден."}), 404
            
            # Cascade delete manually since we didn't specify cascade="all, delete" in models
            cats = db.session.execute(db.select(WorkCategory).where(WorkCategory.competency_id == node.id)).scalars().all()
            for cat in cats:
                works = db.session.execute(db.select(Work).where(Work.category_id == cat.id)).scalars().all()
                for w in works:
                    db.session.delete(w)
                db.session.delete(cat)
            
            # Delete MasterCompetency links
            db.session.execute(db.delete(MasterCompetency).where(MasterCompetency.competency_id == node.id))
            db.session.delete(node)

        elif node_type == "category":
            node = db.session.get(WorkCategory, node_id)
            if not node:
                return jsonify({"ok": False, "message": "Категория не найдена."}), 404
                
            works = db.session.execute(db.select(Work).where(Work.category_id == node.id)).scalars().all()
            for w in works:
                db.session.delete(w)
            db.session.delete(node)

        elif node_type == "work":
            node = db.session.get(Work, node_id)
            if not node:
                return jsonify({"ok": False, "message": "Операция не найдена."}), 404
            db.session.delete(node)
            
        else:
            return jsonify({"ok": False, "message": "Неизвестный тип узла."}), 400

        db.session.commit()
        return jsonify({"ok": True})
    except IntegrityError:
        db.session.rollback()
        return jsonify({"ok": False, "message": "Невозможно удалить узел. Возможно, он используется в заявках или других документах."}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "message": str(e)}), 500


@bp.post("/work-tree/rename")
@admin_required
def work_tree_rename():
    payload = request.get_json(silent=True) or {}
    node_type = payload.get("node_type")
    node_id = int(payload.get("node_id") or 0)
    field = payload.get("field", "title")
    value = (payload.get("value") or "").strip()

    if not node_type or not node_id or not value:
        return jsonify({"ok": False, "message": "Недостаточно данных."}), 400

    if node_type == "competency":
        item = db.session.get(Competency, node_id)
        if not item:
            return jsonify({"ok": False, "message": "Участок не найден."}), 404
        item.title = value
    elif node_type == "category":
        item = db.session.get(WorkCategory, node_id)
        if not item:
            return jsonify({"ok": False, "message": "Категория не найдена."}), 404
        item.title = value
    elif node_type == "work":
        item = db.session.get(Work, node_id)
        if not item:
            return jsonify({"ok": False, "message": "Операция не найдена."}), 404
        item.title = value
    else:
        return jsonify({"ok": False, "message": "Неизвестный тип узла."}), 400

    db.session.commit()
    return jsonify({"ok": True, "value": value})


# --- Расписание (Слоты) ---

@bp.route("/schedule/<int:master_id>")
@admin_required
def schedule(master_id):
    master = db.session.get(Master, master_id)
    if not master:
        abort(404)
    
    # Показать слоты на ближайшие 7 дней
    today = datetime.now().date()
    end_date = today + timedelta(days=7)
    
    slots = db.session.execute(
        db.select(TimeSlot)
        .where(TimeSlot.master_id == master_id)
        .where(TimeSlot.start_at >= datetime.combine(today, time.min))
        .where(TimeSlot.start_at <= datetime.combine(end_date, time.max))
        .order_by(TimeSlot.start_at)
    ).scalars().all()
    booked_slot_ids = set(
        db.session.execute(
            db.select(AppointmentSlot.slot_id).where(
                AppointmentSlot.slot_id.in_([slot.id for slot in slots])
            )
        ).scalars().all()
    ) if slots else set()

    return render_template(
        "admin/schedule/view.html",
        master=master,
        slots=slots,
        booked_slot_ids=booked_slot_ids,
        today_str=today.isoformat(),
        end_date_str=end_date.isoformat(),
    )

@bp.route("/schedule/generate/<int:master_id>", methods=["POST"])
@admin_required
def schedule_generate(master_id):
    master = db.session.get(Master, master_id)
    if not master:
        abort(404)

    settings_obj = OrganizationSettings.get_settings()
    slot_minutes = int(settings_obj.slot_minutes or 60)
    if slot_minutes < 15:
        slot_minutes = 15

    work_hours_range = _parse_work_hours_range(settings_obj.work_hours)
    if not work_hours_range:
        flash("Сначала укажите корректные часы работы организации в настройках системы.", "warning")
        return redirect(url_for("admin.schedule", master_id=master_id))

    selected_weekdays = set(request.form.getlist("weekdays"))
    company_work_days = set(settings_obj.work_days.split(",")) if settings_obj.work_days else set()
    disallowed_weekdays = selected_weekdays - company_work_days if company_work_days else set()
    allowed_weekdays = selected_weekdays & company_work_days if selected_weekdays else company_work_days

    if disallowed_weekdays:
        flash("Выбраны дни, которые не являются рабочими для предприятия. Генерация для них запрещена.", "warning")

    if not allowed_weekdays:
        flash("Не выбраны рабочие дни предприятия для генерации слотов.", "warning")
        return redirect(url_for("admin.schedule", master_id=master_id))

    start_time, end_time = work_hours_range
    if start_time >= end_time:
        flash("Часы работы организации указаны некорректно.", "warning")
        return redirect(url_for("admin.schedule", master_id=master_id))

    today = datetime.now().date()
    month_end = today.replace(day=28) + timedelta(days=4)
    month_end = month_end - timedelta(days=month_end.day)
    count = 0
    skipped_non_working = 0

    day = today
    while day <= month_end:
        if str(day.weekday()) not in allowed_weekdays:
            if str(day.weekday()) in selected_weekdays and str(day.weekday()) not in company_work_days:
                skipped_non_working += 1
            day += timedelta(days=1)
            continue

        current_dt = datetime.combine(day, start_time)
        day_end_dt = datetime.combine(day, end_time)

        while current_dt + timedelta(minutes=slot_minutes) <= day_end_dt:
            slot_start = current_dt
            slot_end = current_dt + timedelta(minutes=slot_minutes)

            exists = db.session.execute(
                db.select(TimeSlot)
                .where(TimeSlot.master_id == master_id, TimeSlot.start_at == slot_start)
            ).scalar()

            if not exists:
                db.session.add(
                    TimeSlot(
                        master_id=master_id,
                        start_at=slot_start,
                        end_at=slot_end,
                        status="free"
                    )
                )
                count += 1

            current_dt = slot_end

        day += timedelta(days=1)

    db.session.commit()
    message = f"Сгенерировано слотов: {count} (интервал {slot_minutes} мин)"
    if skipped_non_working:
        message += f". Пропущено нерабочих дней предприятия: {skipped_non_working}"
    flash(message, "success")
    return redirect(url_for("admin.schedule", master_id=master_id))


@bp.post("/schedule/delete-all/<int:master_id>")
@admin_required
def schedule_delete_all(master_id):
    master = db.session.get(Master, master_id)
    if not master:
        abort(404)

    date_from = datetime.fromisoformat(request.form.get("date_from")).date()
    date_to = datetime.fromisoformat(request.form.get("date_to")).date()
    used_slot_ids = db.select(AppointmentSlot.slot_id)
    slots_to_delete = db.session.execute(
        db.select(TimeSlot)
        .where(TimeSlot.master_id == master_id)
        .where(TimeSlot.start_at >= datetime.combine(date_from, time.min))
        .where(TimeSlot.start_at <= datetime.combine(date_to, time.max))
        .where(~TimeSlot.id.in_(used_slot_ids))
    ).scalars().all()

    deleted = 0
    for slot in slots_to_delete:
        db.session.delete(slot)
        deleted += 1

    db.session.commit()
    flash(f"Удалено слотов: {deleted}", "success")
    return redirect(url_for("admin.schedule", master_id=master_id))


@bp.post("/schedule/delete-selected/<int:master_id>")
@admin_required
def schedule_delete_selected(master_id):
    slot_ids = [int(slot_id) for slot_id in request.form.getlist("slot_ids") if slot_id.isdigit()]
    used_slot_ids = set(db.session.execute(db.select(AppointmentSlot.slot_id).where(AppointmentSlot.slot_id.in_(slot_ids))).scalars().all()) if slot_ids else set()
    slots = db.session.execute(db.select(TimeSlot).where(TimeSlot.master_id == master_id, TimeSlot.id.in_(slot_ids))).scalars().all()
    deleted = 0
    for slot in slots:
        if slot.id in used_slot_ids:
            continue
        db.session.delete(slot)
        deleted += 1
    db.session.commit()
    flash(f"Удалено слотов: {deleted}", "success")
    return redirect(url_for("admin.schedule", master_id=master_id))


@bp.post("/schedule/block-selected/<int:master_id>")
@admin_required
def schedule_block_selected(master_id):
    slot_ids = [int(slot_id) for slot_id in request.form.getlist("slot_ids") if slot_id.isdigit()]
    used_slot_ids = set(db.session.execute(db.select(AppointmentSlot.slot_id).where(AppointmentSlot.slot_id.in_(slot_ids))).scalars().all()) if slot_ids else set()
    slots = db.session.execute(db.select(TimeSlot).where(TimeSlot.master_id == master_id, TimeSlot.id.in_(slot_ids))).scalars().all()
    updated = 0
    for slot in slots:
        if slot.id in used_slot_ids:
            continue
        slot.status = "blocked"
        updated += 1
    db.session.commit()
    flash(f"Заблокировано слотов: {updated}", "success")
    return redirect(url_for("admin.schedule", master_id=master_id))

# --- Заявки (Requests) ---

@bp.get("/appointments")
@admin_required
def appointments():
    all_appointments = db.session.execute(
        db.select(Appointment)
        .order_by(Appointment.created_at.desc())
        .options(selectinload(Appointment.slots).selectinload(AppointmentSlot.slot))
    ).scalars().all()
    return render_template("admin/appointments/index.html", appointments=all_appointments)

@bp.route("/appointments/<int:appointment_id>", methods=["GET", "POST"])
@admin_required
def appointment_detail(appointment_id):
    appointment = db.session.execute(
        db.select(Appointment)
        .where(Appointment.id == appointment_id)
        .options(
            selectinload(Appointment.slots).selectinload(AppointmentSlot.slot),
            selectinload(Appointment.client),
        )
    ).scalar_one_or_none()
    if not appointment:
        abort(404)
        
    form = AppointmentForm(obj=appointment)
    item_form = AppointmentItemForm()

    # WTForms SelectField doesn't auto-map bool<->string choices
    if request.method == "GET":
        if appointment.has_turbo is True:
            form.has_turbo.data = "yes"
        elif appointment.has_turbo is False:
            form.has_turbo.data = "no"
        else:
            form.has_turbo.data = ""
    
    # Загружаем список мастеров
    masters = db.session.execute(db.select(Master).where(Master.is_active == True)).scalars().all()
    form.master_id.choices = [(m.id, m.name) for m in masters]
    
    categories_list = db.session.execute(
        db.select(WorkCategory).order_by(
            WorkCategory.competency_id.asc(), WorkCategory.sort_order.asc(), WorkCategory.title.asc()
        )
    ).scalars().all()

    sections = db.session.execute(
        db.select(Competency).order_by(Competency.sort_order.asc(), Competency.title.asc())
    ).scalars().all()
    works_tree = db.session.execute(
        db.select(Work)
        .where(Work.is_active == True)
        .order_by(Work.category_id.asc(), Work.sort_order.asc(), Work.title.asc())
    ).scalars().all()

    categories_by_section: dict[int | None, list[WorkCategory]] = {}
    for cat in categories_list:
        categories_by_section.setdefault(cat.competency_id, []).append(cat)

    works_by_category: dict[int, list[Work]] = {}
    for w in works_tree:
        works_by_category.setdefault(w.category_id, []).append(w)
    
    ap_slots_m = [x for x in appointment.slots if x.slot and x.slot.master_id == appointment.master_id]
    ap_slots_m.sort(key=lambda x: x.slot.start_at)
    current_slot_ids = [x.slot_id for x in ap_slots_m]

    if form.validate_on_submit():
        appointment.master_id = form.master_id.data
        appointment.status = form.status.data
        appointment.car_make = (form.car_make.data or "").strip() or None
        appointment.car_model = (form.car_model.data or "").strip() or None
        appointment.car_year = form.car_year.data
        appointment.car_number = (form.car_number.data or "").strip() or None
        appointment.win_number = normalize_win_number(form.win_number.data) or None
        appointment.engine_type = (form.engine_type.data or "").strip() or None
        ht = (form.has_turbo.data or "").strip().lower()
        appointment.has_turbo = True if ht == "yes" else False if ht == "no" else None
        appointment.engine_volume_l = form.engine_volume_l.data
        appointment.transmission_type = (form.transmission_type.data or "").strip() or None
        appointment.mileage_km = form.mileage_km.data
        raw_ids = (request.form.get("time_slot_ids") or "").replace(",", " ").split()
        slot_ids: list[int] = []
        seen: set[int] = set()
        for x in raw_ids:
            if not x.strip().isdigit():
                continue
            i = int(x)
            if i not in seen:
                seen.add(i)
                slot_ids.append(i)

        if slot_ids:
            slots_objs: list[TimeSlot] = []
            for sid in slot_ids:
                slot = db.session.get(TimeSlot, sid)
                if not slot or slot.master_id != form.master_id.data:
                    flash("Указан недопустимый временной слот.", "danger")
                    return redirect(url_for("admin.appointment_detail", appointment_id=appointment.id))
                other = db.session.execute(
                    db.select(AppointmentSlot).where(
                        AppointmentSlot.slot_id == slot.id,
                        AppointmentSlot.appointment_id != appointment.id,
                    )
                ).scalar_one_or_none()
                if other:
                    flash("Один из слотов уже занят другой заявкой.", "danger")
                    return redirect(url_for("admin.appointment_detail", appointment_id=appointment.id))
                slots_objs.append(slot)

            slots_objs.sort(key=lambda s: s.start_at)
            for _, grp in groupby(slots_objs, key=lambda s: s.start_at.date()):
                chunk = list(grp)
                chunk.sort(key=lambda s: s.start_at)
                for i in range(len(chunk) - 1):
                    if chunk[i].end_at != chunk[i + 1].start_at:
                        flash(
                            "Внутри одного дня слоты должны идти подряд. Можно назначить визиты в несколько дней.",
                            "danger",
                        )
                        return redirect(url_for("admin.appointment_detail", appointment_id=appointment.id))

            appointment.start_at = slots_objs[0].start_at
            appointment.end_at = slots_objs[-1].end_at
            db.session.execute(delete(AppointmentSlot).where(AppointmentSlot.appointment_id == appointment.id))
            for s in slots_objs:
                db.session.add(AppointmentSlot(appointment_id=appointment.id, slot_id=s.id))
        else:
            appointment.start_at = form.start_at.data
            appointment.end_at = appointment.start_at + timedelta(minutes=60)
            db.session.execute(delete(AppointmentSlot).where(AppointmentSlot.appointment_id == appointment.id))

        appointment.status = form.status.data
        appointment.car_make = form.car_make.data
        appointment.car_model = form.car_model.data
        appointment.car_year = form.car_year.data
        appointment.car_number = form.car_number.data
        appointment.win_number = normalize_win_number(form.win_number.data) or None
        appointment.engine_type = (form.engine_type.data or "").strip() or None
        ht = (form.has_turbo.data or "").strip().lower()
        if ht == "yes":
            appointment.has_turbo = True
        elif ht == "no":
            appointment.has_turbo = False
        else:
            appointment.has_turbo = None
        appointment.engine_volume_l = form.engine_volume_l.data
        appointment.transmission_type = (form.transmission_type.data or "").strip() or None
        appointment.mileage_km = form.mileage_km.data
        appointment.problem_description = form.problem_description.data

        # Если статус заявки изменен на "confirmed" и заказ-наряд еще не создан, создаем его
        if form.status.data == "confirmed" and not appointment.work_order:
            order = WorkOrder(
                appointment_id=appointment.id,
                client_user_id=appointment.client_user_id,
                master_id=appointment.master_id,
                status="draft",
                total_amount=0,
            )
            db.session.add(order)

            # Переносим услуги из заявки в заказ-наряд
            for item in appointment.items:
                if bool(getattr(item, "declined_by_client", False)):
                    continue
                k1 = float(item.k1 or 1.0)
                k2 = float(item.k2 or 1.0)
                duration = int(round((item.duration_snapshot or 0) * k1))
                price = int(round((item.price_snapshot or 0) * k2))
                order_item = WorkOrderItem(
                    work_order=order,
                    title=item.work.title,
                    duration=duration,
                    price=price,
                    comment=item.extra,
                )
                db.session.add(order_item)
                if order.total_amount is None:
                    order.total_amount = 0
                order.total_amount += price

            db.session.commit()
            flash("Заказ-наряд создан", "success")

        db.session.commit()
        flash("Заявка успешно обновлена", "success")
        return redirect(url_for("admin.appointment_detail", appointment_id=appointment.id))

    issue_media_slots, issue_media_hash_val = _admin_issue_media_bundle(appointment)

    cl = appointment.client
    prompt_templates = db.session.execute(
        db.select(AiPromptTemplate)
        .where(AiPromptTemplate.is_active.is_(True))
        .order_by(AiPromptTemplate.title.asc())
    ).scalars().all()
    prompt_templates_json = [
        {
            "id": int(t.id),
            "title": t.title,
            "body_md": t.body_md or "",
            "is_active": 1 if t.is_active else 0,
        }
        for t in prompt_templates
    ]
    ai_questions_rows = db.session.execute(
        db.select(AppointmentAiQuestion)
        .where(AppointmentAiQuestion.appointment_id == appointment.id)
        .order_by(AppointmentAiQuestion.id.asc())
    ).scalars().all()
    ai_questions = []
    for q in ai_questions_rows:
        try:
            opts = json.loads(q.options_json or "[]")
        except Exception:
            opts = []
        if not isinstance(opts, list):
            opts = []
        ai_questions.append(
            {
                "id": int(q.id),
                "question": q.question,
                "options": [str(x) for x in opts if str(x or "").strip()],
                "client_answer": q.client_answer or "",
            }
        )
    return render_template(
        "admin/appointments/detail.html",
        appointment=appointment,
        form=form,
        item_form=item_form,
        categories=categories_list,
        sections=sections,
        categories_by_section=categories_by_section,
        works_by_category=works_by_category,
        problem_description_hash=problem_description_hash(appointment.problem_description),
        issue_media_slots=issue_media_slots,
        issue_media_hash=issue_media_hash_val,
        current_slot_ids=current_slot_ids,
        client_wa_url=client_whatsapp_url(cl) if cl else None,
        client_tg_url=client_telegram_url(cl) if cl else None,
        client_user=cl,
        prompt_templates=prompt_templates,
        prompt_templates_json=prompt_templates_json,
        ai_questions=ai_questions,
    )


@bp.post("/appointments/<int:appointment_id>/email-client")
@admin_required
def appointment_email_client(appointment_id: int):
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment:
        abort(404)
    client = appointment.client
    if not client:
        flash("Клиент не найден.", "danger")
        return redirect(url_for("admin.appointments"))

    to = (client.client_email or "").strip()
    if not to:
        flash("У клиента не указан email в профиле.", "warning")
        return redirect(url_for("admin.appointment_detail", appointment_id=appointment_id))

    settings_obj = OrganizationSettings.get_settings()
    subject = f"Заявка №{appointment.id}"
    body = (
        f"Здравствуйте, {client.name or 'клиент'}!\n\n"
        f"Пишем по вашей заявке №{appointment.id}.\n"
        f"При необходимости ответьте на это письмо.\n"
    )
    try:
        send_organization_email([to], subject, body, settings=settings_obj)
        flash(f"Письмо отправлено на {to}", "success")
    except MailConfigurationError as e:
        flash(str(e), "danger")
    except smtplib.SMTPException as e:
        flash(f"Ошибка SMTP: {e}", "danger")
    except OSError as e:
        flash(f"Сеть / соединение: {e}", "danger")
    except Exception as e:
        flash(f"Не удалось отправить: {e}", "danger")

    return redirect(url_for("admin.appointment_detail", appointment_id=appointment_id))


@bp.get("/appointments/<int:appointment_id>/available-slots-json")
@admin_required
def appointment_available_slots_json(appointment_id: int):
    master_id = request.args.get("master_id", type=int)
    if not master_id:
        return jsonify({"ok": False, "message": "Укажите мастера."}), 400
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment:
        return jsonify({"ok": False, "message": "Заявка не найдена."}), 404

    now_ts = datetime.now()
    last_calendar_day = now_ts.date() + timedelta(days=60)
    end_range = datetime.combine(last_calendar_day, time.max)

    sub_other = db.select(AppointmentSlot.slot_id).where(AppointmentSlot.appointment_id != appointment_id)

    slot_rows = db.session.execute(
        db.select(TimeSlot)
        .where(TimeSlot.master_id == master_id)
        .where(TimeSlot.start_at >= now_ts)
        .where(TimeSlot.start_at <= end_range)
        .where(TimeSlot.status == "free")
        .where(~TimeSlot.id.in_(sub_other))
        .order_by(TimeSlot.start_at.asc())
    ).scalars().all()

    selected_slot_ids: list[int] = []
    for ap_slot in appointment.slots:
        if ap_slot.slot and ap_slot.slot.master_id == master_id:
            selected_slot_ids.append(ap_slot.slot_id)
    selected_slot_ids = list(dict.fromkeys(selected_slot_ids))
    sel_ts = [db.session.get(TimeSlot, sid) for sid in selected_slot_ids]
    sel_ts = [x for x in sel_ts if x]
    sel_ts.sort(key=lambda s: s.start_at)
    selected_slot_ids = [s.id for s in sel_ts]

    seen = {s.id for s in slot_rows}
    for sid in selected_slot_ids:
        if sid not in seen:
            extra = db.session.get(TimeSlot, sid)
            if extra and extra.master_id == master_id:
                slot_rows = list(slot_rows) + [extra]
                seen.add(sid)
    slot_rows = sorted(slot_rows, key=lambda x: x.start_at)

    weekdays_ru = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    slots_payload = []
    for s in slot_rows:
        st = s.start_at
        en = s.end_at
        wd = weekdays_ru[st.weekday()]
        label = f"{st.strftime('%d.%m.%Y')}  {wd}  {st.strftime('%H:%M')}–{en.strftime('%H:%M')}"
        slots_payload.append(
            {
                "id": s.id,
                "start_at": st.strftime("%Y-%m-%dT%H:%M:%S"),
                "end_at": en.strftime("%Y-%m-%dT%H:%M:%S"),
                "label": label,
            }
        )

    all_master_slots = db.session.execute(
        db.select(TimeSlot)
        .where(TimeSlot.master_id == master_id)
        .where(TimeSlot.start_at >= now_ts)
        .where(TimeSlot.start_at <= end_range)
        .order_by(TimeSlot.start_at.asc())
    ).scalars().all()

    slot_to_appt: dict[int, int] = {}
    if all_master_slots:
        mids = [s.id for s in all_master_slots]
        for sid, aid in db.session.execute(
            db.select(AppointmentSlot.slot_id, AppointmentSlot.appointment_id).where(AppointmentSlot.slot_id.in_(mids))
        ).all():
            slot_to_appt[int(sid)] = int(aid)

    def _timeline_state(ts: TimeSlot) -> str:
        if (ts.status or "") == "blocked":
            return "blocked"
        aid = slot_to_appt.get(ts.id)
        if not aid:
            return "free"
        if aid == appointment_id:
            return "self"
        return "other"

    by_day_tl: dict[str, list[dict]] = defaultdict(list)
    today_iso = now_ts.date().isoformat()
    for ts in all_master_slots:
        dk = ts.start_at.date().isoformat()
        if dk < today_iso:
            continue
        st = ts.start_at
        en = ts.end_at
        by_day_tl[dk].append(
            {
                "id": ts.id,
                "start_at": st.strftime("%Y-%m-%dT%H:%M:%S"),
                "end_at": en.strftime("%Y-%m-%dT%H:%M:%S"),
                "state": _timeline_state(ts),
            }
        )

    day_timelines = []
    for dk in sorted(by_day_tl.keys()):
        segs = sorted(by_day_tl[dk], key=lambda x: x["start_at"])
        day_timelines.append({"date": dk, "segments": segs})

    return jsonify(
        {
            "ok": True,
            "selected_slot_ids": selected_slot_ids,
            "slots": slots_payload,
            "day_timelines": day_timelines,
        }
    )


@bp.get("/appointments/<int:appointment_id>/work-tree-json")
@admin_required
def appointment_work_tree_json(appointment_id: int):
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment:
        return jsonify({"ok": False, "message": "Заявка не найдена."}), 404

    categories_list = db.session.execute(
        db.select(WorkCategory).order_by(
            WorkCategory.competency_id.asc(), WorkCategory.sort_order.asc(), WorkCategory.title.asc()
        )
    ).scalars().all()

    sections = db.session.execute(
        db.select(Competency).order_by(Competency.sort_order.asc(), Competency.title.asc())
    ).scalars().all()

    works_tree = db.session.execute(
        db.select(Work)
        .where(Work.is_active == True)
        .order_by(Work.category_id.asc(), Work.sort_order.asc(), Work.title.asc())
    ).scalars().all()

    categories_by_section: dict[int | None, list[WorkCategory]] = {}
    for cat in categories_list:
        categories_by_section.setdefault(cat.competency_id, []).append(cat)

    works_by_category: dict[int, list[Work]] = {}
    for w in works_tree:
        works_by_category.setdefault(w.category_id, []).append(w)

    sections_payload = []
    for section in sections:
        cats_payload = []
        for cat in categories_by_section.get(section.id, []):
            works_payload = []
            for w in works_by_category.get(cat.id, []):
                works_payload.append(
                    {
                        "id": int(w.id),
                        "title": w.title,
                        "duration_min": int(w.duration_min or 0),
                        "base_price": int(w.base_price or 0),
                    }
                )
            cats_payload.append({"id": int(cat.id), "title": cat.title, "works": works_payload})
        sections_payload.append({"id": int(section.id), "title": section.title, "categories": cats_payload})

    return jsonify({"ok": True, "sections": sections_payload})


@bp.get("/appointments/<int:appointment_id>/items/status-json")
@admin_required
def appointment_items_status_json(appointment_id: int):
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment:
        return jsonify({"ok": False, "message": "Заявка не найдена."}), 404

    rows = db.session.execute(
        db.select(
            AppointmentItem.id,
            AppointmentItem.price_snapshot,
            AppointmentItem.k2,
            AppointmentItem.qty,
            AppointmentItem.duration_snapshot,
            AppointmentItem.k1,
            Work.duration_min,
            AppointmentItem.declined_by_client,
        )
        .join(Work, Work.id == AppointmentItem.work_id)
        .where(AppointmentItem.appointment_id == appointment.id)
        .order_by(AppointmentItem.id.asc())
    ).all()

    items = []
    total_price = 0
    total_duration = 0
    for item_id, price_snapshot, k2, qty, duration_snapshot, k1, duration_min, declined_by_client in rows:
        declined = bool(declined_by_client)
        items.append({"id": int(item_id), "declined_by_client": declined})
        if declined:
            continue
        q = int(qty or 1)
        p = int(price_snapshot or 0)
        d = int(duration_snapshot or duration_min or 0)
        total_price += int(round(p * float(k2 or 1.0) * q))
        total_duration += int(round(d * float(k1 or 1.0) * q))

    issue_media_slots, im_hash = _admin_issue_media_bundle(appointment)

    return jsonify(
        {
            "ok": True,
            "items": items,
            "total_price": int(total_price),
            "total_duration": int(total_duration),
            "problem_hash": problem_description_hash(appointment.problem_description),
            "problem_description": appointment.problem_description or "",
            "win_number": appointment.win_number or "",
            "issue_media": issue_media_slots,
            "issue_media_hash": im_hash,
        }
    )


@bp.get("/appointments/<int:appointment_id>/issue-media/<int:media_id>/file")
@admin_required
def appointment_issue_media_file(appointment_id: int, media_id: int):
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment:
        abort(404)
    m = db.session.get(AppointmentIssueMedia, media_id)
    if not m or m.appointment_id != appointment.id:
        abort(404)
    directory = current_app.config["DOCUMENTS_DIR"]
    rel = m.storage_path.replace("/", os.sep)
    folder = os.path.dirname(rel)
    name = os.path.basename(rel)
    return send_from_directory(os.path.join(directory, folder), name, mimetype=m.mime)


@bp.post("/appointments/<int:appointment_id>/issue-media/<int:media_id>/delete-json")
@admin_required
def appointment_issue_media_delete_json(appointment_id: int, media_id: int):
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment:
        return jsonify({"ok": False, "message": "Заявка не найдена."}), 404

    m = db.session.get(AppointmentIssueMedia, media_id)
    if not m or m.appointment_id != appointment.id:
        return jsonify({"ok": False, "message": "Файл не найден."}), 404

    delete_appointment_issue_media_file(m)
    db.session.delete(m)
    db.session.commit()

    issue_media_slots, im_hash = _admin_issue_media_bundle(appointment)
    return jsonify(
        {
            "ok": True,
            "issue_media_hash": im_hash,
            "issue_media": issue_media_slots,
        }
    )


@bp.post("/appointments/<int:appointment_id>/problem-description-json")
@admin_required
def appointment_problem_description_json(appointment_id: int):
    """Мгновенное сохранение текста неисправностей для синхронизации с кабинетом клиента (опрос snapshot-json)."""
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment:
        return jsonify({"ok": False, "message": "Заявка не найдена."}), 404

    payload = request.get_json(silent=True) or {}
    if "problem_description" not in payload:
        return jsonify({"ok": False, "message": "Нет поля problem_description."}), 400

    appointment.problem_description = str(payload.get("problem_description") or "")
    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "problem_hash": problem_description_hash(appointment.problem_description),
        }
    )


@bp.get("/appointments/<int:appointment_id>/snapshot-json")
@admin_required
def appointment_snapshot_json(appointment_id: int):
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment:
        return jsonify({"ok": False, "message": "Заявка не найдена."}), 404
    return jsonify(
        {
            "ok": True,
            "car_make": appointment.car_make or "",
            "car_model": appointment.car_model or "",
            "car_year": int(appointment.car_year) if appointment.car_year is not None else None,
            "car_number": appointment.car_number or "",
            "win_number": appointment.win_number or "",
            "engine_type": appointment.engine_type or "",
            "has_turbo": bool(appointment.has_turbo) if appointment.has_turbo is not None else None,
            "engine_volume_l": float(appointment.engine_volume_l) if appointment.engine_volume_l is not None else None,
            "transmission_type": appointment.transmission_type or "",
            "mileage_km": int(appointment.mileage_km) if appointment.mileage_km is not None else None,
        }
    )


@bp.post("/appointments/<int:appointment_id>/ai-analyze-json")
@admin_required
@csrf.exempt
def appointment_ai_analyze_json(appointment_id: int):
    """ИИ-анализ заявки (админка): строим промпт из данных заявки и получаем ответ."""
    try:
        appointment = db.session.execute(
            db.select(Appointment)
            .where(Appointment.id == appointment_id)
            .options(selectinload(Appointment.client), selectinload(Appointment.master))
        ).scalar_one_or_none()
        if not appointment:
            return jsonify({"ok": False, "message": "Заявка не найдена."}), 404

        payload = request.get_json(silent=True) or {}
        mode = str(payload.get("mode") or "prompt").strip().lower()
        custom_prompt = str(payload.get("prompt") or "").strip()
        template_id = payload.get("template_id", None)
        messages_in = payload.get("messages", None)

        car_make = (appointment.car_make or "").strip()
        car_model = (appointment.car_model or "").strip()
        car_year = str(appointment.car_year or "").strip()
        win = (appointment.win_number or "").strip()
        car_number = (appointment.car_number or "").strip()
        issues_text = (appointment.problem_description or "").strip()
        client_name = (appointment.client.name if appointment.client else "") or ""

        base_prompt = (
            "Роль: автоэлектрик-диагност.\n"
            "Задача: гипотезы причин, вопросы для уточнения, план диагностики, риски. "
            "Не делай выводов без проверок.\n\n"
            f"Клиент: {client_name}\n"
            f"Авто: {car_make} {car_model} {car_year}\n"
            f"Госномер: {car_number}\n"
            f"VIN/WIN: {win}\n"
            f"Статус: {appointment.status}\n\n"
            "Неисправности:\n"
            f"{issues_text or '(не указано)'}\n\n"
            "Ответ:\n"
            "1) Резюме\n"
            "2) Гипотезы (топ-5)\n"
            "3) Вопросы\n"
            "4) План диагностики\n"
            "5) Риски\n"
            "\n\n"
            "Дополнительно: верни в конце JSON-блок в формате:\n"
            "```json\n"
            "{\n"
            "  \"clarifying_questions\": [\n"
            "    {\"q\": \"...\", \"options\": [\"...\", \"...\", \"...\"]}\n"
            "  ]\n"
            "}\n"
            "```\n"
        )

        settings_obj = OrganizationSettings.get_settings()
        tpl_row = None
        tpl_used_id = None
        try:
            if template_id is not None and str(template_id).strip():
                tpl_used_id = int(template_id)
            else:
                tpl_used_id = int(settings_obj.ai_default_prompt_template_id_appt or 0) or None
        except ValueError:
            tpl_used_id = None
        if tpl_used_id:
            tpl_row = db.session.get(AiPromptTemplate, tpl_used_id)
        ctx = _appt_prompt_context(appointment)
        # include client answers (if any) for follow-up prompts
        answered = db.session.execute(
            db.select(AppointmentAiQuestion)
            .where(
                AppointmentAiQuestion.appointment_id == appointment.id,
                AppointmentAiQuestion.client_answer.is_not(None),
            )
            .order_by(AppointmentAiQuestion.id.asc())
        ).scalars().all()
        if answered:
            ctx["CLIENT_QA"] = "\n".join(
                f"- {q.question.strip()}: {str(q.client_answer or '').strip()}"
                for q in answered
                if (q.question or "").strip() and (q.client_answer or "").strip()
            )
        else:
            ctx["CLIENT_QA"] = ""
        prompt = custom_prompt or (_render_prompt_mustache(tpl_row.body_md, ctx) if tpl_row else base_prompt)
        if mode == "prompt":
            return jsonify({"ok": True, "prompt": prompt})

        api_key = ((settings_obj.ai_api_key or "") or (current_app.config.get("OPENAI_API_KEY") or "")).strip()
        model = ((settings_obj.ai_model or "") or (current_app.config.get("OPENAI_MODEL") or "")).strip()
        base_url = ((settings_obj.ai_base_url or "") or (current_app.config.get("OPENAI_BASE_URL") or "")).strip()
        if not api_key:
            return jsonify({"ok": False, "message": "AI отключён: задайте OPENAI_API_KEY в окружении сервера."})

        try:
            extra_headers = {}
            if (settings_obj.ai_site_url or "").strip():
                extra_headers["HTTP-Referer"] = settings_obj.ai_site_url
            if (settings_obj.ai_app_name or "").strip():
                extra_headers["X-Title"] = settings_obj.ai_app_name
            if isinstance(messages_in, list) and messages_in:
                messages = []
                for m in messages_in:
                    if not isinstance(m, dict):
                        continue
                    role = str(m.get("role") or "").strip()
                    content = str(m.get("content") or "")
                    if role not in ("user", "assistant", "system"):
                        continue
                    if not content.strip():
                        continue
                    messages.append({"role": role, "content": content})
                if not messages or messages[0]["role"] != "system":
                    messages = [{"role": "system", "content": prompt}] + messages
            else:
                messages = [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": "Дай ответ по структуре."},
                ]

            answer = openai_chat_completion(
                api_key=api_key,
                base_url=base_url,
                model=model,
                messages=messages,
                temperature=0.2,
                timeout_s=60,
                extra_headers=extra_headers,
            )
        except AiError as e:
            return jsonify({"ok": False, "message": str(e)})

        try:
            log = AiRequestLog(
                appointment_id=int(appointment.id),
                template_id=int(tpl_used_id) if tpl_used_id else None,
                model=model,
                prompt_md=prompt,
                messages_json=json.dumps(messages, ensure_ascii=False),
                answer_text=answer,
            )
            db.session.add(log)
            db.session.commit()
            qrows = _extract_questions_json(answer)
            if qrows:
                for qr in qrows[:20]:
                    db.session.add(
                        AppointmentAiQuestion(
                            appointment_id=int(appointment.id),
                            ai_request_log_id=int(log.id),
                            question=str(qr.get("question") or "").strip(),
                            options_json=json.dumps(qr.get("options") or [], ensure_ascii=False),
                        )
                    )
                db.session.commit()
        except Exception:
            current_app.logger.exception("AI request log failed")

        return jsonify({"ok": True, "prompt": prompt, "answer": answer, "model": model, "template_id": tpl_used_id})
    except Exception as e:
        current_app.logger.exception("AI appointment endpoint failed")
        return jsonify({"ok": False, "message": f"{type(e).__name__}: {e}"}), 500


@bp.post("/appointments/<int:appointment_id>/items/add")
@admin_required
def appointment_item_add(appointment_id):
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment:
        abort(404)
        
    item_form = AppointmentItemForm()
    works_list = db.session.execute(db.select(Work).where(Work.is_active == True)).scalars().all()
    item_form.work_id.choices = [
        (
            w.id,
            f"{w.category.competency.title + ' / ' if w.category and w.category.competency else ''}{w.category.title if w.category else 'Без категории'} / {w.title}",
        )
        for w in works_list
    ]
    
    if item_form.validate_on_submit():
        work = db.session.get(Work, item_form.work_id.data)
        if work:
            item = AppointmentItem(
                appointment_id=appointment.id,
                work_id=work.id,
                price_snapshot=work.base_price,
                duration_snapshot=work.duration_min
            )
            db.session.add(item)
            db.session.commit()
            flash("Специализация добавлена в заявку", "success")
            
    return redirect(url_for("admin.appointment_detail", appointment_id=appointment_id))


@bp.post("/appointments/<int:appointment_id>/items/add-json")
@admin_required
def appointment_item_add_json(appointment_id):
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment:
        return jsonify({"ok": False, "message": "Заявка не найдена."}), 404

    payload = request.get_json(silent=True) or {}
    work_id = int(payload.get("work_id") or 0)
    if work_id <= 0:
        return jsonify({"ok": False, "message": "Некорректная работа."}), 400

    work = db.session.get(Work, work_id)
    if not work or not work.is_active:
        return jsonify({"ok": False, "message": "Работа не найдена."}), 404

    existing_item = db.session.execute(
        db.select(
            AppointmentItem.id,
            AppointmentItem.k1,
            AppointmentItem.k2,
            AppointmentItem.extra,
            AppointmentItem.qty,
        ).where(
            AppointmentItem.appointment_id == appointment.id,
            AppointmentItem.work_id == work.id,
        )
    ).one_or_none()

    if existing_item:
        existing_item_id, k1, k2, extra, qty = existing_item
        duration_snapshot = int(work.duration_min or 0)
        price_snapshot = int(work.base_price or 0)
        kk1 = float(k1 or 1.0)
        kk2 = float(k2 or 1.0)
        q = int(qty or 1)
        return jsonify(
            {
                "ok": True,
                "already": True,
                "item_id": int(existing_item_id or 0),
                "title": work.title,
                "duration_snapshot": duration_snapshot,
                "price_snapshot": price_snapshot,
                "k1": kk1,
                "k2": kk2,
                "extra": (extra or ""),
                "qty": q,
                "total_duration": int(round(duration_snapshot * kk1 * q)),
                "total_price": int(round(price_snapshot * kk2 * q)),
            }
        )

    item = AppointmentItem(
        appointment_id=appointment.id,
        work_id=work.id,
        price_snapshot=work.base_price,
        duration_snapshot=work.duration_min,
    )
    db.session.add(item)
    db.session.commit()
    duration_snapshot = int(item.duration_snapshot or 0)
    price_snapshot = int(item.price_snapshot or 0)
    kk1 = float(item.k1 or 1.0)
    kk2 = float(item.k2 or 1.0)
    q = int(item.qty or 1)
    return jsonify(
        {
            "ok": True,
            "already": False,
            "item_id": int(item.id),
            "title": work.title,
            "duration_snapshot": duration_snapshot,
            "price_snapshot": price_snapshot,
            "k1": kk1,
            "k2": kk2,
            "extra": (item.extra or ""),
            "qty": q,
            "total_duration": int(round(duration_snapshot * kk1 * q)),
            "total_price": int(round(price_snapshot * kk2 * q)),
        }
    )


@bp.post("/appointments/<int:appointment_id>/items/update-coeffs-json/<int:item_id>")
@admin_required
def appointment_item_update_coeffs_json(appointment_id: int, item_id: int):
    item = db.session.get(AppointmentItem, item_id)
    if not item or item.appointment_id != appointment_id:
        return jsonify({"ok": False, "message": "Позиция не найдена."}), 404

    payload = request.get_json(silent=True) or {}
    k1_raw = payload.get("k1")
    k2_raw = payload.get("k2")

    try:
        k1 = float(k1_raw)
        k2 = float(k2_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "К1/К2 должны быть числами."}), 400

    if k1 <= 0 or k2 <= 0:
        return jsonify({"ok": False, "message": "К1/К2 должны быть больше нуля."}), 400

    item.k1 = k1
    item.k2 = k2
    db.session.commit()
    duration_snapshot = int(item.duration_snapshot or 0)
    price_snapshot = int(item.price_snapshot or 0)
    kk1 = float(item.k1 or 1.0)
    kk2 = float(item.k2 or 1.0)
    q = int(item.qty or 1)
    return jsonify(
        {
            "ok": True,
            "k1": kk1,
            "k2": kk2,
            "qty": q,
            "total_duration": int(round(duration_snapshot * kk1 * q)),
            "total_price": int(round(price_snapshot * kk2 * q)),
        }
    )


@bp.post("/appointments/<int:appointment_id>/items/update-extra-json/<int:item_id>")
@admin_required
def appointment_item_update_extra_json(appointment_id: int, item_id: int):
    item = db.session.get(AppointmentItem, item_id)
    if not item or item.appointment_id != appointment_id:
        return jsonify({"ok": False, "message": "Позиция не найдена."}), 404

    payload = request.get_json(silent=True) or {}
    extra_raw = payload.get("extra")
    extra = (extra_raw or "").strip()
    if extra == "":
        extra = None
    if extra is not None and len(extra) > 255:
        return jsonify({"ok": False, "message": "Слишком длинно (макс 255 символов)."}), 400

    item.extra = extra
    db.session.commit()
    return jsonify({"ok": True, "extra": (item.extra or "")})

@bp.post("/appointments/<int:appointment_id>/items/add-new-work")
@admin_required
def appointment_item_add_new_work(appointment_id):
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment:
        abort(404)

    title_raw = request.form.get("new_work_title", "")
    duration_raw = request.form.get("new_work_duration_min", "")
    price_raw = request.form.get("new_work_base_price", "")
    category_id = request.form.get("new_work_category_id", type=int)

    title = normalize_work_title(title_raw)
    if not title:
        flash("Укажите название работы", "warning")
        return redirect(url_for("admin.appointment_detail", appointment_id=appointment_id))

    try:
        duration_min = int(duration_raw)
    except (TypeError, ValueError):
        flash("Укажите длительность (минуты)", "warning")
        return redirect(url_for("admin.appointment_detail", appointment_id=appointment_id))

    if duration_min < 0:
        flash("Длительность не может быть отрицательной", "warning")
        return redirect(url_for("admin.appointment_detail", appointment_id=appointment_id))

    base_price = None
    if str(price_raw).strip() != "":
        try:
            base_price = int(price_raw)
        except (TypeError, ValueError):
            flash("Стоимость должна быть числом", "warning")
            return redirect(url_for("admin.appointment_detail", appointment_id=appointment_id))
        if base_price < 0:
            flash("Стоимость не может быть отрицательной", "warning")
            return redirect(url_for("admin.appointment_detail", appointment_id=appointment_id))

    category = db.session.get(WorkCategory, category_id or 0)
    if not category:
        flash("Выберите категорию для новой работы", "warning")
        return redirect(url_for("admin.appointment_detail", appointment_id=appointment_id))

    key = work_title_key(title)
    existing_rows = db.session.execute(
        db.select(Work.id, Work.title).where(Work.category_id == category.id)
    ).all()
    existing_work_id = next((wid for wid, t in existing_rows if work_title_key(t) == key), None)

    if existing_work_id:
        work = db.session.get(Work, existing_work_id)
    else:
        next_sort = (
            db.session.execute(
                db.select(db.func.max(Work.sort_order)).where(Work.category_id == category.id)
            ).scalar()
            or 0
        ) + 1
        work = Work(
            category_id=category.id,
            title=title,
            duration_min=duration_min,
            base_price=base_price,
            is_active=True,
            sort_order=next_sort,
        )
        db.session.add(work)
        try:
            db.session.flush()
        except IntegrityError:
            db.session.rollback()
            flash("Не удалось создать работу", "danger")
            return redirect(url_for("admin.appointment_detail", appointment_id=appointment_id))

    item = AppointmentItem(
        appointment_id=appointment.id,
        work_id=work.id,
        price_snapshot=work.base_price,
        duration_snapshot=work.duration_min,
    )
    db.session.add(item)
    db.session.commit()
    flash("Работа добавлена в выбранные услуги и сохранена в базе", "success")
    return redirect(url_for("admin.appointment_detail", appointment_id=appointment_id))

@bp.post("/appointments/<int:appointment_id>/items/delete/<int:item_id>")
@admin_required
def appointment_item_delete(appointment_id, item_id):
    item = db.session.get(AppointmentItem, item_id)
    if item and item.appointment_id == appointment_id:
        db.session.delete(item)
        db.session.commit()
        flash("Специализация удалена из заявки", "success")
        
    return redirect(url_for("admin.appointment_detail", appointment_id=appointment_id))

@bp.post("/appointments/<int:appointment_id>/delete")
@admin_required
def appointment_delete(appointment_id):
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment:
        abort(404)
    
    # SQLAlchemy сама удалит связанный заказ-наряд благодаря cascade="all, delete-orphan"
    db.session.delete(appointment)
    db.session.commit()
    flash("Заявка удалена", "success")
    return redirect(url_for("admin.appointments"))

# --- Заказ-наряды (Work Orders) ---

@bp.get("/work-orders")
@admin_required
def work_orders():
    orders = db.session.execute(
        db.select(WorkOrder).order_by(WorkOrder.id.desc())
    ).scalars().all()
    return render_template("admin/work_orders/index.html", orders=orders)

@bp.post("/work-orders/create-from-appointment/<int:appointment_id>")
@admin_required
def work_order_create(appointment_id):
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment:
        abort(404)
        
    if appointment.work_order:
        flash("Заказ-наряд уже существует", "warning")
        return redirect(url_for("admin.work_order_detail", order_id=appointment.work_order.id))
        
    order = WorkOrder(
        appointment_id=appointment.id,
        client_user_id=appointment.client_user_id,
        master_id=appointment.master_id,
        status="draft",
        total_amount=0,
    )
    db.session.add(order)
    
    # Переносим услуги из заявки в заказ-наряд
    for item in appointment.items:
        if bool(getattr(item, "declined_by_client", False)):
            continue
        k1 = float(item.k1 or 1.0)
        k2 = float(item.k2 or 1.0)
        duration = int(round((item.duration_snapshot or 0) * k1))
        price = int(round((item.price_snapshot or 0) * k2))
        order_item = WorkOrderItem(
            work_order=order,
            title=item.work.title,
            duration=duration,
            price=price,
            comment=item.extra,
        )
        db.session.add(order_item)
        if order.total_amount is None:
            order.total_amount = 0
        order.total_amount += price
        
    db.session.commit()
    flash("Заказ-наряд создан", "success")
    return redirect(url_for("admin.work_order_detail", order_id=order.id))

@bp.route("/work-orders/<int:order_id>", methods=["GET", "POST"])
@admin_required
def work_order_detail(order_id):
    order = db.session.get(WorkOrder, order_id)
    if not order:
        abort(404)
        
    form = WorkOrderForm(obj=order)
    doc_form = DocumentUploadForm()
    item_form = WorkOrderItemForm()
    detail_form = WorkOrderDetailForm()
    material_form = WorkOrderMaterialForm()
    
    # Заполняем список мастеров для выбора исполнителя работы
    masters_list = db.session.execute(db.select(Master).where(Master.is_active == True).order_by(Master.name)).scalars().all()
    item_form.master_id.choices = [(0, "-- Основной мастер --")] + [(m.id, m.name) for m in masters_list]
    
    additional_work_form = WorkOrderAdditionalWorkForm()

    def _inventory_catalog_groups():
        """Каталог из истории запчастей + деталей; второй блок — не «шт» (как материалы)."""
        rows_p = db.session.execute(
            db.select(
                WorkOrderPart.title,
                WorkOrderPart.unit,
                WorkOrderPart.price,
                db.func.count(WorkOrderPart.id),
            )
            .group_by(WorkOrderPart.title, WorkOrderPart.unit, WorkOrderPart.price)
        ).all()
        rows_d = db.session.execute(
            db.select(
                WorkOrderDetail.title,
                WorkOrderDetail.unit,
                WorkOrderDetail.price,
                db.func.count(WorkOrderDetail.id),
            )
            .group_by(WorkOrderDetail.title, WorkOrderDetail.unit, WorkOrderDetail.price)
        ).all()

        buckets: dict[tuple[str, str, int], dict] = {}
        for title, unit, price, cnt in rows_p + rows_d:
            t = (title or "").strip()
            u = (unit or "шт.").strip()
            p = int(price or 0)
            c = int(cnt or 0)
            if not t:
                continue
            key = (t.casefold(), u.casefold(), p)
            if key not in buckets:
                buckets[key] = {"title": t, "unit": u, "price": p, "count": 0}
            buckets[key]["count"] += c
        normalized = list(buckets.values())

        def is_piece_unit(u: str) -> bool:
            v = (u or "").strip().lower()
            return v in {"шт", "шт.", "pcs"}

        details_lines = [x for x in normalized if is_piece_unit(x["unit"])]
        materials_lines = [x for x in normalized if not is_piece_unit(x["unit"])]

        def build_groups(items):
            by_key = {}
            for x in items:
                key = (x["title"].strip().casefold(), x["unit"].strip().casefold())
                prev = by_key.get(key)
                if not prev or x["count"] > prev["count"]:
                    by_key[key] = x

            uniq = list(by_key.values())
            uniq.sort(key=lambda x: (-x["count"], x["title"].casefold()))

            groups = {"ПОПУЛЯРНЫЕ": uniq[:30]}
            for x in uniq:
                ch = x["title"][:1].upper() if x["title"] else "#"
                if ch.isdigit():
                    ch = "#"
                groups.setdefault(ch, []).append(x)
            return groups

        return build_groups(details_lines), build_groups(materials_lines)

    detail_catalog_groups, materials_catalog_groups = _inventory_catalog_groups()

    merged_inventory_rows = merged_work_order_inventory_rows(order)
    selected_materials_list = list(order.materials or [])

    # Всегда пересчитываем итог при открытии, чтобы значение в БД не было устаревшим
    recalculate_work_order_total(order)
    sync_work_order_is_paid_from_cashflow(order)
    db.session.commit()

    if form.validate_on_submit():
        order.status = form.status.data
        order.inspection_results = form.inspection_results.data
        
        recalculate_work_order_total(order)

        if order.status == "closed":
            order.closed_at = datetime.utcnow()
            # Автоматическая запись в книгу приходов при закрытии заказ-наряда
            existing_cash = db.session.execute(
                db.select(CashFlow).where(CashFlow.work_order_id == order.id, CashFlow.amount > 0)
            ).scalar_one_or_none()
            if not existing_cash:
                cash = CashFlow(
                    amount=order.total_amount or 0,
                    category="Оплата услуг",
                    description=f"Оплата заказ-наряда #{order.id}",
                    work_order_id=order.id,
                )
                db.session.add(cash)
        # is_paid только по факту прихода в книге (не «автоматом» при смене статуса)
        sync_work_order_is_paid_from_cashflow(order)

        db.session.commit()
        flash("Заказ-наряд обновлен", "success")
        return redirect(url_for("admin.work_order_detail", order_id=order.id))
        
    order_paid_in_book = work_order_has_positive_cashflow(order.id)
    prompt_templates = db.session.execute(
        db.select(AiPromptTemplate)
        .where(AiPromptTemplate.is_active.is_(True))
        .order_by(AiPromptTemplate.title.asc())
    ).scalars().all()
    prompt_templates_json = [
        {
            "id": int(t.id),
            "title": t.title,
            "body_md": t.body_md or "",
            "is_active": 1 if t.is_active else 0,
        }
        for t in prompt_templates
    ]
    return render_template(
        "admin/work_orders/detail.html",
        order=order,
        order_paid_in_book=order_paid_in_book,
        form=form,
        doc_form=doc_form,
        item_form=item_form,
        additional_work_form=additional_work_form,
        detail_form=detail_form,
        material_form=material_form,
        detail_catalog_groups=detail_catalog_groups,
        materials_catalog_groups=materials_catalog_groups,
        merged_inventory_rows=merged_inventory_rows,
        selected_materials_list=selected_materials_list,
        complaints=order.complaints or [],
        prompt_templates=prompt_templates,
        prompt_templates_json=prompt_templates_json,
    )

@bp.post("/work-orders/<int:order_id>/pay")
@admin_required
def work_order_pay(order_id):
    order = db.session.get(WorkOrder, order_id)
    if not order:
        abort(404)

    if work_order_has_positive_cashflow(order.id):
        flash("По этому заказ-наряду уже есть приход в книге.", "info")
        return redirect(url_for("admin.work_order_detail", order_id=order.id))

    # Создаем запись в CashFlow если её нет
    existing_cash = db.session.execute(
        db.select(CashFlow).where(CashFlow.work_order_id == order.id, CashFlow.amount > 0)
    ).scalar_one_or_none()

    if not existing_cash:
        cash = CashFlow(
            amount=order.total_amount or 0,
            category="Оплата услуг",
            description=f"Оплата заказ-наряда #{order.id}",
            work_order_id=order.id
        )
        db.session.add(cash)

    sync_work_order_is_paid_from_cashflow(order)
    db.session.commit()
    flash("Оплата зафиксирована", "success")
    return redirect(url_for("admin.work_order_detail", order_id=order.id))


@bp.post("/work-orders/<int:order_id>/unpay")
@admin_required
def work_order_unpay(order_id):
    order = db.session.get(WorkOrder, order_id)
    if not order:
        abort(404)

    # Если по книге нет положительного баланса — отменять нечего
    if not work_order_has_positive_cashflow(order.id):
        flash("По этому заказ-наряду нет оплаты в книге.", "info")
        return redirect(url_for("admin.work_order_detail", order_id=order.id))

    from sqlalchemy import func

    paid_sum = db.session.execute(
        db.select(func.coalesce(func.sum(CashFlow.amount), 0)).where(CashFlow.work_order_id == order.id)
    ).scalar_one()
    paid_sum = int(paid_sum or 0)
    if paid_sum <= 0:
        sync_work_order_is_paid_from_cashflow(order)
        db.session.commit()
        flash("Оплата уже отменена.", "info")
        return redirect(url_for("admin.work_order_detail", order_id=order.id))

    cash = CashFlow(
        amount=-paid_sum,
        category="Отмена оплаты",
        description=f"Отмена оплаты заказ-наряда #{order.id}",
        work_order_id=order.id,
    )
    db.session.add(cash)

    sync_work_order_is_paid_from_cashflow(order)
    db.session.commit()
    flash("Оплата отменена (проведена по книге).", "success")
    return redirect(url_for("admin.work_order_detail", order_id=order.id))


@bp.get("/work-orders/<int:order_id>/sbp-qr.png")
@admin_required
def work_order_sbp_qr(order_id: int):
    """QR для оплаты по СБП (технологический: кодирует телефон/сумму/назначение).

    Для production-эквайринга обычно нужен провайдер СБП, который выдаёт payload вида https://qr.nspk.ru/...
    Здесь генерируем QR с понятным payload для ручной оплаты по номеру телефона.
    """
    from io import BytesIO

    import qrcode
    from flask import send_file

    order = db.session.get(WorkOrder, order_id)
    if not order:
        abort(404)

    settings_obj = OrganizationSettings.get_settings()
    phone = (getattr(settings_obj, "sbp_phone", None) or "").strip()
    if not phone:
        abort(404)

    recalculate_work_order_total(order)
    amount = int(order.total_amount or 0)

    # Payload (неофициальный) — чтобы сканер/камера читали текст, а оператор/клиент мог быстро скопировать телефон/сумму.
    payload = f"SBP|PHONE={phone}|AMOUNT={amount}|ORDER={order.id}"

    qr = qrcode.QRCode(border=2, box_size=8)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", download_name=f"work-order-{order.id}-sbp.png", max_age=0)


@bp.post("/work-orders/<int:order_id>/ai-analyze-json")
@admin_required
@csrf.exempt
def work_order_ai_analyze_json(order_id: int):
    """ИИ-анализ заказ-наряда (админка): промпт из жалоб/данных заказа."""
    try:
        order = db.session.execute(
            db.select(WorkOrder)
            .where(WorkOrder.id == order_id)
            .options(
                selectinload(WorkOrder.client),
                selectinload(WorkOrder.master),
                selectinload(WorkOrder.appointment),
                selectinload(WorkOrder.complaints),
                selectinload(WorkOrder.items),
            )
        ).scalar_one_or_none()
        if not order:
            abort(404)

        payload = request.get_json(silent=True) or {}
        mode = str(payload.get("mode") or "prompt").strip().lower()
        custom_prompt = str(payload.get("prompt") or "").strip()
        template_id = payload.get("template_id", None)
        messages_in = payload.get("messages", None)

        client_name = (order.client.name if order.client else "") or ""
        client_phone = (order.client.phone if order.client else "") or ""
        master_name = (order.master.name if order.master else "") or ""
        appt = order.appointment
        car_make = ((appt.car_make if appt else "") or "").strip()
        car_model = ((appt.car_model if appt else "") or "").strip()
        car_year = str(((appt.car_year if appt else "") or "")).strip()
        win = ((appt.win_number if appt else "") or "").strip()
        car_number = ((appt.car_number if appt else "") or "").strip()

        complaints_lines = []
        for c in (order.complaints or []):
            if not c:
                continue
            line = (c.description or "").strip()
            if not line:
                continue
            if c.is_refused and c.refusal_reason:
                line += f" (отказ: {c.refusal_reason})"
            complaints_lines.append(line)

        works_lines = []
        for it in (order.items or []):
            if not it:
                continue
            title = (it.title or "").strip()
            if not title:
                continue
            price = int(it.price or 0)
            done = "✓" if getattr(it, "is_done", False) else "·"
            works_lines.append(f"{done} {title} — {price} руб.")

        recalculate_work_order_total(order)
        db.session.commit()

        base_prompt = (
            "Роль: автоэлектрик-диагност.\n"
            "Задача: гипотезы причин, вопросы для уточнения, план диагностики, рекомендации, риски.\n\n"
            f"Заказ-наряд: №{order.id}\n"
            f"Статус: {order.status}\n"
            f"Клиент: {client_name} ({client_phone})\n"
            f"Мастер: {master_name}\n"
            f"Авто: {car_make} {car_model} {car_year}\n"
            f"Госномер: {car_number}\n"
            f"VIN/WIN: {win}\n"
            f"Сумма: {int(order.total_amount or 0)} руб.\n\n"
            "Жалобы:\n"
            + ("\n".join([f"- {x}" for x in complaints_lines]) if complaints_lines else "(нет жалоб)\n")
            + "\n\nРаботы/позиции:\n"
            + ("\n".join([f"- {x}" for x in works_lines[:25]]) if works_lines else "(позиций нет)\n")
            + "\n\nОтвет:\n"
            "1) Резюме\n"
            "2) Гипотезы (топ-5)\n"
            "3) Вопросы\n"
            "4) План диагностики\n"
            "5) Рекомендации\n"
            "6) Риски\n"
            "\n\n"
            "Дополнительно: верни в конце JSON-блок в формате:\n"
            "```json\n"
            "{\n"
            "  \"clarifying_questions\": [\n"
            "    {\"q\": \"...\", \"options\": [\"...\", \"...\", \"...\"]}\n"
            "  ]\n"
            "}\n"
            "```\n"
        )

        settings_obj = OrganizationSettings.get_settings()
        tpl_row = None
        tpl_used_id = None
        try:
            if template_id is not None and str(template_id).strip():
                tpl_used_id = int(template_id)
            else:
                tpl_used_id = int(settings_obj.ai_default_prompt_template_id_wo or 0) or None
        except ValueError:
            tpl_used_id = None
        if tpl_used_id:
            tpl_row = db.session.get(AiPromptTemplate, tpl_used_id)
        ctx = _wo_prompt_context(order)
        appt_id = int(order.appointment_id) if order.appointment_id else None
        if appt_id:
            answered = db.session.execute(
                db.select(AppointmentAiQuestion)
                .where(
                    AppointmentAiQuestion.appointment_id == appt_id,
                    AppointmentAiQuestion.client_answer.is_not(None),
                )
                .order_by(AppointmentAiQuestion.id.asc())
            ).scalars().all()
            if answered:
                ctx["CLIENT_QA"] = "\n".join(
                    f"- {q.question.strip()}: {str(q.client_answer or '').strip()}"
                    for q in answered
                    if (q.question or "").strip() and (q.client_answer or "").strip()
                )
            else:
                ctx["CLIENT_QA"] = ""
        else:
            ctx["CLIENT_QA"] = ""
        prompt = custom_prompt or (_render_prompt_mustache(tpl_row.body_md, ctx) if tpl_row else base_prompt)
        if mode == "prompt":
            return jsonify({"ok": True, "prompt": prompt})

        api_key = ((settings_obj.ai_api_key or "") or (current_app.config.get("OPENAI_API_KEY") or "")).strip()
        model = ((settings_obj.ai_model or "") or (current_app.config.get("OPENAI_MODEL") or "")).strip()
        base_url = ((settings_obj.ai_base_url or "") or (current_app.config.get("OPENAI_BASE_URL") or "")).strip()
        if not api_key:
            return jsonify({"ok": False, "message": "AI отключён: задайте OPENAI_API_KEY в окружении сервера."})

        try:
            extra_headers = {}
            if (settings_obj.ai_site_url or "").strip():
                extra_headers["HTTP-Referer"] = settings_obj.ai_site_url
            if (settings_obj.ai_app_name or "").strip():
                extra_headers["X-Title"] = settings_obj.ai_app_name
            if isinstance(messages_in, list) and messages_in:
                messages = []
                for m in messages_in:
                    if not isinstance(m, dict):
                        continue
                    role = str(m.get("role") or "").strip()
                    content = str(m.get("content") or "")
                    if role not in ("user", "assistant", "system"):
                        continue
                    if not content.strip():
                        continue
                    messages.append({"role": role, "content": content})
                if not messages or messages[0]["role"] != "system":
                    messages = [{"role": "system", "content": prompt}] + messages
            else:
                messages = [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": "Дай ответ по структуре."},
                ]

            answer = openai_chat_completion(
                api_key=api_key,
                base_url=base_url,
                model=model,
                messages=messages,
                temperature=0.2,
                timeout_s=60,
                extra_headers=extra_headers,
            )
        except AiError as e:
            return jsonify({"ok": False, "message": str(e)})

        try:
            log = AiRequestLog(
                work_order_id=int(order.id),
                appointment_id=int(order.appointment_id) if order.appointment_id else None,
                template_id=int(tpl_used_id) if tpl_used_id else None,
                model=model,
                prompt_md=prompt,
                messages_json=json.dumps(messages, ensure_ascii=False),
                answer_text=answer,
            )
            db.session.add(log)
            db.session.commit()
            appt_id = int(order.appointment_id) if order.appointment_id else None
            if appt_id:
                qrows = _extract_questions_json(answer)
                if qrows:
                    for qr in qrows[:20]:
                        db.session.add(
                            AppointmentAiQuestion(
                                appointment_id=appt_id,
                                ai_request_log_id=int(log.id),
                                question=str(qr.get("question") or "").strip(),
                                options_json=json.dumps(qr.get("options") or [], ensure_ascii=False),
                            )
                        )
                    db.session.commit()
        except Exception:
            current_app.logger.exception("AI request log failed")

        return jsonify({"ok": True, "prompt": prompt, "answer": answer, "model": model, "template_id": tpl_used_id})
    except Exception as e:
        current_app.logger.exception("AI work order endpoint failed")
        return jsonify({"ok": False, "message": f"{type(e).__name__}: {e}"}), 500


def _pdf_safe_text(s: str) -> str:
    return str(s or "").replace("\r\n", "\n").replace("\r", "\n")


def _ensure_pdf_font_registered() -> str:
    """Register a Unicode TTF if available, otherwise fallback to built-in."""
    # Windows-friendly: try DejaVuSans if exists in common locations, else Helvetica.
    candidates = [
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "DejaVuSans.ttf"),
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "arial.ttf"),
    ]
    for p in candidates:
        if p and os.path.exists(p):
            name = "DiagFont"
            try:
                pdfmetrics.registerFont(TTFont(name, p))
                return name
            except Exception:
                continue
    return "Helvetica"


def _render_pdf_lines(c: canvas.Canvas, *, title: str, lines: list[str]) -> None:
    font_name = _ensure_pdf_font_registered()
    width, height = A4
    margin = 48
    y = height - margin
    c.setFont(font_name, 14)
    c.drawString(margin, y, title[:120])
    y -= 24
    c.setFont(font_name, 10)

    for raw in lines:
        txt = _pdf_safe_text(raw)
        for part in txt.split("\n"):
            if y < margin:
                c.showPage()
                width, height = A4
                y = height - margin
                c.setFont(font_name, 10)
            c.drawString(margin, y, part[:180])
            y -= 14


@bp.post("/work-orders/<int:order_id>/ai-chat/save-pdf-json")
@admin_required
@csrf.exempt
def work_order_ai_chat_save_pdf_json(order_id: int):
    """Сохранить чат ИИ в PDF и прикрепить к заказ-наряду."""
    order = db.session.get(WorkOrder, order_id)
    if not order:
        abort(404)

    payload = request.get_json(silent=True) or {}
    messages = payload.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return jsonify({"ok": False, "message": "Нет сообщений для сохранения."}), 400

    # Prepare text
    lines = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip()
        content = str(m.get("content") or "")
        if not content.strip():
            continue
        who = "СИСТЕМА" if role == "system" else ("ПОЛЬЗОВАТЕЛЬ" if role == "user" else "ИИ")
        lines.append(f"{who}:")
        lines.append(content.strip())
        lines.append("")

    if not lines:
        return jsonify({"ok": False, "message": "Сообщения пустые."}), 400

    # Save PDF file into documents/<order_id>/
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"AI_CHAT_WORK_ORDER_{order.id}_{ts}.pdf"
    order_dir = os.path.join(current_app.config["DOCUMENTS_DIR"], str(order.id))
    os.makedirs(order_dir, exist_ok=True)
    file_path = os.path.join(order_dir, filename)

    try:
        c = canvas.Canvas(file_path, pagesize=A4)
        _render_pdf_lines(c, title=f"ИИ-чат · Заказ-наряд №{order.id}", lines=lines)
        c.save()
        size_bytes = os.path.getsize(file_path) if os.path.exists(file_path) else 0
    except Exception as e:
        current_app.logger.exception("PDF generation failed")
        return jsonify({"ok": False, "message": f"PDF error: {e}"}), 500

    doc = WorkOrderDocument(
        work_order=order,
        filename=filename,
        mime="application/pdf",
        storage_path=os.path.relpath(file_path, current_app.config["DOCUMENTS_DIR"]).replace(os.path.sep, "/"),
        size_bytes=int(size_bytes or 0),
    )
    db.session.add(doc)
    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "doc_id": doc.id,
            "filename": doc.filename,
            "storage_path": doc.storage_path,
            "download_url": url_for("admin.get_document", filename=doc.storage_path),
        }
    )


def _ai_chat_messages_to_md(messages: list) -> str:
    parts: list[str] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip() or "assistant"
        content = str(m.get("content") or "")
        if not content.strip():
            continue
        who = "Система" if role == "system" else ("Пользователь" if role == "user" else "ИИ")
        parts.append(f"## {who}\n\n{content.strip()}\n")
    return "\n".join(parts).strip() + "\n"


@bp.post("/work-orders/<int:order_id>/ai-chat/save-md-json")
@admin_required
def work_order_ai_chat_save_md_json(order_id: int):
    """Сохранить чат ИИ в Markdown и прикрепить к заказ-наряду."""
    order = db.session.get(WorkOrder, order_id)
    if not order:
        abort(404)

    payload = request.get_json(silent=True) or {}
    messages = payload.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return jsonify({"ok": False, "message": "Нет сообщений для сохранения."}), 400

    md = _ai_chat_messages_to_md(messages)
    if not md.strip():
        return jsonify({"ok": False, "message": "Сообщения пустые."}), 400

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"AI_CHAT_WORK_ORDER_{order.id}_{ts}.md"
    order_dir = os.path.join(current_app.config["DOCUMENTS_DIR"], str(order.id))
    os.makedirs(order_dir, exist_ok=True)
    file_path = os.path.join(order_dir, filename)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(md)
    size_bytes = os.path.getsize(file_path) if os.path.exists(file_path) else 0

    doc = WorkOrderDocument(
        work_order=order,
        filename=filename,
        mime="text/markdown",
        storage_path=os.path.relpath(file_path, current_app.config["DOCUMENTS_DIR"]).replace(os.path.sep, "/"),
        size_bytes=int(size_bytes or 0),
    )
    db.session.add(doc)
    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "doc_id": doc.id,
            "filename": doc.filename,
            "storage_path": doc.storage_path,
            "download_url": url_for("admin.get_document", filename=doc.storage_path),
        }
    )


@bp.post("/appointments/<int:appointment_id>/ai-chat/save-md-json")
@admin_required
def appointment_ai_chat_save_md_json(appointment_id: int):
    """Сохранить чат ИИ в Markdown и прикрепить к заявке."""
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment:
        abort(404)

    payload = request.get_json(silent=True) or {}
    messages = payload.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return jsonify({"ok": False, "message": "Нет сообщений для сохранения."}), 400

    md = _ai_chat_messages_to_md(messages)
    if not md.strip():
        return jsonify({"ok": False, "message": "Сообщения пустые."}), 400

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"AI_CHAT_APPOINTMENT_{appointment.id}_{ts}.md"
    subdir = os.path.join("appointments", str(appointment.id))
    abs_dir = os.path.join(current_app.config["DOCUMENTS_DIR"], subdir)
    os.makedirs(abs_dir, exist_ok=True)
    file_path = os.path.join(abs_dir, filename)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(md)
    size_bytes = os.path.getsize(file_path) if os.path.exists(file_path) else 0

    rel_path = os.path.relpath(file_path, current_app.config["DOCUMENTS_DIR"]).replace(os.path.sep, "/")
    doc = AppointmentDocument(
        appointment_id=appointment.id,
        filename=filename,
        mime="text/markdown",
        storage_path=rel_path,
        size_bytes=int(size_bytes or 0),
    )
    db.session.add(doc)
    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "doc_id": doc.id,
            "filename": doc.filename,
            "storage_path": doc.storage_path,
            "download_url": url_for("admin.get_document", filename=doc.storage_path),
        }
    )

@bp.get("/work-orders/<int:order_id>/print")
@admin_required
def work_order_print(order_id):
    order = db.session.get(WorkOrder, order_id)
    if not order:
        abort(404)

    recalculate_work_order_total(order)
    db.session.commit()

    merged_inventory_rows = merged_work_order_inventory_rows(order)
    settings = OrganizationSettings.get_settings()
    org = (settings.name or "").strip()
    org_disp = org or "Сервис"
    print_abs = url_for("admin.work_order_print", order_id=order.id, _external=True)
    client_email = ""
    if order.client:
        client_email = (order.client.client_email or "").strip()
    telegram_delivery = session.pop("telegram_print_delivery", None)
    if not telegram_delivery or int(telegram_delivery.get("order_id", 0)) != int(order.id):
        telegram_delivery = None
    return render_template(
        "admin/work_orders/print.html",
        order=order,
        settings=settings,
        merged_inventory_rows=merged_inventory_rows,
        selected_materials_list=list(order.materials or []),
        print_back_url=url_for("admin.work_order_detail", order_id=order.id),
        print_back_label="← Вернуться к заказу",
        whatsapp_share_href=work_order_whatsapp_share_href(order, org_disp, print_abs),
        telegram_delivery=telegram_delivery,
        telegram_code_post_url=url_for("admin.work_order_print_telegram_code", order_id=order.id),
        telegram_bot_name=get_telegram_bot_username(),
        send_email_url=url_for("admin.work_order_print_send_email", order_id=order.id),
        send_email_available=bool(client_email),
        send_email_hint="Укажите email клиента в карточке клиента",
    )


@bp.post("/work-orders/<int:order_id>/print/telegram-code")
@admin_required
def work_order_print_telegram_code(order_id):
    order = db.session.get(WorkOrder, order_id)
    if not order:
        abort(404)
    code = issue_work_order_telegram_code(order.id)
    bot = get_telegram_bot_username()
    msg = build_zakaz_delivery_message(order=order, code=code, bot_username=bot)
    link = db.session.execute(
        db.select(TelegramLink).where(
            TelegramLink.user_id == order.client_user_id,
            TelegramLink.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if link and get_telegram_bot_token():
        try:
            telegram_bot_send_message(link.telegram_chat_id, msg)
            flash("Сообщение с адресом бота и кодом отправлено клиенту в Telegram.", "success")
        except Exception as e:
            flash(f"Не удалось отправить в Telegram автоматически: {e}", "warning")
    elif not link:
        flash("Клиент не привязал Telegram в кабинете — передайте текст вручную.", "warning")
    else:
        flash("Укажите токен бота в разделе «Связь», чтобы отправить сообщение через API.", "warning")

    session["telegram_print_delivery"] = {
        "order_id": order.id,
        "code": code,
        "bot": bot,
        "message": msg,
    }
    return redirect(url_for("admin.work_order_print", order_id=order.id))


@bp.post("/work-orders/<int:order_id>/print/send-email")
@admin_required
def work_order_print_send_email(order_id):
    order = db.session.get(WorkOrder, order_id)
    if not order:
        abort(404)
    if not order.client:
        flash("Нет данных клиента.", "danger")
        return redirect(url_for("admin.work_order_print", order_id=order_id))
    to = (order.client.client_email or "").strip()
    if not to:
        flash("У клиента не указан email для связи.", "warning")
        return redirect(url_for("admin.work_order_print", order_id=order_id))
    settings = OrganizationSettings.get_settings()
    recalculate_work_order_total(order)
    db.session.commit()
    merged_inventory_rows = merged_work_order_inventory_rows(order)
    subject = f"Заказ-наряд №{order.id}"
    body_plain = work_order_share_text(order, (settings.name or "").strip() or "Сервис", max_length=8000)
    body_html = render_template(
        "email/work_order_compact.html",
        order=order,
        settings=settings,
        merged_inventory_rows=merged_inventory_rows,
        selected_materials_list=list(order.materials or []),
    )
    try:
        send_organization_email([to], subject, body_plain, body_html=body_html, settings=settings)
        flash(f"Письмо отправлено на {to}", "success")
    except MailConfigurationError as e:
        flash(str(e), "danger")
    except (OSError, smtplib.SMTPException) as e:
        flash(f"Ошибка отправки почты: {e}", "danger")
    return redirect(url_for("admin.work_order_print", order_id=order_id))


@bp.post("/work-orders/<int:order_id>/items/add")
@admin_required
def work_order_item_add(order_id):
    order = db.session.get(WorkOrder, order_id)
    if not order:
        abort(404)
    
    item_form = WorkOrderItemForm()
    masters_list = db.session.execute(db.select(Master).where(Master.is_active == True).order_by(Master.name)).scalars().all()
    item_form.master_id.choices = [(0, "-- Основной мастер --")] + [(m.id, m.name) for m in masters_list]
    
    if item_form.validate_on_submit():
        item = WorkOrderItem(
            work_order=order,
            title=item_form.title.data,
            duration=item_form.duration.data,
            actual_duration=item_form.actual_duration.data,
            price=item_form.price.data,
            is_done=item_form.is_done.data,
            comment=item_form.comment.data,
            master_id=item_form.master_id.data if item_form.master_id.data != 0 else None
        )
        db.session.add(item)
        
        # Автоматический пересчет суммы
        if order.total_amount is None:
            order.total_amount = 0
        order.total_amount += item.price
        
        db.session.commit()
        flash("Работа добавлена", "success")
    
    return redirect(url_for("admin.work_order_detail", order_id=order.id))

@bp.post("/work-orders/<int:order_id>/items/delete/<int:item_id>")
@admin_required
def work_order_item_delete(order_id, item_id):
    item = db.session.get(WorkOrderItem, item_id)
    if item and item.work_order_id == order_id:
        order = item.work_order
        if order.total_amount is not None:
            order.total_amount -= item.price
            
        db.session.delete(item)
        db.session.commit()
        flash("Работа удалена", "success")
        
    return redirect(url_for("admin.work_order_detail", order_id=order_id))

@bp.post("/work-orders/<int:order_id>/items/update/<int:item_id>")
@admin_required
def work_order_item_update(order_id, item_id):
    item = db.session.get(WorkOrderItem, item_id)
    if not item or item.work_order_id != order_id:
        abort(404)
        
    if "update_is_done" in request.form:
        item.is_done = request.form.get("is_done") == "on"
        # Если чекбокс отмечен и факт еще не заполнен, подставляем норму
        if item.is_done and (not item.actual_duration or item.actual_duration == 0):
            item.actual_duration = item.duration
            
        # Если чекбокс снят, возвращаем статус в "opened" (В работе)
        if not item.is_done and item.work_order.status == "closed":
            item.work_order.status = "opened"
        elif not item.is_done and item.work_order.status == "draft":
            # Если был черновик, возможно стоит оставить или перевести в opened
            pass
    
    if "duration" in request.form:
        try:
            item.duration = int(request.form.get("duration"))
        except (ValueError, TypeError):
            pass

    if "master_id" in request.form:
        m_id = int(request.form.get("master_id"))
        item.master_id = m_id if m_id != 0 else None
    
    if "comment" in request.form:
        item.comment = request.form.get("comment")

    if "actual_duration" in request.form:
        try:
            item.actual_duration = int(request.form.get("actual_duration"))
        except (ValueError, TypeError):
            pass
        
    db.session.commit()
    return redirect(url_for("admin.work_order_detail", order_id=order_id))

@bp.post("/work-orders/<int:order_id>/parts/add")
@admin_required
def work_order_part_add(order_id):
    order = db.session.get(WorkOrder, order_id)
    if not order:
        abort(404)

    form = WorkOrderDetailForm()
    if form.validate_on_submit():
        detail = WorkOrderDetail(
            work_order=order,
            title=form.title.data,
            quantity=form.quantity.data,
            unit=form.unit.data,
            price=form.price.data,
        )
        db.session.add(detail)
        recalculate_work_order_total(order)
        db.session.commit()
        flash("Позиция добавлена в детали", "success")

    return redirect(url_for("admin.work_order_detail", order_id=order.id))


@bp.post("/work-orders/<int:order_id>/parts/add-json")
@admin_required
def work_order_part_add_json(order_id):
    order = db.session.get(WorkOrder, order_id)
    if not order:
        return jsonify({"ok": False, "message": "Заказ-наряд не найден."}), 404

    payload = request.get_json(silent=True) or {}
    title = normalize_work_title(payload.get("title", ""))
    unit = (payload.get("unit") or "шт.").strip()
    price_raw = payload.get("price")
    qty_raw = payload.get("quantity")

    if not title:
        return jsonify({"ok": False, "message": "Укажите наименование."}), 400

    try:
        quantity = float(qty_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Количество должно быть числом."}), 400
    if quantity <= 0:
        return jsonify({"ok": False, "message": "Количество должно быть больше нуля."}), 400

    try:
        price = int(price_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Цена должна быть числом."}), 400
    if price < 0:
        return jsonify({"ok": False, "message": "Цена не может быть отрицательной."}), 400

    existing, ek = _find_duplicate_inventory_line(order, title, unit)
    if existing:
        if ek == "detail":
            return jsonify({"ok": True, "already": True, "detail_id": int(existing.id)}), 200
        return jsonify({"ok": True, "already": True, "part_id": int(existing.id)}), 200

    detail = WorkOrderDetail(work_order=order, title=title, quantity=quantity, unit=unit, price=price)
    db.session.add(detail)
    order_total = recalculate_work_order_total(order)

    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "already": False,
            "detail_id": int(detail.id),
            "part_id": int(detail.id),
            "title": detail.title,
            "quantity": float(detail.quantity or 0),
            "unit": detail.unit or "",
            "price": int(detail.price or 0),
            "total": int((detail.price or 0) * (detail.quantity or 0)),
            "order_total": order_total,
        }
    )


@bp.post("/work-orders/<int:order_id>/parts/update-json/<int:part_id>")
@admin_required
def work_order_part_update_json(order_id, part_id):
    part = db.session.get(WorkOrderPart, part_id)
    if not part or part.work_order_id != order_id:
        return jsonify({"ok": False, "message": "Запчасть не найдена."}), 404

    payload = request.get_json(silent=True) or {}
    title = normalize_work_title(payload.get("title", ""))
    unit = (payload.get("unit") or "шт.").strip()
    price_raw = payload.get("price")
    qty_raw = payload.get("quantity")

    if not title:
        return jsonify({"ok": False, "message": "Укажите наименование."}), 400

    try:
        quantity = float(qty_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Количество должно быть числом."}), 400
    if quantity <= 0:
        return jsonify({"ok": False, "message": "Количество должно быть больше нуля."}), 400

    try:
        price = int(price_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Цена должна быть числом."}), 400
    if price < 0:
        return jsonify({"ok": False, "message": "Цена не может быть отрицательной."}), 400

    part.title = title
    part.quantity = quantity
    part.unit = unit
    part.price = price

    order_total = recalculate_work_order_total(part.work_order)

    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "part_id": int(part.id),
            "title": part.title,
            "quantity": float(part.quantity or 0),
            "unit": part.unit or "",
            "price": int(part.price or 0),
            "total": int((part.price or 0) * (part.quantity or 0)),
            "order_total": order_total,
        }
    )


@bp.post("/work-orders/<int:order_id>/details/add")
@admin_required
def work_order_detail_add(order_id):
    order = db.session.get(WorkOrder, order_id)
    if not order:
        abort(404)
    
    form = WorkOrderDetailForm()
    if form.validate_on_submit():
        detail = WorkOrderDetail(
            work_order=order,
            title=form.title.data,
            quantity=form.quantity.data,
            unit=form.unit.data,
            price=form.price.data
        )
        db.session.add(detail)
        
        # Автоматический пересчет суммы
        total_detail_price = int(detail.price * detail.quantity)
        if order.total_amount is None:
            order.total_amount = 0
        order.total_amount += total_detail_price
        
        db.session.commit()
        flash("Деталь добавлена", "success")
    
    return redirect(url_for("admin.work_order_detail", order_id=order.id))


@bp.post("/work-orders/<int:order_id>/details/add-json")
@admin_required
def work_order_detail_add_json(order_id):
    order = db.session.get(WorkOrder, order_id)
    if not order:
        return jsonify({"ok": False, "message": "Заказ-наряд не найден."}), 404

    payload = request.get_json(silent=True) or {}
    title = normalize_work_title(payload.get("title", ""))
    unit = (payload.get("unit") or "шт.").strip()
    price_raw = payload.get("price")
    qty_raw = payload.get("quantity")

    if not title:
        return jsonify({"ok": False, "message": "Укажите наименование."}), 400

    try:
        quantity = float(qty_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Количество должно быть числом."}), 400
    if quantity <= 0:
        return jsonify({"ok": False, "message": "Количество должно быть больше нуля."}), 400

    try:
        price = int(price_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Цена должна быть числом."}), 400
    if price < 0:
        return jsonify({"ok": False, "message": "Цена не может быть отрицательной."}), 400

    existing, ek = _find_duplicate_inventory_line(order, title, unit)
    if existing:
        if ek == "detail":
            return jsonify({"ok": True, "already": True, "detail_id": int(existing.id)}), 200
        return jsonify({"ok": True, "already": True, "part_id": int(existing.id)}), 200

    detail = WorkOrderDetail(work_order=order, title=title, quantity=quantity, unit=unit, price=price)
    db.session.add(detail)

    order_total = recalculate_work_order_total(order)

    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "already": False,
            "detail_id": int(detail.id),
            "title": detail.title,
            "quantity": float(detail.quantity or 0),
            "unit": detail.unit or "",
            "price": int(detail.price or 0),
            "total": int((detail.price or 0) * (detail.quantity or 0)),
            "order_total": order_total,
        }
    )


@bp.post("/work-orders/<int:order_id>/details/update-json/<int:detail_id>")
@admin_required
def work_order_detail_update_json(order_id, detail_id):
    detail = db.session.get(WorkOrderDetail, detail_id)
    if not detail or detail.work_order_id != order_id:
        return jsonify({"ok": False, "message": "Деталь не найдена."}), 404

    payload = request.get_json(silent=True) or {}
    title = normalize_work_title(payload.get("title", ""))
    unit = (payload.get("unit") or "шт.").strip()
    price_raw = payload.get("price")
    qty_raw = payload.get("quantity")

    if not title:
        return jsonify({"ok": False, "message": "Укажите наименование."}), 400

    try:
        quantity = float(qty_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Количество должно быть числом."}), 400
    if quantity <= 0:
        return jsonify({"ok": False, "message": "Количество должно быть больше нуля."}), 400

    try:
        price = int(price_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Цена должна быть числом."}), 400
    if price < 0:
        return jsonify({"ok": False, "message": "Цена не может быть отрицательной."}), 400

    detail.title = title
    detail.quantity = quantity
    detail.unit = unit
    detail.price = price

    order_total = recalculate_work_order_total(detail.work_order)

    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "detail_id": int(detail.id),
            "title": detail.title,
            "quantity": float(detail.quantity or 0),
            "unit": detail.unit or "",
            "price": int(detail.price or 0),
            "total": int((detail.price or 0) * (detail.quantity or 0)),
            "order_total": order_total,
        }
    )


@bp.post("/work-orders/<int:order_id>/materials/update-json/<int:material_id>")
@admin_required
def work_order_material_update_json(order_id, material_id):
    material = db.session.get(WorkOrderMaterial, material_id)
    if not material or material.work_order_id != order_id:
        return jsonify({"ok": False, "message": "Материал не найден."}), 404

    payload = request.get_json(silent=True) or {}
    title = normalize_work_title(payload.get("title", ""))
    unit = (payload.get("unit") or "шт.").strip()
    price_raw = payload.get("price")
    qty_raw = payload.get("quantity")

    if not title:
        return jsonify({"ok": False, "message": "Укажите наименование."}), 400

    try:
        quantity = float(qty_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Количество должно быть числом."}), 400
    if quantity <= 0:
        return jsonify({"ok": False, "message": "Количество должно быть больше нуля."}), 400

    try:
        price = int(price_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Цена должна быть числом."}), 400
    if price < 0:
        return jsonify({"ok": False, "message": "Цена не может быть отрицательной."}), 400

    material.title = title
    material.quantity = quantity
    material.unit = unit
    material.price = price

    order_total = recalculate_work_order_total(material.work_order)

    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "material_id": int(material.id),
            "title": material.title,
            "quantity": float(material.quantity or 0),
            "unit": material.unit or "",
            "price": int(material.price or 0),
            "total": int((material.price or 0) * (material.quantity or 0)),
            "order_total": order_total,
        }
    )


@bp.post("/work-orders/<int:order_id>/materials/add")
@admin_required
def work_order_material_add(order_id):
    order = db.session.get(WorkOrder, order_id)
    if not order:
        abort(404)
    
    form = WorkOrderMaterialForm()
    if form.validate_on_submit():
        material = WorkOrderMaterial(
            work_order=order,
            title=form.title.data,
            quantity=form.quantity.data,
            unit=form.unit.data,
            price=form.price.data
        )
        db.session.add(material)
        
        # Автоматический пересчет суммы
        total_material_price = int(material.price * material.quantity)
        if order.total_amount is None:
            order.total_amount = 0
        order.total_amount += total_material_price
        
        db.session.commit()
        flash("Материал добавлен", "success")
    
    return redirect(url_for("admin.work_order_detail", order_id=order.id))


@bp.post("/work-orders/<int:order_id>/parts/update/<int:part_id>")
@admin_required
def work_order_part_update(order_id, part_id):
    part = db.session.get(WorkOrderPart, part_id)
    if part and part.work_order_id == order_id:
        form_keys = {k for k in request.form if k != "csrf_token"}
        if not form_keys.intersection({"title", "quantity", "unit", "price"}):
            flash("Укажите поля для сохранения (форма была пустой).", "warning")
        else:
            title = request.form.get("title", "").strip()
            quantity = request.form.get("quantity", type=float, default=1.0)
            unit = request.form.get("unit", "").strip()
            price = request.form.get("price", type=int, default=0)

            if title:
                part.title = title
            part.quantity = quantity
            part.unit = unit
            part.price = price

            recalculate_work_order_total(part.work_order)

            db.session.commit()
            flash("Запчасть обновлена", "success")

    return redirect(url_for("admin.work_order_detail", order_id=order_id))


@bp.post("/work-orders/<int:order_id>/materials/add-json")
@admin_required
def work_order_material_add_json(order_id):
    order = db.session.get(WorkOrder, order_id)
    if not order:
        return jsonify({"ok": False, "message": "Заказ-наряд не найден."}), 404

    payload = request.get_json(silent=True) or {}
    title = normalize_work_title(payload.get("title", ""))
    unit = (payload.get("unit") or "шт.").strip()
    price_raw = payload.get("price")
    qty_raw = payload.get("quantity")

    if not title:
        return jsonify({"ok": False, "message": "Укажите наименование."}), 400

    try:
        quantity = float(qty_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Количество должно быть числом."}), 400
    if quantity <= 0:
        return jsonify({"ok": False, "message": "Количество должно быть больше нуля."}), 400

    try:
        price = int(price_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Цена должна быть числом."}), 400
    if price < 0:
        return jsonify({"ok": False, "message": "Цена не может быть отрицательной."}), 400

    key = (work_title_key(title), work_title_key(unit))
    existing_material = None
    for material in order.materials:
        material_key = (work_title_key(material.title or ""), work_title_key(material.unit or ""))
        if material_key == key:
            existing_material = material
            break

    if existing_material:
        return jsonify({"ok": True, "already": True, "material_id": int(existing_material.id)}), 200

    material = WorkOrderMaterial(work_order=order, title=title, quantity=quantity, unit=unit, price=price)
    db.session.add(material)

    total_material_price = int(price * quantity)
    if order.total_amount is None:
        order.total_amount = 0
    order.total_amount += total_material_price

    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "already": False,
            "material_id": int(material.id),
            "title": material.title,
            "quantity": float(material.quantity or 0),
            "unit": material.unit or "",
            "price": int(material.price or 0),
            "total": int((material.price or 0) * (material.quantity or 0)),
            "order_total": int(order.total_amount or 0),
        }
    )


@bp.post("/work-orders/<int:order_id>/parts/delete/<int:part_id>")
@admin_required
def work_order_part_delete(order_id, part_id):
    part = db.session.get(WorkOrderPart, part_id)
    if part and part.work_order_id == order_id:
        order = part.work_order
        total_part_price = int(part.price * part.quantity)
        if order.total_amount is not None:
            order.total_amount -= total_part_price
            
        db.session.delete(part)
        db.session.commit()
        flash("Запчасть удалена", "success")
        
    return redirect(url_for("admin.work_order_detail", order_id=order_id))


@bp.post("/work-orders/<int:order_id>/details/delete/<int:detail_id>")
@admin_required
def work_order_detail_delete(order_id, detail_id):
    detail = db.session.get(WorkOrderDetail, detail_id)
    if detail and detail.work_order_id == order_id:
        order = detail.work_order
        total_detail_price = int(detail.price * detail.quantity)
        if order.total_amount is not None:
            order.total_amount -= total_detail_price
            
        db.session.delete(detail)
        db.session.commit()
        flash("Деталь удалена", "success")
        
    return redirect(url_for("admin.work_order_detail", order_id=order_id))


@bp.post("/work-orders/<int:order_id>/materials/delete/<int:material_id>")
@admin_required
def work_order_material_delete(order_id, material_id):
    material = db.session.get(WorkOrderMaterial, material_id)
    if material and material.work_order_id == order_id:
        order = material.work_order
        total_material_price = int(material.price * material.quantity)
        if order.total_amount is not None:
            order.total_amount -= total_material_price
            
        db.session.delete(material)
        db.session.commit()
        flash("Материал удалена", "success")
        
    return redirect(url_for("admin.work_order_detail", order_id=order_id))


@bp.post("/work-orders/<int:order_id>/details/update/<int:detail_id>")
@admin_required
def work_order_detail_update(order_id, detail_id):
    detail = db.session.get(WorkOrderDetail, detail_id)
    if detail and detail.work_order_id == order_id:
        form_keys = {k for k in request.form if k != "csrf_token"}
        if not form_keys.intersection({"title", "quantity", "unit", "price"}):
            flash("Укажите поля для сохранения (форма была пустой).", "warning")
        else:
            title = request.form.get("title", "").strip()
            quantity = request.form.get("quantity", type=float, default=1.0)
            unit = request.form.get("unit", "").strip()
            price = request.form.get("price", type=int, default=0)

            if title:
                detail.title = title
            detail.quantity = quantity
            detail.unit = unit
            detail.price = price

            recalculate_work_order_total(detail.work_order)

            db.session.commit()
            flash("Деталь обновлена", "success")

    return redirect(url_for("admin.work_order_detail", order_id=order_id))


@bp.post("/work-orders/<int:order_id>/materials/update/<int:material_id>")
@admin_required
def work_order_material_update(order_id, material_id):
    material = db.session.get(WorkOrderMaterial, material_id)
    if material and material.work_order_id == order_id:
        form_keys = {k for k in request.form if k != "csrf_token"}
        if not form_keys.intersection({"title", "quantity", "unit", "price"}):
            flash("Укажите поля для сохранения (форма была пустой).", "warning")
        else:
            title = request.form.get("title", "").strip()
            quantity = request.form.get("quantity", type=float, default=1.0)
            unit = request.form.get("unit", "").strip()
            price = request.form.get("price", type=int, default=0)

            if title:
                material.title = title
            material.quantity = quantity
            material.unit = unit
            material.price = price

            recalculate_work_order_total(material.work_order)

            db.session.commit()
            flash("Материал обновлен", "success")

    return redirect(url_for("admin.work_order_detail", order_id=order_id))


@bp.post("/work-orders/<int:order_id>/additional-works/add")
@admin_required
def work_order_additional_work_add(order_id):
    order = db.session.get(WorkOrder, order_id)
    if not order:
        abort(404)
    form = WorkOrderAdditionalWorkForm()
    if form.validate_on_submit():
        row = WorkOrderAdditionalWork(
            work_order=order,
            title=normalize_work_title(form.title.data or ""),
            price=int(form.price.data or 0),
            comment=(form.comment.data or "").strip() or None,
        )
        db.session.add(row)
        recalculate_work_order_total(order)
        db.session.commit()
        flash("Дополнительная работа добавлена", "success")
    return redirect(url_for("admin.work_order_detail", order_id=order.id))


@bp.post("/work-orders/<int:order_id>/additional-works/add-json")
@admin_required
def work_order_additional_work_add_json(order_id):
    order = db.session.get(WorkOrder, order_id)
    if not order:
        return jsonify({"ok": False, "message": "Заказ-наряд не найден."}), 404
    payload = request.get_json(silent=True) or {}
    title = normalize_work_title(payload.get("title", ""))
    if not title:
        return jsonify({"ok": False, "message": "Укажите наименование."}), 400
    try:
        price = int(payload.get("price"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Сумма должна быть числом."}), 400
    if price < 0:
        return jsonify({"ok": False, "message": "Сумма не может быть отрицательной."}), 400
    comment = (payload.get("comment") or "").strip() or None
    row = WorkOrderAdditionalWork(work_order=order, title=title, price=price, comment=comment)
    db.session.add(row)
    order_total = recalculate_work_order_total(order)
    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "additional_work_id": int(row.id),
            "title": row.title,
            "price": int(row.price or 0),
            "comment": row.comment or "",
            "order_total": order_total,
        }
    )


@bp.post("/work-orders/<int:order_id>/additional-works/update-json/<int:row_id>")
@admin_required
def work_order_additional_work_update_json(order_id, row_id):
    row = db.session.get(WorkOrderAdditionalWork, row_id)
    if not row or row.work_order_id != order_id:
        return jsonify({"ok": False, "message": "Строка не найдена."}), 404
    payload = request.get_json(silent=True) or {}
    title = normalize_work_title(payload.get("title", ""))
    if not title:
        return jsonify({"ok": False, "message": "Укажите наименование."}), 400
    try:
        price = int(payload.get("price"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Сумма должна быть числом."}), 400
    if price < 0:
        return jsonify({"ok": False, "message": "Сумма не может быть отрицательной."}), 400
    row.title = title
    row.price = price
    row.comment = (payload.get("comment") or "").strip() or None
    order_total = recalculate_work_order_total(row.work_order)
    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "additional_work_id": int(row.id),
            "title": row.title,
            "price": int(row.price or 0),
            "comment": row.comment or "",
            "order_total": order_total,
        }
    )


@bp.post("/work-orders/<int:order_id>/additional-works/delete/<int:row_id>")
@admin_required
def work_order_additional_work_delete(order_id, row_id):
    row = db.session.get(WorkOrderAdditionalWork, row_id)
    if row and row.work_order_id == order_id:
        order = row.work_order
        db.session.delete(row)
        recalculate_work_order_total(order)
        db.session.commit()
        flash("Строка удалена", "success")
    return redirect(url_for("admin.work_order_detail", order_id=order_id))


@bp.post("/work-orders/<int:order_id>/complaints/done/<int:complaint_id>")
@admin_required
def work_order_complaint_done(order_id, complaint_id):
    complaint = db.session.get(WorkOrderComplaintItem, complaint_id)
    if complaint and complaint.work_order_id == order_id:
        complaint.is_done = True
        complaint.is_refused = False
        complaint.refusal_reason = None
        db.session.commit()
        flash("Жалоба отмечена как выполненная", "success")
    return redirect(url_for("admin.work_order_detail", order_id=order_id))


@bp.post("/work-orders/<int:order_id>/complaints/refuse/<int:complaint_id>")
@admin_required
def work_order_complaint_refuse(order_id, complaint_id):
    complaint = db.session.get(WorkOrderComplaintItem, complaint_id)
    if complaint and complaint.work_order_id == order_id:
        complaint.is_refused = True
        complaint.is_done = False
        # Получаем причину отказа из формы
        refusal_reason = request.form.get("refusal_reason", "").strip()
        complaint.refusal_reason = refusal_reason
        db.session.commit()
        flash("Жалоба отмечена как отклоненная", "success")
    return redirect(url_for("admin.work_order_detail", order_id=order_id))


@bp.post("/work-orders/<int:order_id>/upload")
@admin_required
def work_order_upload(order_id):
    order = db.session.get(WorkOrder, order_id)
    if not order:
        abort(404)
        
    form = DocumentUploadForm()
    if form.validate_on_submit():
        for f in form.files.data:
            filename = secure_filename(f.filename)
            if not filename:
                continue
                
            # Сохраняем файл: documents/<order_id>/<filename>
            order_dir = os.path.join(current_app.config["DOCUMENTS_DIR"], str(order.id))
            os.makedirs(order_dir, exist_ok=True)
            
            file_path = os.path.join(order_dir, filename)
            f.save(file_path)
            
            doc = WorkOrderDocument(
                work_order=order,
                filename=filename,
                mime=f.content_type,
                storage_path=os.path.relpath(file_path, current_app.config["DOCUMENTS_DIR"]).replace(os.path.sep, "/"),
                size_bytes=os.path.getsize(file_path)
            )
            db.session.add(doc)
            
        db.session.commit()
        flash("Документы загружены", "success")

    return redirect(url_for("admin.work_order_detail", order_id=order.id))


@bp.get("/documents/<path:filename>")
@admin_required
def get_document(filename):
    return send_from_directory(current_app.config["DOCUMENTS_DIR"], filename)

@bp.post("/work-orders/<int:order_id>/documents/delete/<int:doc_id>")
@admin_required
def work_order_document_delete(order_id, doc_id):
    doc = db.session.get(WorkOrderDocument, doc_id)
    if doc and doc.work_order_id == order_id:
        file_path = os.path.join(current_app.config["DOCUMENTS_DIR"], str(order_id), doc.filename)
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                current_app.logger.error(f"Error deleting document file: {e}")
        
        db.session.delete(doc)
        db.session.commit()
        flash("Документ удален", "success")
        
    return redirect(url_for("admin.work_order_detail", order_id=order_id))

@bp.post("/work-orders/<int:order_id>/delete")
@admin_required
def work_order_delete(order_id):
    order = db.session.get(WorkOrder, order_id)
    if not order:
        abort(404)
        
    # Удаляем файлы документов
    order_dir = os.path.join(current_app.config["DOCUMENTS_DIR"], str(order.id))
    if os.path.exists(order_dir):
        import shutil
        shutil.rmtree(order_dir)
        
    db.session.delete(order)
    db.session.commit()
    flash("Заказ-наряд удален", "success")
    return redirect(url_for("admin.work_orders"))

@bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    settings_obj = OrganizationSettings.get_settings()
    # Преобразуем строку в список для SelectMultipleField
    if request.method == "GET":
        initial_work_days = settings_obj.work_days.split(",") if settings_obj.work_days else []
        settings_form = OrganizationSettingsForm(obj=settings_obj, work_days=initial_work_days)
    else:
        settings_form = OrganizationSettingsForm()
        
    credentials_form = AdminCredentialsForm(login=current_user.phone)

    # Обработка настроек организации
    if "submit_settings" in request.form and settings_form.validate_on_submit():
        settings_form.populate_obj(settings_obj)
        # Преобразуем список из формы в строку для БД
        if settings_form.work_days.data:
            settings_obj.work_days = ",".join(settings_form.work_days.data)
        else:
            settings_obj.work_days = ""
            
        db.session.commit()
        flash("Настройки организации сохранены", "success")
        return redirect(url_for("admin.settings"))

    if "submit_credentials" in request.form or (credentials_form.submit.data and credentials_form.validate_on_submit()):
        new_phone = normalize_phone(credentials_form.login.data)
        if not new_phone:
            flash("Некорректный формат телефона", "danger")
            return redirect(url_for("admin.settings"))

        if new_phone != current_user.phone:
            exists = db.session.execute(db.select(User).where(User.phone == new_phone)).scalar()
            if exists:
                flash("Этот логин уже занят", "danger")
                return redirect(url_for("admin.settings"))
            current_user.phone = new_phone

        if credentials_form.password.data:
            current_user.set_password(credentials_form.password.data)

        db.session.commit()
        flash("Учетные данные обновлены", "success")
        return redirect(url_for("admin.settings"))

    return render_template("admin/settings.html", settings_form=settings_form, credentials_form=credentials_form)


@bp.route("/contact", methods=["GET", "POST"])
@admin_required
def contact():
    settings_obj = OrganizationSettings.get_settings()
    form = ContactSettingsForm(obj=settings_obj)
    if request.method == "GET":
        form.smtp_password.data = ""
        form.telegram_bot_token.data = ""

    if form.validate_on_submit():
        old_smtp_pw = settings_obj.smtp_password
        old_tg_tok = settings_obj.telegram_bot_token
        form.populate_obj(settings_obj)
        pwd = (form.smtp_password.data or "").strip()
        if not pwd:
            settings_obj.smtp_password = old_smtp_pw
        tg_tok = (form.telegram_bot_token.data or "").strip()
        if not tg_tok:
            settings_obj.telegram_bot_token = old_tg_tok
        settings_obj.org_telegram = normalize_telegram_username(settings_obj.org_telegram) or None
        settings_obj.telegram_bot_username = normalize_telegram_username(settings_obj.telegram_bot_username) or None
        settings_obj.site_public_url = (settings_obj.site_public_url or "").strip().rstrip("/") or None
        db.session.commit()
        flash("Параметры связи сохранены", "success")
        return redirect(url_for("admin.contact"))

    webhook_url = ""
    base = (getattr(settings_obj, "site_public_url", None) or "").strip().rstrip("/")
    if base:
        webhook_url = f"{base}/telegram/webhook"

    return render_template("admin/contact.html", form=form, telegram_webhook_url=webhook_url)


@bp.route("/ai-assistant", methods=["GET", "POST"])
@admin_required
def ai_assistant():
    settings_obj = OrganizationSettings.get_settings()
    form = AiAssistantSettingsForm(obj=settings_obj)
    models = (
        db.session.execute(
            db.select(AiModel).where(AiModel.is_active.is_(True)).order_by(AiModel.title.asc())
        )
        .scalars()
        .all()
    )
    models_json = [
        {
            "id": int(m.id),
            "title": m.title,
            "model_id": m.model_id,
            "context": m.context or "",
            "price_in_per_1m": float(m.price_in_per_1m) if m.price_in_per_1m is not None else None,
            "price_out_per_1m": float(m.price_out_per_1m) if m.price_out_per_1m is not None else None,
            "is_active": bool(m.is_active),
        }
        for m in (models or [])
    ]
    if request.method == "GET":
        form.ai_api_key.data = ""

    if form.validate_on_submit():
        old_key = settings_obj.ai_api_key
        form.populate_obj(settings_obj)
        key = (form.ai_api_key.data or "").strip()
        if not key:
            settings_obj.ai_api_key = old_key

        prov = (settings_obj.ai_provider or "").strip().lower()
        if prov == "openrouter" and not (settings_obj.ai_base_url or "").strip():
            settings_obj.ai_base_url = "https://openrouter.ai/api/v1"
        if prov == "openai" and not (settings_obj.ai_base_url or "").strip():
            settings_obj.ai_base_url = "https://api.openai.com/v1"

        db.session.commit()
        flash("Настройки ИИ-помощника сохранены", "success")
        return redirect(url_for("admin.ai_assistant"))

    return render_template(
        "admin/ai_assistant.html",
        form=form,
        settings=settings_obj,
        ai_models=models_json,
    )


@bp.post("/ai-model/current-json")
@admin_required
def ai_model_current_json():
    settings_obj = OrganizationSettings.get_settings()
    payload = request.get_json(silent=True) or {}
    model_id = str(payload.get("model_id") or "").strip()
    is_custom = bool(payload.get("is_custom", False))
    if not model_id:
        settings_obj.ai_model = ""
        db.session.commit()
        return jsonify({"ok": True, "model": ""})
    if is_custom:
        settings_obj.ai_model = model_id[:120]
        db.session.commit()
        return jsonify({"ok": True, "model": settings_obj.ai_model or ""})
    row = (
        db.session.execute(db.select(AiModel).where(AiModel.model_id == model_id))
        .scalar_one_or_none()
    )
    if not row or not row.is_active:
        return jsonify({"ok": False, "message": "Модель не найдена или отключена."}), 404
    settings_obj.ai_model = row.model_id
    db.session.commit()
    return jsonify({"ok": True, "model": settings_obj.ai_model or ""})


@bp.get("/ai-models")
@admin_required
def ai_models():
    rows = db.session.execute(db.select(AiModel).order_by(AiModel.title.asc())).scalars().all()
    models_json = [
        {
            "id": int(r.id),
            "model": r.model_id,
            "context": r.context or "",
            "price_in_per_1m": float(r.price_in_per_1m) if r.price_in_per_1m is not None else None,
            "price_out_per_1m": float(r.price_out_per_1m) if r.price_out_per_1m is not None else None,
            "is_active": bool(r.is_active),
        }
        for r in rows
    ]
    return render_template("admin/ai_models.html", models=models_json)


@bp.post("/ai-models/save-json")
@admin_required
def ai_models_save_json():
    payload = request.get_json(silent=True) or {}
    rid = payload.get("id")
    model_id = str(payload.get("model") or payload.get("model_id") or "").strip()[:120]
    context = str(payload.get("context") or "").strip()[:10]
    pin = payload.get("price_in_per_1m")
    pout = payload.get("price_out_per_1m")
    is_active = bool(payload.get("is_active", True))
    if not model_id:
        return jsonify({"ok": False, "message": "Заполните Модель."}), 400

    def _float_or_none(x):
        s = str(x or "").strip().replace(",", ".")
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None

    price_in = _float_or_none(pin)
    price_out = _float_or_none(pout)

    if rid and str(rid).isdigit():
        row = db.session.get(AiModel, int(rid))
        if not row:
            return jsonify({"ok": False, "message": "Модель не найдена."}), 404
        row.model_id = model_id
        row.context = context or None
        row.price_in_per_1m = price_in
        row.price_out_per_1m = price_out
        row.is_active = is_active
    else:
        row = AiModel(
            title=model_id,
            model_id=model_id,
            context=context or None,
            price_in_per_1m=price_in,
            price_out_per_1m=price_out,
            is_active=is_active,
        )
        db.session.add(row)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"ok": False, "message": "Не удалось сохранить (возможно, такой model_id уже есть)."}), 400

    rows = db.session.execute(db.select(AiModel).order_by(AiModel.title.asc())).scalars().all()
    return jsonify(
        {
            "ok": True,
            "models": [
                {
                    "id": int(r.id),
                    "model": r.model_id,
                    "context": r.context or "",
                    "price_in_per_1m": float(r.price_in_per_1m) if r.price_in_per_1m is not None else None,
                    "price_out_per_1m": float(r.price_out_per_1m) if r.price_out_per_1m is not None else None,
                    "is_active": bool(r.is_active),
                }
                for r in rows
            ],
        }
    )


@bp.post("/ai-models/delete-json")
@admin_required
def ai_models_delete_json():
    payload = request.get_json(silent=True) or {}
    rid = payload.get("id")
    if not rid or not str(rid).isdigit():
        return jsonify({"ok": False, "message": "Некорректный id."}), 400
    row = db.session.get(AiModel, int(rid))
    if not row:
        return jsonify({"ok": False, "message": "Модель не найдена."}), 404
    db.session.delete(row)
    db.session.commit()
    rows = db.session.execute(db.select(AiModel).order_by(AiModel.title.asc())).scalars().all()
    return jsonify(
        {
            "ok": True,
            "models": [
                {
                    "id": int(r.id),
                    "model": r.model_id,
                    "context": r.context or "",
                    "price_in_per_1m": float(r.price_in_per_1m) if r.price_in_per_1m is not None else None,
                    "price_out_per_1m": float(r.price_out_per_1m) if r.price_out_per_1m is not None else None,
                    "is_active": bool(r.is_active),
                }
                for r in rows
            ],
        }
    )


@bp.get("/ai-prompt-templates")
@admin_required
def ai_prompt_templates():
    rows = db.session.execute(
        db.select(AiPromptTemplate).order_by(AiPromptTemplate.is_active.desc(), AiPromptTemplate.title.asc())
    ).scalars().all()
    return render_template("admin/ai_prompt_templates.html", templates=rows)


@bp.post("/ai-prompt-templates/create")
@admin_required
def ai_prompt_template_create():
    title = (request.form.get("title") or "").strip()[:160]
    body_md = (request.form.get("body_md") or "").strip()
    if not title or not body_md:
        flash("Заполните название и текст шаблона.", "warning")
        return redirect(url_for("admin.ai_prompt_templates"))
    row = AiPromptTemplate(title=title, body_md=body_md, is_active=True)
    db.session.add(row)
    db.session.commit()
    flash("Шаблон сохранён.", "success")
    return redirect(url_for("admin.ai_prompt_templates"))


@bp.post("/ai-prompt-templates/<int:tpl_id>/toggle")
@admin_required
def ai_prompt_template_toggle(tpl_id: int):
    row = db.session.get(AiPromptTemplate, tpl_id)
    if not row:
        abort(404)
    row.is_active = not bool(row.is_active)
    db.session.commit()
    flash("Статус шаблона обновлён.", "success")
    return redirect(url_for("admin.ai_prompt_templates"))


@bp.post("/ai-prompt-templates/<int:tpl_id>/delete")
@admin_required
def ai_prompt_template_delete(tpl_id: int):
    row = db.session.get(AiPromptTemplate, tpl_id)
    if not row:
        abort(404)
    db.session.delete(row)
    db.session.commit()
    flash("Шаблон удалён.", "success")
    return redirect(url_for("admin.ai_prompt_templates"))


@bp.get("/ai-requests")
@admin_required
def ai_requests():
    appt_id = request.args.get("appointment_id", "").strip()
    wo_id = request.args.get("work_order_id", "").strip()
    q = db.select(AiRequestLog).order_by(AiRequestLog.created_at.desc())
    try:
        if appt_id:
            q = q.where(AiRequestLog.appointment_id == int(appt_id))
        if wo_id:
            q = q.where(AiRequestLog.work_order_id == int(wo_id))
    except ValueError:
        pass
    rows = db.session.execute(q.limit(300)).scalars().all()

    def _last_user_snippet(messages_json: str | None) -> str:
        if not messages_json:
            return ""
        try:
            arr = json.loads(messages_json)
        except Exception:
            return ""
        if not isinstance(arr, list):
            return ""
        for m in reversed(arr):
            if not isinstance(m, dict):
                continue
            if str(m.get("role") or "") == "user":
                s = str(m.get("content") or "").strip()
                return (s[:180] + "…") if len(s) > 180 else s
        return ""

    out = []
    for r in rows:
        out.append(
            {
                "id": int(r.id),
                "created_at": r.created_at,
                "appointment_id": int(r.appointment_id) if r.appointment_id else None,
                "work_order_id": int(r.work_order_id) if r.work_order_id else None,
                "template_id": int(r.template_id) if r.template_id else None,
                "model": r.model or "",
                "question": _last_user_snippet(r.messages_json),
            }
        )
    return render_template("admin/ai_requests.html", rows=out)


@bp.get("/ai-requests-json")
@admin_required
def ai_requests_json():
    appt_id = (request.args.get("appointment_id") or "").strip()
    wo_id = (request.args.get("work_order_id") or "").strip()
    q = db.select(AiRequestLog).order_by(AiRequestLog.created_at.desc())
    try:
        if appt_id:
            q = q.where(AiRequestLog.appointment_id == int(appt_id))
        if wo_id:
            q = q.where(AiRequestLog.work_order_id == int(wo_id))
    except ValueError:
        return jsonify({"ok": False, "message": "Некорректный id."}), 400

    rows = db.session.execute(q.limit(200)).scalars().all()

    out = []
    for r in rows:
        tpl_title = ""
        if r.template_id:
            t = db.session.get(AiPromptTemplate, int(r.template_id))
            tpl_title = (t.title or "") if t else ""
        out.append(
            {
                "id": int(r.id),
                "created_at": r.created_at.isoformat() if r.created_at else "",
                "model": r.model or "",
                "template_id": int(r.template_id) if r.template_id else None,
                "template_title": tpl_title,
                "prompt_md": r.prompt_md or "",
                "answer_text": r.answer_text or "",
            }
        )
    return jsonify({"ok": True, "rows": out})


@bp.get("/ai-request/<int:req_id>/json")
@admin_required
def ai_request_detail_json(req_id: int):
    r = db.session.get(AiRequestLog, req_id)
    if not r:
        return jsonify({"ok": False, "message": "Запрос не найден."}), 404
    tpl_title = ""
    if r.template_id:
        t = db.session.get(AiPromptTemplate, int(r.template_id))
        tpl_title = (t.title or "") if t else ""
    return jsonify(
        {
            "ok": True,
            "id": int(r.id),
            "created_at": r.created_at.isoformat() if r.created_at else "",
            "model": r.model or "",
            "template_id": int(r.template_id) if r.template_id else None,
            "template_title": tpl_title,
            "prompt_md": r.prompt_md or "",
            "answer_text": r.answer_text or "",
        }
    )


@bp.get("/ai-prompt-templates/<int:tpl_id>")
@admin_required
def ai_prompt_template_edit(tpl_id: int):
    row = db.session.get(AiPromptTemplate, tpl_id)
    if not row:
        abort(404)
    return render_template("admin/ai_prompt_template_edit.html", tpl=row)


@bp.post("/ai-prompt-templates/<int:tpl_id>/update")
@admin_required
def ai_prompt_template_update(tpl_id: int):
    row = db.session.get(AiPromptTemplate, tpl_id)
    if not row:
        abort(404)
    title = (request.form.get("title") or "").strip()[:160]
    body_md = (request.form.get("body_md") or "").strip()
    is_active = bool(request.form.get("is_active"))
    if not title or not body_md:
        flash("Заполните название и текст шаблона.", "warning")
        return redirect(url_for("admin.ai_prompt_template_edit", tpl_id=tpl_id))
    row.title = title
    row.body_md = body_md
    row.is_active = is_active
    db.session.commit()
    flash("Шаблон обновлён.", "success")
    return redirect(url_for("admin.ai_prompt_template_edit", tpl_id=tpl_id))


@bp.post("/contact/test-email")
@admin_required
def contact_test_email():
    settings_obj = OrganizationSettings.get_settings()
    to = (settings_obj.email or "").strip()
    if not to:
        flash("Укажите «Email для связи», сохраните настройки, затем повторите тест.", "danger")
        return redirect(url_for("admin.contact"))
    if not (settings_obj.smtp_host or "").strip():
        flash("Укажите SMTP сервер и сохраните настройки.", "danger")
        return redirect(url_for("admin.contact"))
    try:
        send_organization_email(
            [to],
            f"Тест SMTP — {settings_obj.name or 'Сервис'}",
            "Это тестовое письмо из раздела «Связь». Если вы его получили, SMTP настроен верно.",
            settings=settings_obj,
        )
        flash(f"Тестовое письмо отправлено на {to}", "success")
    except MailConfigurationError as e:
        flash(str(e), "danger")
    except smtplib.SMTPException as e:
        flash(f"Ошибка SMTP: {e}", "danger")
    except OSError as e:
        flash(f"Сеть / соединение: {e}", "danger")
    except Exception as e:
        flash(f"Не удалось отправить: {e}", "danger")
    return redirect(url_for("admin.contact"))


@bp.get("/materials-report")
@admin_required
def materials_report():
    """Ведомость использованных материалов"""
    start_date_str = request.args.get("start_date")
    end_date_str = request.args.get("end_date")
    
    # По умолчанию за текущий месяц
    today = datetime.now()
    if not start_date_str:
        start_date = datetime(today.year, today.month, 1)
    else:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
        
    if not end_date_str:
        end_date = today.replace(hour=23, minute=59, second=59)
    else:
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    
    materials_groups, total_cost = materials_report_groups_for_period(start_date, end_date)

    return render_template(
        "admin/materials_report.html",
        materials_groups=materials_groups,
        total_cost=total_cost,
        start_date=start_date.strftime("%Y-%m-%d"),
        end_date=end_date.strftime("%Y-%m-%d")
    )

@bp.get("/payouts")
@admin_required
def payouts():
    start_date_str = request.args.get("start_date")
    end_date_str = request.args.get("end_date")
    
    # По умолчанию за текущий месяц
    today = datetime.now()
    if not start_date_str:
        start_date = datetime(today.year, today.month, 1)
    else:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
        
    if not end_date_str:
        end_date = today.replace(hour=23, minute=59, second=59)
    else:
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    
    # Получаем всех мастеров
    masters = db.session.execute(db.select(Master).order_by(Master.name)).scalars().all()
    
    payouts_data = []
    
    for master in masters:
        # Ищем работы, выполненные этим мастером в указанный период
        # Либо это основной мастер заказа, либо исполнитель конкретной работы
        
        # 1. Работы, где он явно указан как исполнитель
        direct_items = db.session.execute(
            db.select(WorkOrderItem)
            .join(WorkOrder)
            .where(WorkOrderItem.master_id == master.id)
            .where(WorkOrderItem.is_done == True)
            .where(WorkOrder.status.in_(("opened", "closed")))
            .where(WorkOrder.created_at >= start_date)
            .where(WorkOrder.created_at <= end_date)
        ).scalars().all()
        
        # 2. Работы, где мастер не указан, но он основной мастер заказа
        order_items = db.session.execute(
            db.select(WorkOrderItem)
            .join(WorkOrder)
            .where(WorkOrder.master_id == master.id)
            .where(WorkOrderItem.master_id == None)
            .where(WorkOrderItem.is_done == True)
            .where(WorkOrder.status.in_(("opened", "closed")))
            .where(WorkOrder.created_at >= start_date)
            .where(WorkOrder.created_at <= end_date)
        ).scalars().all()
        
        all_items = list(direct_items) + list(order_items)
        
        # Преобразуем SQLAlchemy объекты в простые словари
        serializable_items = []
        for item in all_items:
            serializable_items.append({
                "id": item.id,
                "work_order_id": item.work_order_id,
                "title": item.title,
                "price": item.price,
                "is_paid": item.is_paid,
                "created_at": item.created_at
            })
        
        if serializable_items:
            # Расчет выплаты: только за неоплаченные работы
            unpaid_work_amount = sum(item["price"] for item in serializable_items if not item["is_paid"])
            total_work_amount = sum(item["price"] for item in serializable_items)
            
            master_payout = int(unpaid_work_amount * (master.payout_percent or 100) / 100)
            
            payouts_data.append({
                "master": master,
                "work_items": serializable_items,
                "total_work_amount": total_work_amount,
                "unpaid_work_amount": unpaid_work_amount,
                "total_payout": master_payout
            })
            
    return render_template(
        "admin/payouts.html", 
        payouts=payouts_data,
        start_date=start_date.strftime("%Y-%m-%d"),
        end_date=end_date.strftime("%Y-%m-%d")
    )

@bp.get("/payouts/print")
@admin_required
def payouts_print():
    start_date_str = request.args.get("start_date")
    end_date_str = request.args.get("end_date")
    today = datetime.now()
    if not start_date_str:
        start_date = datetime(today.year, today.month, 1)
    else:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
    if not end_date_str:
        end_date = today.replace(hour=23, minute=59, second=59)
    else:
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    masters = db.session.execute(db.select(Master).order_by(Master.name)).scalars().all()
    payouts_data = []
    for master in masters:
        direct_items = db.session.execute(
            db.select(WorkOrderItem)
            .join(WorkOrder)
            .where(WorkOrderItem.master_id == master.id)
            .where(WorkOrderItem.is_done == True)
            .where(WorkOrder.status.in_(("opened", "closed")))
            .where(WorkOrder.created_at >= start_date)
            .where(WorkOrder.created_at <= end_date)
        ).scalars().all()
        order_items = db.session.execute(
            db.select(WorkOrderItem)
            .join(WorkOrder)
            .where(WorkOrder.master_id == master.id)
            .where(WorkOrderItem.master_id == None)
            .where(WorkOrderItem.is_done == True)
            .where(WorkOrder.status.in_(("opened", "closed")))
            .where(WorkOrder.created_at >= start_date)
            .where(WorkOrder.created_at <= end_date)
        ).scalars().all()
        all_items = list(direct_items) + list(order_items)
        serializable_items = []
        for item in all_items:
            serializable_items.append({
                "id": item.id,
                "work_order_id": item.work_order_id,
                "title": item.title,
                "price": item.price,
                "is_paid": item.is_paid,
                "created_at": item.created_at
            })
        if serializable_items:
            unpaid_work_amount = sum(item["price"] for item in serializable_items if not item["is_paid"])
            total_work_amount = sum(item["price"] for item in serializable_items)
            master_payout = int(unpaid_work_amount * (master.payout_percent or 100) / 100)
            payouts_data.append({
                "master": master,
                "work_items": serializable_items,
                "total_work_amount": total_work_amount,
                "unpaid_work_amount": unpaid_work_amount,
                "total_payout": master_payout
            })
    settings_obj = OrganizationSettings.get_settings()
    return render_template(
        "admin/payouts_print.html",
        payouts=payouts_data,
        settings=settings_obj,
        start_date=start_date.strftime("%Y-%m-%d"),
        end_date=end_date.strftime("%Y-%m-%d"),
        printed_at=datetime.now()
    )

@bp.post("/payouts/pay/<int:master_id>")
@admin_required
def payouts_pay(master_id):
    master = db.session.get(Master, master_id)
    if not master:
        abort(404)
        
    start_date_str = request.args.get("start_date")
    end_date_str = request.args.get("end_date")
    
    if not start_date_str or not end_date_str:
        flash("Не указан период для выплаты", "danger")
        return redirect(url_for("admin.payouts"))
        
    start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
    end_date = datetime.strptime(end_date_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    
    # Ищем неоплаченные работы мастера за период
    unpaid_items = db.session.execute(
        db.select(WorkOrderItem)
        .join(WorkOrder)
        .where(
            db.or_(
                WorkOrderItem.master_id == master.id,
                db.and_(WorkOrder.master_id == master.id, WorkOrderItem.master_id == None)
            )
        )
        .where(WorkOrderItem.is_done == True)
        .where(WorkOrderItem.is_paid == False)
        .where(WorkOrder.status.in_(("opened", "closed")))
        .where(WorkOrder.created_at >= start_date)
        .where(WorkOrder.created_at <= end_date)
    ).scalars().all()
    
    if not unpaid_items:
        flash("Нет неоплаченных работ за этот период", "warning")
        return redirect(url_for("admin.payouts", start_date=start_date_str, end_date=end_date_str))
        
    total_work_amount = sum(item.price for item in unpaid_items)
    payout_amount = int(total_work_amount * (master.payout_percent or 100) / 100)
    
    # Отмечаем как оплаченные
    for item in unpaid_items:
        item.is_paid = True
        
    # Записываем расход в книгу
    cash = CashFlow(
        amount=-payout_amount, # Отрицательное значение для расхода
        category="Выплата мастеру",
        description=f"Выплата мастеру {master.name} за период {start_date_str} - {end_date_str}",
        master_id=master.id
    )
    db.session.add(cash)
    db.session.commit()
    
    flash(f"Выплата мастеру {master.name} в размере {payout_amount} руб. отмечена", "success")
    return redirect(url_for("admin.payouts", start_date=start_date_str, end_date=end_date_str))

@bp.get("/cash-flow")
@admin_required
def cash_flow():
    start_date_str = request.args.get("start_date")
    end_date_str = request.args.get("end_date")
    
    today = datetime.now()
    if not start_date_str:
        start_date = datetime(today.year, today.month, 1)
    else:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
        
    if not end_date_str:
        end_date = today
    else:
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
        
    end_date_full = end_date.replace(hour=23, minute=59, second=59)
        
    query = db.select(CashFlow).where(
        CashFlow.date >= start_date,
        CashFlow.date <= end_date_full
    ).order_by(CashFlow.date.desc())
    
    entries = db.session.execute(query).scalars().all()
    
    total_income = sum(e.amount for e in entries if e.amount > 0)
    total_expense = abs(sum(e.amount for e in entries if e.amount < 0))
    balance = total_income - total_expense
    
    return render_template(
        "admin/cash_flow.html",
        entries=entries,
        total_income=total_income,
        total_expense=total_expense,
        balance=balance,
        start_date=start_date.strftime("%Y-%m-%d"),
        end_date=end_date.strftime("%Y-%m-%d")
    )

@bp.get("/cash-flow/print")
@admin_required
def cash_flow_print():
    start_date_str = request.args.get("start_date")
    end_date_str = request.args.get("end_date")
    today = datetime.now()
    if not start_date_str:
        start_date = datetime(today.year, today.month, 1)
    else:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
    if not end_date_str:
        end_date = today
    else:
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
    end_date_full = end_date.replace(hour=23, minute=59, second=59)
    query = db.select(CashFlow).where(
        CashFlow.date >= start_date,
        CashFlow.date <= end_date_full
    ).order_by(CashFlow.date.desc())
    entries = db.session.execute(query).scalars().all()
    total_income = sum(e.amount for e in entries if e.amount > 0)
    total_expense = abs(sum(e.amount for e in entries if e.amount < 0))
    balance = total_income - total_expense
    settings_obj = OrganizationSettings.get_settings()
    return render_template(
        "admin/cash_flow_print.html",
        entries=entries,
        total_income=total_income,
        total_expense=total_expense,
        balance=balance,
        settings=settings_obj,
        start_date=start_date.strftime("%Y-%m-%d"),
        end_date=end_date.strftime("%Y-%m-%d"),
        printed_at=datetime.now()
    )

@bp.get("/materials-report/print")
@admin_required
def materials_report_print():
    start_date_str = request.args.get("start_date")
    end_date_str = request.args.get("end_date")
    today = datetime.now()
    if not start_date_str:
        start_date = datetime(today.year, today.month, 1)
    else:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
    if not end_date_str:
        end_date = today.replace(hour=23, minute=59, second=59)
    else:
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    materials_groups, total_cost = materials_report_groups_for_period(start_date, end_date)
    settings_obj = OrganizationSettings.get_settings()
    return render_template(
        "admin/materials_report_print.html",
        materials_groups=materials_groups,
        total_cost=total_cost,
        settings=settings_obj,
        start_date=start_date.strftime("%Y-%m-%d"),
        end_date=end_date.strftime("%Y-%m-%d"),
        printed_at=datetime.now()
    )

# --- Баннеры ---

@bp.get("/banners")
@admin_required
def banners():
    all_banners = db.session.execute(db.select(Banner).order_by(Banner.order)).scalars().all()
    return render_template("admin/banners/index.html", banners=all_banners)

@bp.route("/banners/add", methods=["GET", "POST"])
@bp.route("/banners/edit/<int:banner_id>", methods=["GET", "POST"])
@admin_required
def banner_edit(banner_id=None):
    banner = db.session.get(Banner, banner_id) if banner_id else Banner()
    form = BannerForm(obj=banner)
    
    if form.validate_on_submit():
        # Если это новый баннер, изображение обязательно
        if not banner.id and not form.image.data:
            flash("Для нового баннера необходимо загрузить изображение", "danger")
            return render_template("admin/banners/edit.html", form=form, banner=banner)
            
        form.populate_obj(banner)
        
        # Обработка загрузки изображения
        if form.image.data:
            # Удаляем старый файл если он есть и это не внешняя ссылка
            if banner.image_path and not banner.image_path.startswith('http'):
                old_file_path = os.path.join(current_app.static_folder, banner.image_path)
                if os.path.exists(old_file_path):
                    try:
                        os.remove(old_file_path)
                    except Exception as e:
                        current_app.logger.error(f"Error deleting old banner image: {e}")
                        
            f = form.image.data
            filename = secure_filename(f.filename)
            if filename:
                # Генерируем уникальное имя файла
                ext = os.path.splitext(filename)[1]
                new_filename = f"banner_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}{ext}"
                
                upload_dir = os.path.join(current_app.static_folder, "uploads", "banners")
                os.makedirs(upload_dir, exist_ok=True)
                
                file_path = os.path.join(upload_dir, new_filename)
                f.save(file_path)
                
                # Сохраняем относительный путь для БД
                banner.image_path = f"uploads/banners/{new_filename}"
        
        if not banner.id:
            db.session.add(banner)
        
        db.session.commit()
        flash("Баннер сохранен", "success")
        return redirect(url_for("admin.banners"))
        
    return render_template("admin/banners/edit.html", form=form, banner=banner)

@bp.post("/banners/delete/<int:banner_id>")
@admin_required
def banner_delete(banner_id):
    banner = db.session.get(Banner, banner_id)
    if banner:
        # Удаляем файл изображения если это не внешняя ссылка
        if banner.image_path and not banner.image_path.startswith('http'):
            file_path = os.path.join(current_app.static_folder, banner.image_path)
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception as e:
                    current_app.logger.error(f"Error deleting banner image on delete: {e}")
        
        db.session.delete(banner)
        db.session.commit()
        flash("Баннер удален", "success")
    return redirect(url_for("admin.banners"))

# --- Отзывы ---

@bp.get("/reviews")
@admin_required
def reviews():
    all_reviews = db.session.execute(db.select(Review).order_by(Review.created_at.desc())).scalars().all()
    return render_template("admin/reviews/index.html", reviews=all_reviews)

@bp.route("/reviews/add", methods=["GET", "POST"])
@bp.route("/reviews/edit/<int:review_id>", methods=["GET", "POST"])
@admin_required
def review_edit(review_id=None):
    review = db.session.get(Review, review_id) if review_id else Review()
    form = ReviewForm(obj=review)
    
    if form.validate_on_submit():
        form.populate_obj(review)
        if not review.id:
            db.session.add(review)
        db.session.commit()
        flash("Отзыв сохранен", "success")
        return redirect(url_for("admin.reviews"))
        
    return render_template("admin/reviews/edit.html", form=form, review=review)

@bp.post("/reviews/delete/<int:review_id>")
@admin_required
def review_delete(review_id):
    review = db.session.get(Review, review_id)
    if review:
        db.session.delete(review)
        db.session.commit()
        flash("Отзыв удален", "success")
    return redirect(url_for("admin.reviews"))

@bp.post("/reviews/toggle-publish/<int:review_id>")
@admin_required
def review_toggle_publish(review_id):
    review = db.session.get(Review, review_id)
    if review:
        review.is_published = not review.is_published
        db.session.commit()
        flash("Статус публикации изменен", "info")
    return redirect(url_for("admin.reviews"))

@bp.post("/settings/cleanup-data")
@admin_required
def cleanup_data():
    """Очистка всех финансовых данных: заказы, ведомости, книга приходов-расходов"""
    try:
        # Удаляем все записи денежного потока
        db.session.execute(db.delete(CashFlow))
        
        # Сбрасываем статусы оплаты в заказах и работах
        db.session.execute(db.update(WorkOrder).values(is_paid=False))
        db.session.execute(db.update(WorkOrderItem).values(is_paid=False))
        
        db.session.commit()
        
        flash("Все финансовые данные успешно очищены. Статусы оплаты сброшены.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Ошибка при очистке данных: {str(e)}", "danger")
    
    return redirect(url_for("admin.settings"))
