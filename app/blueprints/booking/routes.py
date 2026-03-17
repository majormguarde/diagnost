from __future__ import annotations

from datetime import datetime, time, timedelta
from math import ceil
from typing import Iterable

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from ...extensions import db
from ...models import Appointment, AppointmentItem, AppointmentSlot, Master, TimeSlot, Work, OrganizationSettings
from .forms import BookingConfirmForm, BookingStep1Form


bp = Blueprint("booking", __name__)


def _day_bounds(d) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time.min)
    end = datetime.combine(d, time.max)
    return start, end


def _total_duration_min(work_ids: Iterable[int]) -> int:
    ids = [int(x) for x in work_ids if x]
    if not ids:
        return 0
    rows = db.session.execute(db.select(Work.duration_min).where(Work.id.in_(ids))).all()
    return sum(int(r[0]) for r in rows)


def _slots_needed(total_duration_min: int, default_duration_min: int) -> int:
    duration = total_duration_min if total_duration_min > 0 else default_duration_min
    return max(1, int(ceil(duration / 30)))


def _fetch_free_slots(master_id: int, date_value, min_start_at: datetime | None = None):
    start, end = _day_bounds(date_value)
    subq = db.select(AppointmentSlot.slot_id).subquery()
    q = (
        db.select(TimeSlot)
        .where(TimeSlot.master_id == master_id)
        .where(TimeSlot.start_at >= start, TimeSlot.start_at <= end)
        .where(TimeSlot.status == "free")
        .where(~TimeSlot.id.in_(db.select(subq.c.slot_id)))
        .order_by(TimeSlot.start_at.asc())
    )
    if min_start_at is not None:
        q = q.where(TimeSlot.start_at >= min_start_at)
    return db.session.execute(q).scalars().all()


def _available_starts(slots: list[TimeSlot], slots_needed: int) -> list[list[TimeSlot]]:
    if slots_needed <= 0:
        return []
    if len(slots) < slots_needed:
        return []

    sequences: list[list[TimeSlot]] = []
    for i in range(0, len(slots) - slots_needed + 1):
        ok = True
        for j in range(i, i + slots_needed - 1):
            if slots[j].end_at != slots[j + 1].start_at:
                ok = False
                break
        if ok:
            sequences.append(slots[i : i + slots_needed])
    return sequences


@bp.route("/", methods=["GET", "POST"])
def step1():
    masters = db.session.execute(
        db.select(Master).where(Master.is_active.is_(True)).order_by(Master.name.asc())
    ).scalars().all()
    works = db.session.execute(
        db.select(Work).where(Work.is_active.is_(True)).order_by(Work.title.asc())
    ).scalars().all()

    form = BookingStep1Form()
    form.master_id.choices = [(m.id, m.name) for m in masters]

    if form.validate_on_submit():
        date_value = form.date.data
        # Проверка рабочих дней (0=Пн, 6=Вс в БД, weekday() в Python тоже 0=Пн, 6=Вс)
        settings = OrganizationSettings.get_settings()
        if settings.work_days is not None:
            work_days = settings.work_days.split(",")
            if str(date_value.weekday()) not in work_days:
                flash("Извините, в выбранный день мы не работаем. Пожалуйста, выберите другой день.", "warning")
                return render_template("booking/step1.html", form=form, today_str=datetime.now().date().isoformat())

        return redirect(
            url_for(
                "booking.slots",
                master_id=form.master_id.data,
                date=form.date.data.isoformat(),
                car_make=form.car_make.data,
                car_model=form.car_model.data,
                car_year=form.car_year.data or "",
                car_number=form.car_number.data or "",
                problem_description=form.problem_description.data,
            )
        )

    return render_template("booking/step1.html", form=form, today_str=datetime.now().date().isoformat())


