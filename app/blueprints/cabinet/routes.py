from __future__ import annotations

from datetime import datetime, timedelta
from secrets import token_urlsafe
import os
import smtplib
import uuid
from flask import Blueprint, Response, current_app, flash, redirect, render_template, session, url_for, send_from_directory, abort, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename

from ...extensions import db
from ...mail import MailConfigurationError, send_organization_email
from ...models import (
    Appointment,
    AppointmentIssueMedia,
    AppointmentItem,
    AppointmentSlot,
    OrganizationSettings,
    TelegramLinkToken,
    Work,
    WorkOrder,
    WorkOrderDocument,
    TelegramLink,
)
from ...telegram_bot import (
    build_zakaz_delivery_message,
    get_telegram_bot_token,
    get_telegram_bot_username,
    issue_work_order_telegram_code,
    telegram_bot_send_message,
)
from ...utils import (
    delete_appointment_issue_media_file,
    issue_media_fingerprint,
    merged_work_order_inventory_rows,
    normalize_win_number,
    problem_description_hash,
    recalculate_work_order_total,
    work_order_has_positive_cashflow,
    work_order_share_text,
    work_order_whatsapp_share_href,
)

bp = Blueprint("cabinet", __name__)

_CABINET_APPT_BADGE: dict[str, tuple[str, str]] = {
    "new": ("Новая", "primary"),
    "negotiation": ("Согласование", "primary"),
    "confirmed": ("Подтверждена", "success"),
    "in_progress": ("В работе", "warning"),
    "done": ("Выполнена", "info"),
    "cancelled_by_admin": ("Отменена администратором", "danger"),
    "cancelled_by_client": ("Отменена клиентом", "secondary"),
}


def _cabinet_appt_badge(status: str) -> tuple[str, str]:
    return _CABINET_APPT_BADGE.get(status, (status, "secondary"))


def _cabinet_client_can_edit_appointment(appointment: Appointment) -> bool:
    """Клиент может править заявку (описание, работы, WIN и т.д.) в этих статусах."""
    return appointment.status in ("new", "negotiation")


def _cabinet_visit_fields(appointment: Appointment) -> tuple[str, str]:
    """Ключ для сравнения при опросе и подпись для отображения времени приёма."""
    return appointment.visit_fingerprint(), appointment.visit_display_label()


_ISSUE_MEDIA_MIMES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "image/heic",
        "image/heif",
        "video/mp4",
        "video/quicktime",
        "video/webm",
    }
)
_ISSUE_MEDIA_EXT = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".mp4", ".mov", ".webm"})
_MAX_ISSUE_MEDIA_BYTES = 20 * 1024 * 1024


def _issue_mime_ok(mime: str | None) -> bool:
    return (mime or "").lower() in _ISSUE_MEDIA_MIMES


def _issue_media_nested_json(appointment: Appointment) -> tuple[list[list[dict]], list[int]]:
    """Списки вложений по слотам (как у problem_items) + все id для отпечатка."""
    items = appointment.problem_items()
    n = len(items)
    rows = db.session.execute(
        select(AppointmentIssueMedia)
        .where(AppointmentIssueMedia.appointment_id == appointment.id)
        .order_by(
            AppointmentIssueMedia.issue_slot.asc(),
            AppointmentIssueMedia.sort_order.asc(),
            AppointmentIssueMedia.id.asc(),
        )
    ).scalars().all()
    slots: list[list[dict]] = [[] for _ in range(n)]
    all_ids: list[int] = []
    for m in rows:
        all_ids.append(m.id)
        if 0 <= m.issue_slot < n:
            slots[m.issue_slot].append(
                {
                    "id": m.id,
                    "mime": m.mime,
                    "url": url_for(
                        "cabinet.appointment_issue_media_file",
                        appointment_id=appointment.id,
                        media_id=m.id,
                    ),
                }
            )
    return slots, all_ids


