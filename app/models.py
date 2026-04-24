from __future__ import annotations

from datetime import datetime
import re

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.String(32), nullable=False, default="client")
    phone = db.Column(db.String(32), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # Контакты клиента (WhatsApp / Telegram / email), указываются при регистрации и в админке
    client_whatsapp = db.Column(db.String(32), nullable=True)
    client_telegram = db.Column(db.String(64), nullable=True)
    client_email = db.Column(db.String(120), nullable=True)

    telegram_link = db.relationship(
        "TelegramLink", uselist=False, back_populates="user", cascade="all, delete-orphan"
    )

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Master(db.Model):
    __tablename__ = "masters"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    position = db.Column(db.String(120), nullable=True)
    description = db.Column(db.Text, nullable=True)
    phone = db.Column(db.String(32), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    payout_percent = db.Column(db.Integer, nullable=False, default=100)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    competencies = db.relationship("Competency", secondary="master_competencies", backref="masters")


class Competency(db.Model):
    __tablename__ = "competencies"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False, unique=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    def __repr__(self):
        return f"<Competency {self.title}>"


class MasterCompetency(db.Model):
    __tablename__ = "master_competencies"

    id = db.Column(db.Integer, primary_key=True)
    master_id = db.Column(db.Integer, db.ForeignKey("masters.id"), nullable=False, index=True)
    competency_id = db.Column(db.Integer, db.ForeignKey("competencies.id"), nullable=False, index=True)

    __table_args__ = (db.UniqueConstraint("master_id", "competency_id", name="uq_master_competency"),)


class WorkCategory(db.Model):
    __tablename__ = "work_categories"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False, unique=True)
    competency_id = db.Column(db.Integer, db.ForeignKey("competencies.id"), nullable=True, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    competency = db.relationship("Competency")


class Work(db.Model):
    __tablename__ = "works"

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("work_categories.id"), nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    duration_min = db.Column(db.Integer, nullable=False)
    base_price = db.Column(db.Integer, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    category = db.relationship("WorkCategory")

class CarMake(db.Model):
    __tablename__ = "car_makes"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    key = db.Column(db.String(120), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class TimeSlot(db.Model):
    __tablename__ = "time_slots"

    id = db.Column(db.Integer, primary_key=True)
    master_id = db.Column(db.Integer, db.ForeignKey("masters.id"), nullable=False, index=True)
    start_at = db.Column(db.DateTime, nullable=False, index=True)
    end_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(16), nullable=False, default="free")

    __table_args__ = (db.UniqueConstraint("master_id", "start_at", name="uq_master_start"),)


class Appointment(db.Model):
    __tablename__ = "appointments"

    id = db.Column(db.Integer, primary_key=True)
    client_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    master_id = db.Column(db.Integer, db.ForeignKey("masters.id"), nullable=False, index=True)

    # Данные автомобиля и описание проблемы
    car_make = db.Column(db.String(50), nullable=True)
    car_model = db.Column(db.String(50), nullable=True)
    car_year = db.Column(db.Integer, nullable=True)
    car_number = db.Column(db.String(20), nullable=True)
    win_number = db.Column(db.String(32), nullable=True)
    problem_description = db.Column(db.Text, nullable=True)

    start_at = db.Column(db.DateTime, nullable=False, index=True)
    end_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(32), nullable=False, default="new")
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    cancel_reason = db.Column(db.String(255), nullable=True)

    client = db.relationship("User")
    master = db.relationship("Master")
    work_order = db.relationship("WorkOrder", back_populates="appointment", uselist=False, cascade="all, delete-orphan")
    slots = db.relationship(
        "AppointmentSlot", back_populates="appointment", cascade="all, delete-orphan"
    )
    items = db.relationship(
        "AppointmentItem", back_populates="appointment", cascade="all, delete-orphan"
    )
    issue_media = db.relationship(
        "AppointmentIssueMedia", back_populates="appointment", cascade="all, delete-orphan"
    )

    def problem_items(self) -> list[str]:
        text = (self.problem_description or "").strip()
        if not text:
            return []

        items: list[str] = []
        current: list[str] = []
        found_numbered = False

        for line in text.splitlines():
            m = re.match(r"^\s*(\d+)\s*[\)\.\-:]\s*(.*)\s*$", line)
            if m:
                found_numbered = True
                if current:
                    joined = "\n".join(current).strip()
                    if joined:
                        items.append(joined)
                current = [m.group(2)]
                continue

            current.append(line)

        if current:
            joined = "\n".join(current).strip()
            if joined:
                items.append(joined)

        if found_numbered:
            return [x for x in items if x]

        chunks = [c.strip() for c in re.split(r"\n\s*\n+", text) if c.strip()]
        return chunks if chunks else []

    @staticmethod
    def problem_description_from_items(items: list[str]) -> str:
        cleaned = []
        for x in items or []:
            s = str(x or "").strip()
            if s:
                cleaned.append(s)
        if not cleaned:
            return ""
        return "\n\n".join(f"{i + 1}) {t}" for i, t in enumerate(cleaned))

    def visit_display_lines(self) -> list[str]:
        """Интервалы приёма: каждая строка — отдельный блок (удобно для нескольких дней)."""
        ap_slots = [x for x in self.slots if x.slot]
        if not ap_slots:
            if self.start_at and self.end_at:
                return [
                    f"{self.start_at.strftime('%d.%m.%Y %H:%M')}–{self.end_at.strftime('%H:%M')}"
                ]
            return []
        ap_slots.sort(key=lambda x: x.slot.start_at)
        by_day: dict = {}
        for x in ap_slots:
            d = x.slot.start_at.date()
            by_day.setdefault(d, []).append(x.slot)
        parts: list[str] = []
        for d in sorted(by_day.keys()):
            day_slots = sorted(by_day[d], key=lambda s: s.start_at)
            i = 0
            while i < len(day_slots):
                smin = day_slots[i].start_at
                smax = day_slots[i].end_at
                j = i
                while j + 1 < len(day_slots) and day_slots[j].end_at == day_slots[j + 1].start_at:
                    j += 1
                    smax = day_slots[j].end_at
                parts.append(f"{smin.strftime('%d.%m.%Y %H:%M')}–{smax.strftime('%H:%M')}")
                i = j + 1
        return parts

    def visit_display_label(self) -> str:
        """Одна строка через «;» (списки, админка)."""
        return "; ".join(self.visit_display_lines())

    def visit_fingerprint(self) -> str:
        """Сравнение изменений времени (в т.ч. несколько дней)."""
        ap_slots = [x for x in self.slots if x.slot]
        if not ap_slots:
            sa = self.start_at.isoformat() if self.start_at else ""
            ea = self.end_at.isoformat() if self.end_at else ""
            return f"{sa}|{ea}"
        ap_slots.sort(key=lambda x: x.slot.start_at)
        return "|".join(
            f"{x.slot.start_at.isoformat()}@{x.slot.end_at.isoformat()}" for x in ap_slots
        )


class AppointmentSlot(db.Model):
    __tablename__ = "appointment_slots"

    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(
        db.Integer, db.ForeignKey("appointments.id"), nullable=False, index=True
    )
    slot_id = db.Column(db.Integer, db.ForeignKey("time_slots.id"), nullable=False, unique=True)

    appointment = db.relationship("Appointment", back_populates="slots")
    slot = db.relationship("TimeSlot")


class AppointmentIssueMedia(db.Model):
    """Фото/видео к пунктам описания неисправностей (кабинет клиента)."""

    __tablename__ = "appointment_issue_media"

    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(
        db.Integer, db.ForeignKey("appointments.id"), nullable=False, index=True
    )
    issue_slot = db.Column(db.Integer, nullable=False, default=0)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    filename = db.Column(db.String(255), nullable=False)
    mime = db.Column(db.String(100), nullable=False)
    storage_path = db.Column(db.String(500), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    appointment = db.relationship("Appointment", back_populates="issue_media")


class AppointmentItem(db.Model):
    __tablename__ = "appointment_items"

    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(
        db.Integer, db.ForeignKey("appointments.id"), nullable=False, index=True
    )
    work_id = db.Column(db.Integer, db.ForeignKey("works.id"), nullable=False)
    qty = db.Column(db.Integer, nullable=False, default=1)
    price_snapshot = db.Column(db.Integer, nullable=True)
    duration_snapshot = db.Column(db.Integer, nullable=True)
    k1 = db.Column(db.Float, nullable=False, default=1.0)
    k2 = db.Column(db.Float, nullable=False, default=1.0)
    extra = db.Column(db.String(255), nullable=True)
    declined_by_client = db.Column(db.Boolean, nullable=False, default=False)

    appointment = db.relationship("Appointment", back_populates="items")
    work = db.relationship("Work")


class WorkOrder(db.Model):
    __tablename__ = "work_orders"

    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointments.id"), nullable=True, index=True)
    client_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    master_id = db.Column(db.Integer, db.ForeignKey("masters.id"), nullable=False, index=True)
    status = db.Column(db.String(32), nullable=False, default="new")
    total_amount = db.Column(db.Integer, nullable=True)
    is_paid = db.Column(db.Boolean, nullable=False, default=False)
    inspection_results = db.Column(db.Text, nullable=True)
    complaint_description = db.Column(db.Text, nullable=True)
    closed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    appointment = db.relationship("Appointment", back_populates="work_order")
    client = db.relationship("User")
    master = db.relationship("Master")
    documents = db.relationship("WorkOrderDocument", back_populates="work_order", cascade="all, delete-orphan")
    items = db.relationship("WorkOrderItem", back_populates="work_order", cascade="all, delete-orphan")
    parts = db.relationship("WorkOrderPart", back_populates="work_order", cascade="all, delete-orphan")
    details = db.relationship("WorkOrderDetail", back_populates="work_order", cascade="all, delete-orphan")
    materials = db.relationship("WorkOrderMaterial", back_populates="work_order", cascade="all, delete-orphan")
    additional_works = db.relationship(
        "WorkOrderAdditionalWork", back_populates="work_order", cascade="all, delete-orphan"
    )
    complaints = db.relationship("WorkOrderComplaintItem", back_populates="work_order", cascade="all, delete-orphan")
    telegram_delivery_codes = db.relationship(
        "WorkOrderTelegramCode", back_populates="work_order", cascade="all, delete-orphan"
    )


class WorkOrderTelegramCode(db.Model):
    """Одноразовый код для получения текста заказ-наряда через Telegram-бота (/zakaz КОД)."""

    __tablename__ = "work_order_telegram_codes"

    id = db.Column(db.Integer, primary_key=True)
    work_order_id = db.Column(db.Integer, db.ForeignKey("work_orders.id"), nullable=False, index=True)
    code = db.Column(db.String(24), nullable=False, unique=True, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    work_order = db.relationship("WorkOrder", back_populates="telegram_delivery_codes")


class WorkOrderComplaintItem(db.Model):
    """Жалобы клиента для заказ-наряда."""
    __tablename__ = "work_order_complaint_items"

    id = db.Column(db.Integer, primary_key=True)
    work_order_id = db.Column(db.Integer, db.ForeignKey("work_orders.id"), nullable=False, index=True)
    description = db.Column(db.Text, nullable=False)
    is_done = db.Column(db.Boolean, nullable=False, default=False)
    is_refused = db.Column(db.Boolean, nullable=False, default=False)
    refusal_reason = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    work_order = db.relationship("WorkOrder", back_populates="complaints")


class WorkOrderItem(db.Model):
    __tablename__ = "work_order_items"

    id = db.Column(db.Integer, primary_key=True)
    work_order_id = db.Column(db.Integer, db.ForeignKey("work_orders.id"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    duration = db.Column(db.Integer, nullable=False, default=0)
    actual_duration = db.Column(db.Integer, nullable=True, default=0)
    price = db.Column(db.Integer, nullable=False, default=0)
    is_done = db.Column(db.Boolean, nullable=False, default=False)
    comment = db.Column(db.Text, nullable=True)
    master_id = db.Column(db.Integer, db.ForeignKey("masters.id"), nullable=True)
    is_paid = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    work_order = db.relationship("WorkOrder", back_populates="items")
    master = db.relationship("Master")


class WorkOrderPart(db.Model):
    __tablename__ = "work_order_parts"

    id = db.Column(db.Integer, primary_key=True)
    work_order_id = db.Column(db.Integer, db.ForeignKey("work_orders.id"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=1.0)
    unit = db.Column(db.String(20), nullable=True, default="шт.")
    price = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    work_order = db.relationship("WorkOrder", back_populates="parts")


class WorkOrderDetail(db.Model):
    """Детали (запчасти) для заказ-наряда."""
    __tablename__ = "work_order_details"

    id = db.Column(db.Integer, primary_key=True)
    work_order_id = db.Column(db.Integer, db.ForeignKey("work_orders.id"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=1.0)
    unit = db.Column(db.String(20), nullable=True, default="шт.")
    price = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    work_order = db.relationship("WorkOrder", back_populates="details")


class WorkOrderMaterial(db.Model):
    """Расходные материалы для заказ-наряда."""
    __tablename__ = "work_order_materials"

    id = db.Column(db.Integer, primary_key=True)
    work_order_id = db.Column(db.Integer, db.ForeignKey("work_orders.id"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=1.0)
    unit = db.Column(db.String(20), nullable=True, default="шт.")
    price = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    work_order = db.relationship("WorkOrder", back_populates="materials")


class WorkOrderAdditionalWork(db.Model):
    """Доп. работы сторонних исполнителей (мойка, сварка и т.п.); не участвуют в %% мастеру."""

    __tablename__ = "work_order_additional_works"

    id = db.Column(db.Integer, primary_key=True)
    work_order_id = db.Column(db.Integer, db.ForeignKey("work_orders.id"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    price = db.Column(db.Integer, nullable=False, default=0)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    work_order = db.relationship("WorkOrder", back_populates="additional_works")


class WorkOrderDocument(db.Model):
    __tablename__ = "work_order_documents"

    id = db.Column(db.Integer, primary_key=True)
    work_order_id = db.Column(db.Integer, db.ForeignKey("work_orders.id"), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    mime = db.Column(db.String(100), nullable=False)
    storage_path = db.Column(db.String(500), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    work_order = db.relationship("WorkOrder", back_populates="documents")


class TelegramLinkToken(db.Model):
    __tablename__ = "telegram_link_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    token_hash = db.Column(db.String(255), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class TelegramLink(db.Model):
    __tablename__ = "telegram_links"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True)
    telegram_chat_id = db.Column(db.String(64), nullable=False, unique=True)
    linked_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    user = db.relationship("User", back_populates="telegram_link")


class OrganizationSettings(db.Model):
    __tablename__ = "organization_settings"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, default="DIAGNOST.EXE")
    address = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(32), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    work_hours = db.Column(db.String(120), nullable=True)
    description = db.Column(db.Text, nullable=True)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    work_days = db.Column(db.String(100), nullable=True, default="0,1,2,3,4") # 0-6, comma separated (0 is Monday)
    slot_minutes = db.Column(db.Integer, nullable=False, default=60)
    # Публичные мессенджеры сервиса и исходящая почта (страница «Связь» в админке)
    org_whatsapp = db.Column(db.String(32), nullable=True)
    org_telegram = db.Column(db.String(64), nullable=True)
    smtp_host = db.Column(db.String(120), nullable=True)
    smtp_port = db.Column(db.Integer, nullable=True)
    smtp_user = db.Column(db.String(120), nullable=True)
    smtp_password = db.Column(db.String(255), nullable=True)
    smtp_use_tls = db.Column(db.Boolean, nullable=False, default=True)
    smtp_from = db.Column(db.String(120), nullable=True)
    # Telegram-бот (заказ-наряды по коду / webhook); публичный URL — для подсказки настройки вебхука
    telegram_bot_username = db.Column(db.String(64), nullable=True)
    telegram_bot_token = db.Column(db.String(255), nullable=True)
    site_public_url = db.Column(db.String(255), nullable=True)
    # СБП (оплата по QR/по телефону)
    sbp_phone = db.Column(db.String(32), nullable=True)

    @classmethod
    def get_settings(cls):
        settings = db.session.execute(db.select(cls)).scalar()
        if not settings:
            settings = cls()
            db.session.add(settings)
            db.session.commit()
        return settings

class Banner(db.Model):
    __tablename__ = "banners"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=True)
    subtitle = db.Column(db.String(200), nullable=True)
    image_path = db.Column(db.String(255), nullable=False)
    link = db.Column(db.String(255), nullable=True)
    order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Review(db.Model):
    __tablename__ = "reviews"

    id = db.Column(db.Integer, primary_key=True)
    author_name = db.Column(db.String(100), nullable=False)
    author_car = db.Column(db.String(100), nullable=True)
    text = db.Column(db.Text, nullable=False)
    rating = db.Column(db.Integer, default=5)
    is_published = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class CashFlow(db.Model):
    __tablename__ = "cash_flow"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    amount = db.Column(db.Integer, nullable=False) # Положительное - приход, отрицательное - расход
    category = db.Column(db.String(100), nullable=False) # "Оплата услуг", "Выплата мастеру", "Прочее"
    description = db.Column(db.Text, nullable=True)
    work_order_id = db.Column(db.Integer, db.ForeignKey("work_orders.id"), nullable=True)
    master_id = db.Column(db.Integer, db.ForeignKey("masters.id"), nullable=True)

    work_order = db.relationship("WorkOrder")
    master = db.relationship("Master")
