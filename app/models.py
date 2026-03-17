from __future__ import annotations

from datetime import datetime

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


class Work(db.Model):
    __tablename__ = "works"

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("work_categories.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    duration_min = db.Column(db.Integer, nullable=False)
    base_price = db.Column(db.Integer, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    category = db.relationship("WorkCategory")


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


class AppointmentSlot(db.Model):
    __tablename__ = "appointment_slots"

    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(
        db.Integer, db.ForeignKey("appointments.id"), nullable=False, index=True
    )
    slot_id = db.Column(db.Integer, db.ForeignKey("time_slots.id"), nullable=False, unique=True)

    appointment = db.relationship("Appointment", back_populates="slots")
    slot = db.relationship("TimeSlot")


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
    closed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    appointment = db.relationship("Appointment", back_populates="work_order")
    client = db.relationship("User")
    master = db.relationship("Master")
    documents = db.relationship("WorkOrderDocument", back_populates="work_order", cascade="all, delete-orphan")
    items = db.relationship("WorkOrderItem", back_populates="work_order", cascade="all, delete-orphan")
    parts = db.relationship("WorkOrderPart", back_populates="work_order", cascade="all, delete-orphan")


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