def _apply_issue_media_layout(appointment: Appointment, media_by_slot: list, num_slots: int) -> None:
    """Перепривязка id к слотам; всё лишнее удаляется вместе с файлами."""
    if len(media_by_slot) != num_slots:
        raise ValueError("media_by_slot")

    keep_ids: set[int] = set()
    planned: list[tuple[int, int, int]] = []  # media_id, slot, order

    for slot, row in enumerate(media_by_slot):
        if not isinstance(row, list):
            raise ValueError("slot")
        for order, mid in enumerate(row):
            if not isinstance(mid, int) or mid <= 0:
                raise ValueError("id")
            keep_ids.add(mid)
            planned.append((mid, slot, order))

    existing = db.session.execute(
        select(AppointmentIssueMedia).where(AppointmentIssueMedia.appointment_id == appointment.id)
    ).scalars().all()
    by_id = {m.id: m for m in existing}

    for mid in keep_ids:
        if mid not in by_id:
            raise ValueError("unknown_id")

    for m in existing:
        if m.id not in keep_ids:
            delete_appointment_issue_media_file(m)
            db.session.delete(m)

    for mid, slot, order in planned:
        m = by_id[mid]
        m.issue_slot = slot
        m.sort_order = order


def _cabinet_list_time_label(appointment: Appointment) -> str:
    """Время в списке заявок (кратко при нескольких днях)."""
    full = appointment.visit_display_label()
    if not full:
        return ""
    if ";" in full:
        return full.split(";")[0].strip() + " …"
    return full