@bp.get("/slots")
def slots():
    master_id = request.args.get("master_id", type=int)
    date_str = request.args.get("date", type=str)
    
    # Данные из step1
    car_make = request.args.get("car_make", "")
    car_model = request.args.get("car_model", "")
    car_year = request.args.get("car_year", "")
    car_number = request.args.get("car_number", "")
    problem_description = request.args.get("problem_description", "")

    if not master_id or not date_str:
        flash("Выберите мастера и дату", "warning")
        return redirect(url_for("booking.step1"))

    try:
        date_value = datetime.fromisoformat(date_str).date()
    except ValueError:
        flash("Некорректная дата", "danger")
        return redirect(url_for("booking.step1"))

    master = db.session.get(Master, master_id)
    if not master or not master.is_active:
        flash("Мастер не найден", "danger")
        return redirect(url_for("booking.step1"))

    # По умолчанию для диагностики выделяем 1 час (60 минут)
    total_duration = 60 
    needed = _slots_needed(
        total_duration, int(current_app.config.get("DEFAULT_DURATION_MIN", 60))
    )
    min_start_at = datetime.now() + timedelta(
        minutes=int(current_app.config.get("MIN_LEAD_TIME_MIN", 30))
    )
    free_slots = _fetch_free_slots(master.id, date_value, min_start_at=min_start_at)
    sequences = _available_starts(free_slots, needed)

    starts = []
    for seq in sequences:
        start_slot = seq[0]
        end_at = seq[-1].end_at
        starts.append((start_slot.id, f"{start_slot.start_at:%H:%M} — {end_at:%H:%M}"))

    form = BookingConfirmForm()
    form.start_slot_id.choices = starts

    return render_template(
        "booking/slots.html",
        master=master,
        date_value=date_value,
        car_make=car_make,
        car_model=car_model,
        car_year=car_year,
        car_number=car_number,
        problem_description=problem_description,
        total_duration=total_duration,
        slots_needed=needed,
        form=form,
        no_slots=(len(starts) == 0),
    )


@bp.route("/confirm", methods=["POST"])
@login_required
def confirm():
    master_id = request.form.get("master_id", type=int)
    date_str = request.form.get("date", type=str)
    
    # Данные автомобиля
    car_make = request.form.get("car_make", "")
    car_model = request.form.get("car_model", "")
    car_year = request.form.get("car_year", "")
    car_number = request.form.get("car_number", "")
    problem_description = request.form.get("problem_description", "")

    if not master_id or not date_str:
        flash("Некорректные данные", "danger")
        return redirect(url_for("booking.step1"))

    try:
        date_value = datetime.fromisoformat(date_str).date()
    except ValueError:
        flash("Некорректная дата", "danger")
        return redirect(url_for("booking.step1"))

    master = db.session.get(Master, master_id)
    if not master or not master.is_active:
        flash("Мастер не найден", "danger")
        return redirect(url_for("booking.step1"))

    # Длительность по умолчанию 60 мин
    total_duration = 60
    default_duration_min = int(current_app.config.get("DEFAULT_DURATION_MIN", 60))
    needed = _slots_needed(total_duration, default_duration_min)

    min_start_at = datetime.now() + timedelta(
        minutes=int(current_app.config.get("MIN_LEAD_TIME_MIN", 30))
    )
    free_slots = _fetch_free_slots(master.id, date_value, min_start_at=min_start_at)
    sequences = _available_starts(free_slots, needed)
    available_by_start_id = {seq[0].id: seq for seq in sequences}

    form = BookingConfirmForm()
    form.start_slot_id.choices = [(sid, "") for sid in available_by_start_id.keys()]
    if not form.validate_on_submit():
        flash("Выберите время начала", "warning")
        return redirect(
            url_for(
                "booking.slots",
                master_id=master.id,
                date=date_value.isoformat(),
                car_make=car_make,
                car_model=car_model,
                car_year=car_year,
                car_number=car_number,
                problem_description=problem_description,
            )
        )

    selected_seq = available_by_start_id.get(form.start_slot_id.data)
    if not selected_seq:
        flash("Выбранное время больше недоступно", "warning")
        return redirect(
            url_for(
                "booking.slots",
                master_id=master.id,
                date=date_value.isoformat(),
                car_make=car_make,
                car_model=car_model,
                car_year=car_year,
                car_number=car_number,
                problem_description=problem_description,
            )
        )

    appointment = Appointment(
        client_user_id=current_user.id,
        master_id=master.id,
        start_at=selected_seq[0].start_at,
        end_at=selected_seq[-1].end_at,
        status="new",
        car_make=car_make,
        car_model=car_model,
        car_year=int(car_year) if car_year.isdigit() else None,
        car_number=car_number,
        problem_description=problem_description,
    )

    for slot in selected_seq:
        appointment.slots.append(AppointmentSlot(slot_id=slot.id))

    try:
        db.session.add(appointment)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Кто-то только что занял это время. Выберите другое окно.", "warning")
        return redirect(
            url_for(
                "booking.slots",
                master_id=master.id,
                date=date_value.isoformat(),
                car_make=car_make,
                car_model=car_model,
                car_year=car_year,
                car_number=car_number,
                problem_description=problem_description,
            )
        )

    flash(f"Заявка создана: №{appointment.id}", "success")
    return redirect(url_for("cabinet.appointment_detail", appointment_id=appointment.id))
