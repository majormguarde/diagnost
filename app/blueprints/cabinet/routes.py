from __future__ import annotations

from datetime import datetime, timedelta
from secrets import token_urlsafe
import os

from flask import Blueprint, current_app, flash, redirect, render_template, url_for, send_from_directory, abort
from flask_login import current_user, login_required
from werkzeug.security import generate_password_hash

from ...extensions import db
from ...models import Appointment, AppointmentItem, TelegramLinkToken, Work, WorkOrder, WorkOrderDocument, TelegramLink

bp = Blueprint("cabinet", __name__)


@bp.get("/")
@login_required
def index():
    # Показываем заказы и заявки
    appointments = db.session.execute(
        db.select(Appointment).where(Appointment.client_user_id == current_user.id).order_by(Appointment.start_at.desc())
    ).scalars().all()
    
    orders = db.session.execute(
        db.select(WorkOrder).where(WorkOrder.client_user_id == current_user.id).order_by(WorkOrder.id.desc())
    ).scalars().all()
    
    return render_template("cabinet/index.html", appointments=appointments, orders=orders)


@bp.get("/appointments/<int:appointment_id>")
@login_required
def appointment_detail(appointment_id: int):
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment or appointment.client_user_id != current_user.id:
        abort(404)
    return render_template("cabinet/appointment_detail.html", appointment=appointment)


@bp.get("/telegram")
@login_required
def telegram():
    link = db.session.execute(
        db.select(TelegramLink).where(TelegramLink.user_id == current_user.id)
    ).scalar_one_or_none()
    return render_template("cabinet/telegram.html", link=link)


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