@bp.after_request
def _cabinet_disable_caching(response: Response) -> Response:
    """Личный кабинет не должен кэшироваться браузером — иначе устаревают статусы заявок."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@bp.get("/")
@login_required
def index():
    # Показываем заказы и заявки
    appointments = db.session.execute(
        db.select(Appointment)
        .where(Appointment.client_user_id == current_user.id)
        .order_by(Appointment.start_at.desc())
        .options(selectinload(Appointment.slots).selectinload(AppointmentSlot.slot))
    ).scalars().all()
    
    orders = db.session.execute(
        db.select(WorkOrder)
        .where(WorkOrder.client_user_id == current_user.id)
        .order_by(WorkOrder.id.desc())
    ).scalars().all()

    # Поддерживаем актуальную сумму (без ручного сохранения в админке)
    for o in orders:
        recalculate_work_order_total(o)
    db.session.commit()

    # Оплата: приход в книге (CashFlow) или флаг заказ-наряда (на случай расхождений/наследия данных).
    paid_ids: set[int] = set()
    for o in orders:
        if work_order_has_positive_cashflow(o.id) or bool(o.is_paid):
            paid_ids.add(int(o.id))
    
    return render_template(
        "cabinet/index.html",
        appointments=appointments,
        orders=orders,
        paid_order_ids=paid_ids,
    )


@bp.get("/work-orders/<int:order_id>/print")
@login_required
def work_order_print(order_id: int):
    """Печать заказ-наряда для клиента (только свои заказы)."""
    order = db.session.get(WorkOrder, order_id)
    if not order or order.client_user_id != current_user.id:
        abort(404)

    recalculate_work_order_total(order)
    db.session.commit()

    merged_inventory_rows = merged_work_order_inventory_rows(order)
    selected_materials_list = list(order.materials or [])
    settings = OrganizationSettings.get_settings()
    if order.appointment_id:
        print_back_url = url_for("cabinet.appointment_detail", appointment_id=order.appointment_id)
    else:
        print_back_url = url_for("cabinet.index")
    org = (settings.name or "").strip()
    org_disp = org or "Сервис"
    print_abs = url_for("cabinet.work_order_print", order_id=order.id, _external=True)
    client_email = (current_user.client_email or "").strip()
    telegram_delivery = session.pop("telegram_print_delivery", None)
    if not telegram_delivery or int(telegram_delivery.get("order_id", 0)) != int(order.id):
        telegram_delivery = None
    return render_template(
        "admin/work_orders/print.html",
        order=order,
        settings=settings,
        merged_inventory_rows=merged_inventory_rows,
        selected_materials_list=selected_materials_list,
        print_back_url=print_back_url,
        print_back_label="← К заявке",
        whatsapp_share_href=work_order_whatsapp_share_href(order, org_disp, print_abs),
        telegram_delivery=telegram_delivery,
        telegram_code_post_url=url_for("cabinet.work_order_print_telegram_code", order_id=order.id),
        telegram_bot_name=get_telegram_bot_username(),
        send_email_url=url_for("cabinet.work_order_print_send_email", order_id=order.id),
        send_email_available=bool(client_email),
        send_email_hint="Укажите email в профиле (данные при регистрации или в кабинете)",
    )


@bp.get("/work-orders/<int:order_id>/sbp-qr.png")
@login_required
def work_order_sbp_qr(order_id: int):
    """QR для оплаты по СБП (клиентская версия): доступен только владельцу заказа."""
    from io import BytesIO

    import qrcode
    from flask import send_file

    order = db.session.get(WorkOrder, order_id)
    if not order or order.client_user_id != current_user.id:
        abort(404)

    settings_obj = OrganizationSettings.get_settings()
    phone = (getattr(settings_obj, "sbp_phone", None) or "").strip()
    if not phone:
        abort(404)

    recalculate_work_order_total(order)
    amount = int(order.total_amount or 0)

    payload = f"SBP|PHONE={phone}|AMOUNT={amount}|ORDER={order.id}"

    qr = qrcode.QRCode(border=2, box_size=8)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", download_name=f"work-order-{order.id}-sbp.png", max_age=0)


@bp.post("/work-orders/<int:order_id>/print/telegram-code")
@login_required
def work_order_print_telegram_code(order_id: int):
    order = db.session.get(WorkOrder, order_id)
    if not order or order.client_user_id != current_user.id:
        abort(404)
    code = issue_work_order_telegram_code(order.id)
    bot = get_telegram_bot_username()
    msg = build_zakaz_delivery_message(order=order, code=code, bot_username=bot)
    link = db.session.execute(
        db.select(TelegramLink).where(
            TelegramLink.user_id == current_user.id,
            TelegramLink.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if link and get_telegram_bot_token():
        try:
            telegram_bot_send_message(link.telegram_chat_id, msg)
            flash("Сообщение с адресом бота и кодом отправлено вам в Telegram.", "success")
        except Exception as e:
            flash(f"Не удалось отправить в Telegram: {e}", "warning")
    elif not link:
        flash("Сначала привяжите Telegram в кабинете — затем снова нажмите кнопку.", "warning")
    else:
        flash("Токен бота не задан в настройках сервиса («Связь» в админке) — сообщение не отправлено.", "warning")

    session["telegram_print_delivery"] = {
        "order_id": order.id,
        "code": code,
        "bot": bot,
        "message": msg,
    }
    return redirect(url_for("cabinet.work_order_print", order_id=order.id))


@bp.post("/work-orders/<int:order_id>/print/send-email")
@login_required
def work_order_print_send_email(order_id: int):
    order = db.session.get(WorkOrder, order_id)
    if not order or order.client_user_id != current_user.id:
        abort(404)
    to = (current_user.client_email or "").strip()
    if not to:
        flash("Укажите email в профиле, чтобы получать заказ-наряды на почту.", "warning")
        return redirect(url_for("cabinet.work_order_print", order_id=order_id))
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
    return redirect(url_for("cabinet.work_order_print", order_id=order_id))


@bp.get("/appointments/<int:appointment_id>")
@login_required
def appointment_detail(appointment_id: int):
    appointment = db.session.execute(
        db.select(Appointment)
        .where(Appointment.id == appointment_id, Appointment.client_user_id == current_user.id)
        .options(
            selectinload(Appointment.slots).selectinload(AppointmentSlot.slot),
            selectinload(Appointment.work_order),
        )
    ).scalar_one_or_none()
    if not appointment:
        abort(404)
    wo = appointment.work_order
    work_order_paid_in_book = False
    if wo is not None:
        work_order_paid_in_book = work_order_has_positive_cashflow(wo.id) or bool(wo.is_paid)
        recalculate_work_order_total(wo)
        db.session.commit()
    nested, media_ids = _issue_media_nested_json(appointment)
    im_hash = issue_media_fingerprint(media_ids)
    return render_template(
        "cabinet/appointment_detail.html",
        appointment=appointment,
        problem_description_hash=problem_description_hash(appointment.problem_description),
        issue_media_initial=nested,
        issue_media_hash=im_hash,
        visit_key=appointment.visit_fingerprint(),
        work_order_paid_in_book=work_order_paid_in_book,
    )


@bp.get("/appointments-status-json")
@login_required
def appointments_status_json():
    rows = db.session.execute(
        db.select(Appointment)
        .where(Appointment.client_user_id == current_user.id)
        .options(selectinload(Appointment.slots).selectinload(AppointmentSlot.slot))
    ).scalars().all()
    out = []
    for appt in rows:
        label, badge = _cabinet_appt_badge(appt.status)
        out.append(
            {
                "id": int(appt.id),
                "status": appt.status,
                "label": label,
                "badge_class": badge,
                "list_time_label": _cabinet_list_time_label(appt),
                "visit_lines": appt.visit_display_lines(),
            }
        )
    return jsonify({"ok": True, "appointments": out})


@bp.get("/appointments/<int:appointment_id>/snapshot-json")
@login_required
def appointment_snapshot_json(appointment_id: int):
    appointment = db.session.execute(
        db.select(Appointment)
        .where(Appointment.id == appointment_id, Appointment.client_user_id == current_user.id)
        .options(
            selectinload(Appointment.items).selectinload(AppointmentItem.work),
            selectinload(Appointment.slots).selectinload(AppointmentSlot.slot),
        )
    ).scalar_one_or_none()
    if not appointment:
        return jsonify({"ok": False, "message": "Заявка не найдена."}), 404

    status_label, badge_class = _cabinet_appt_badge(appointment.status)
    is_editable = _cabinet_client_can_edit_appointment(appointment)

    items_out: list[dict] = []
    total_price = 0
    total_duration = 0
    for item in appointment.items:
        w = item.work
        base_dur = int(item.duration_snapshot or (w.duration_min if w else 0) or 0)
        base_price = int(item.price_snapshot or 0)
        qty = int(item.qty or 1)
        tdur = int(round(base_dur * float(item.k1 or 1.0) * qty))
        tprice = int(round(base_price * float(item.k2 or 1.0) * qty))
        declined = bool(item.declined_by_client)
        if not declined:
            total_duration += tdur
            total_price += tprice
        row_h, row_m = tdur // 60, tdur % 60
        items_out.append(
            {
                "id": int(item.id),
                "title": (w.title if w else "") or "",
                "extra": item.extra or "",
                "qty": qty,
                "duration_label": f"{row_h:02d}:{row_m:02d}",
                "price": tprice,
                "declined": declined,
            }
        )

    th, tm = total_duration // 60, total_duration % 60
    total_duration_label = f"{th:02d}:{tm:02d}"
    total_price_display = f"{total_price:,}".replace(",", " ")

    issues_list = appointment.problem_items()
    ph = problem_description_hash(appointment.problem_description)
    nested, media_ids = _issue_media_nested_json(appointment)
    im_hash = issue_media_fingerprint(media_ids)
    visit_key, visit_label = _cabinet_visit_fields(appointment)
    visit_lines = appointment.visit_display_lines()

    return jsonify(
        {
            "ok": True,
            "status": appointment.status,
            "status_label": status_label,
            "badge_class": badge_class,
            "is_editable": is_editable,
            "items": items_out,
            "total_duration": total_duration,
            "total_duration_label": total_duration_label,
            "total_price": total_price,
            "total_price_display": total_price_display,
            "issues": issues_list,
            "problem_hash": ph,
            "issue_media": nested,
            "issue_media_hash": im_hash,
            "visit_key": visit_key,
            "visit_label": visit_label,
            "visit_lines": visit_lines,
            "win_number": appointment.win_number or "",
        }
    )


@bp.post("/appointments/<int:appointment_id>/win/update-json")
@login_required
def appointment_win_update_json(appointment_id: int):
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment or appointment.client_user_id != current_user.id:
        return jsonify({"ok": False, "message": "Заявка не найдена."}), 404

    if not _cabinet_client_can_edit_appointment(appointment):
        return jsonify({"ok": False, "message": "Заявку уже нельзя редактировать."}), 400

    payload = request.get_json(silent=True) or {}
    s = normalize_win_number(payload.get("win_number"))
    appointment.win_number = s or None
    db.session.commit()
    return jsonify({"ok": True, "win_number": appointment.win_number or ""})


@bp.post("/appointments/<int:appointment_id>/problems/update-json")
@login_required
def appointment_problems_update_json(appointment_id: int):
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment or appointment.client_user_id != current_user.id:
        return jsonify({"ok": False, "message": "Заявка не найдена."}), 404

    if not _cabinet_client_can_edit_appointment(appointment):
        return jsonify({"ok": False, "message": "Заявку уже нельзя редактировать."}), 400

    payload = request.get_json(silent=True) or {}
    issues = payload.get("issues") or []
    if not isinstance(issues, list):
        return jsonify({"ok": False, "message": "Некорректные данные."}), 400

    cleaned = []
    for x in issues[:30]:
        s = str(x or "").strip()
        if s:
            cleaned.append(s[:2000])

    media_by_slot = payload.get("media_by_slot")
    if media_by_slot is not None:
        if not isinstance(media_by_slot, list):
            return jsonify({"ok": False, "message": "Некорректные данные вложений."}), 400
        if len(media_by_slot) != len(cleaned):
            return jsonify({"ok": False, "message": "Число вложений не совпадает с описаниями."}), 400
        try:
            _apply_issue_media_layout(appointment, media_by_slot, len(cleaned))
        except ValueError:
            return jsonify({"ok": False, "message": "Некорректные вложения."}), 400

    appointment.problem_description = Appointment.problem_description_from_items(cleaned)
    db.session.commit()
    nested, media_ids = _issue_media_nested_json(appointment)
    im_hash = issue_media_fingerprint(media_ids)
    return jsonify(
        {
            "ok": True,
            "issues": appointment.problem_items(),
            "problem_hash": problem_description_hash(appointment.problem_description),
            "issue_media": nested,
            "issue_media_hash": im_hash,
        }
    )


@bp.post("/appointments/<int:appointment_id>/issue-media/upload")
@login_required
def appointment_issue_media_upload(appointment_id: int):
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment or appointment.client_user_id != current_user.id:
        return jsonify({"ok": False, "message": "Заявка не найдена."}), 404
    if not _cabinet_client_can_edit_appointment(appointment):
        return jsonify({"ok": False, "message": "Заявку уже нельзя редактировать."}), 400

    try:
        issue_slot = int(request.form.get("issue_slot", "0"))
    except ValueError:
        issue_slot = 0
    if issue_slot < 0 or issue_slot > 29:
        return jsonify({"ok": False, "message": "Некорректный номер пункта."}), 400

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "message": "Файл не выбран."}), 400

    orig = secure_filename(f.filename)
    ext = os.path.splitext(orig)[1].lower()
    if ext not in _ISSUE_MEDIA_EXT:
        return jsonify({"ok": False, "message": "Недопустимый тип файла."}), 400

    mime = (f.mimetype or "").lower()
    if not _issue_mime_ok(mime):
        return jsonify({"ok": False, "message": "Недопустимый тип файла."}), 400

    subdir = os.path.join("appointment_issues", str(appointment.id))
    store_name = f"{uuid.uuid4().hex}{ext}"
    rel_path = f"{subdir}/{store_name}".replace(os.sep, "/")
    abs_dir = os.path.join(current_app.config["DOCUMENTS_DIR"], subdir)
    os.makedirs(abs_dir, exist_ok=True)
    abs_path = os.path.join(abs_dir, store_name)
    f.save(abs_path)
    size_b = os.path.getsize(abs_path)
    if size_b > _MAX_ISSUE_MEDIA_BYTES:
        try:
            os.remove(abs_path)
        except OSError:
            pass
        return jsonify({"ok": False, "message": "Файл слишком большой (макс. 20 МБ)."}), 400

    n_in_slot = int(
        db.session.execute(
            select(func.count())
            .select_from(AppointmentIssueMedia)
            .where(
                AppointmentIssueMedia.appointment_id == appointment.id,
                AppointmentIssueMedia.issue_slot == issue_slot,
            )
        ).scalar_one()
        or 0
    )

    row = AppointmentIssueMedia(
        appointment_id=appointment.id,
        issue_slot=issue_slot,
        sort_order=int(n_in_slot),
        filename=orig or store_name,
        mime=mime or "application/octet-stream",
        storage_path=rel_path,
        size_bytes=size_b,
    )
    db.session.add(row)
    db.session.commit()

    _, media_ids = _issue_media_nested_json(appointment)
    im_hash = issue_media_fingerprint(media_ids)

    return jsonify(
        {
            "ok": True,
            "issue_media_hash": im_hash,
            "media": {
                "id": row.id,
                "mime": row.mime,
                "url": url_for(
                    "cabinet.appointment_issue_media_file",
                    appointment_id=appointment.id,
                    media_id=row.id,
                ),
            },
        }
    )


@bp.post("/appointments/<int:appointment_id>/issue-media/<int:media_id>/delete-json")
@login_required
def appointment_issue_media_delete_json(appointment_id: int, media_id: int):
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment or appointment.client_user_id != current_user.id:
        return jsonify({"ok": False, "message": "Заявка не найдена."}), 404
    if not _cabinet_client_can_edit_appointment(appointment):
        return jsonify({"ok": False, "message": "Заявку уже нельзя редактировать."}), 400

    m = db.session.get(AppointmentIssueMedia, media_id)
    if not m or m.appointment_id != appointment.id:
        return jsonify({"ok": False, "message": "Файл не найден."}), 404

    delete_appointment_issue_media_file(m)
    db.session.delete(m)
    db.session.commit()

    _, media_ids = _issue_media_nested_json(appointment)
    im_hash = issue_media_fingerprint(media_ids)

    return jsonify({"ok": True, "issue_media_hash": im_hash})


@bp.get("/appointments/<int:appointment_id>/issue-media/<int:media_id>/file")
@login_required
def appointment_issue_media_file(appointment_id: int, media_id: int):
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment or appointment.client_user_id != current_user.id:
        abort(403)
    m = db.session.get(AppointmentIssueMedia, media_id)
    if not m or m.appointment_id != appointment.id:
        abort(404)
    directory = current_app.config["DOCUMENTS_DIR"]
    rel = m.storage_path.replace("/", os.sep)
    folder = os.path.dirname(rel)
    name = os.path.basename(rel)
    return send_from_directory(os.path.join(directory, folder), name, mimetype=m.mime)


@bp.post("/appointments/<int:appointment_id>/items/delete-json/<int:item_id>")
@login_required
def appointment_item_delete_json(appointment_id: int, item_id: int):
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment or appointment.client_user_id != current_user.id:
        return jsonify({"ok": False, "message": "Заявка не найдена."}), 404

    if not _cabinet_client_can_edit_appointment(appointment):
        return jsonify({"ok": False, "message": "Заявку уже нельзя редактировать."}), 400

    item = db.session.get(AppointmentItem, item_id)
    if not item or item.appointment_id != appointment.id:
        return jsonify({"ok": False, "message": "Позиция не найдена."}), 404

    item.declined_by_client = not bool(item.declined_by_client)
    db.session.commit()

    rows = db.session.execute(
        db.select(
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
    ).all()

    total_price = 0
    total_duration = 0
    for price_snapshot, k2, qty, duration_snapshot, k1, duration_min, declined_by_client in rows:
        if bool(declined_by_client):
            continue
        q = int(qty or 1)
        p = int(price_snapshot or 0)
        d = int(duration_snapshot or duration_min or 0)
        total_price += int(round(p * float(k2 or 1.0) * q))
        total_duration += int(round(d * float(k1 or 1.0) * q))

    return jsonify(
        {
            "ok": True,
            "item_id": int(item_id),
            "count": len(rows),
            "declined_by_client": bool(item.declined_by_client),
            "total_price": int(total_price),
            "total_duration": int(total_duration),
        }
    )


@bp.get("/telegram")
@login_required
def telegram():
    link = db.session.execute(
        db.select(TelegramLink).where(TelegramLink.user_id == current_user.id)
    ).scalar_one_or_none()
    return render_template("cabinet/telegram.html", link=link)


@bp.post("/telegram/unlink")
@login_required
def unlink_telegram():
    link = db.session.execute(
        db.select(TelegramLink).where(TelegramLink.user_id == current_user.id)
    ).scalar_one_or_none()
    if link:
        db.session.delete(link)
        db.session.commit()
        flash("Telegram-аккаунт отвязан от бота.", "success")
    else:
        flash("Активная привязка Telegram не найдена.", "warning")
    return redirect(url_for("cabinet.telegram"))


@bp.post("/telegram/generate-token")
@login_required
def generate_telegram_token():
    token_str = token_urlsafe(8)
    token_hash = generate_password_hash(token_str)
    
    # Удаляем старые неиспользованные токены этого юзера
    db.session.execute(
        db.delete(TelegramLinkToken).where(TelegramLinkToken.user_id == current_user.id, TelegramLinkToken.used_at.is_(None))
    )
    
    token = TelegramLinkToken(
        user_id=current_user.id,
        token_hash=token_hash,
        expires_at=datetime.utcnow() + timedelta(minutes=10)
    )
    db.session.add(token)
    db.session.commit()
    
    flash(f"Ваш код привязки: {token_str}. Он действует 10 минут.", "info")
    return redirect(url_for("cabinet.telegram"))


@bp.get("/documents/<int:doc_id>")
@login_required
def download_document(doc_id):
    doc = db.session.get(WorkOrderDocument, doc_id)
    if not doc:
        abort(404)
        
    # Проверка прав: документ должен принадлежать заказу текущего пользователя
    if doc.work_order.client_user_id != current_user.id and current_user.role != 'admin':
        abort(403)
        
    directory = current_app.config["DOCUMENTS_DIR"]
    # Для изображений разрешаем просмотр в браузере
    is_image = doc.mime and doc.mime.startswith('image/')
    return send_from_directory(directory, doc.storage_path, as_attachment=not is_image)
