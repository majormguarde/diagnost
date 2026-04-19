from __future__ import annotations

from calendar import monthrange
from datetime import datetime, time, timedelta
from math import ceil
from typing import Iterable

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from ...extensions import db
from ...models import Appointment, AppointmentItem, AppointmentSlot, CarMake, Master, TimeSlot, Work, OrganizationSettings
from ...utils import car_make_key, normalize_car_make, normalize_win_number
from .forms import BookingConfirmForm, BookingStep1Form


bp = Blueprint("booking", __name__)

def _seed_car_makes_if_needed() -> None:
    exists = db.session.execute(db.select(CarMake.id).limit(1)).scalar_one_or_none()
    if exists:
        return

    makes = [
        "Acura",
        "Alfa Romeo",
        "Audi",
        "BMW",
        "BYD",
        "Cadillac",
        "Changan",
        "Chery",
        "Chevrolet",
        "Chrysler",
        "Citroen",
        "Cupra",
        "Dacia",
        "Daewoo",
        "Daihatsu",
        "Dodge",
        "DS",
        "Exeed",
        "Fiat",
        "Ford",
        "GAC",
        "Geely",
        "Genesis",
        "GMC",
        "Great Wall",
        "Haval",
        "Honda",
        "Hongqi",
        "Hyundai",
        "Infiniti",
        "Isuzu",
        "Jaguar",
        "Jaecoo",
        "JAC",
        "Jeep",
        "Jetour",
        "KIA",
        "Lada",
        "Lamborghini",
        "Land Rover",
        "Lexus",
        "Lifan",
        "Lincoln",
        "Lotus",
        "Maserati",
        "Mazda",
        "Mercedes-Benz",
        "Mini",
        "Mitsubishi",
        "MG",
        "Nissan",
        "Omoda",
        "Opel",
        "Peugeot",
        "Polestar",
        "Porsche",
        "RAM",
        "Renault",
        "Skoda",
        "Smart",
        "SsangYong",
        "Subaru",
        "Suzuki",
        "Tank",
        "Tesla",
        "Toyota",
        "UAZ",
        "Volkswagen",
        "Volvo",
        "Voyah",
        "Zeekr",
        "ГАЗ",
    ]

    for raw in makes:
        name = normalize_car_make(raw)
        key = car_make_key(name)
        if not key:
            continue
        db.session.add(CarMake(name=name, key=key))
    db.session.commit()


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


def _slot_minutes() -> int:
    settings = OrganizationSettings.get_settings()
    value = int(settings.slot_minutes or 60)
    return value if value >= 15 else 15


def _slots_needed(total_duration_min: int, default_duration_min: int, slot_minutes: int) -> int:
    duration = total_duration_min if total_duration_min > 0 else default_duration_min
    return max(1, int(ceil(duration / slot_minutes)))


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


def _master_available_dates(master_id: int) -> list[str]:
    settings = OrganizationSettings.get_settings()
    work_days = set(settings.work_days.split(",")) if settings.work_days else None
    default_duration_min = int(current_app.config.get("DEFAULT_DURATION_MIN", 60))
    needed = _slots_needed(60, default_duration_min, _slot_minutes())
    min_start_at = datetime.now() + timedelta(
        minutes=int(current_app.config.get("MIN_LEAD_TIME_MIN", 30))
    )

    available_dates: list[str] = []
    today = datetime.now().date()
    last_day = monthrange(today.year, today.month)[1]
    month_end = today.replace(day=last_day)
    total_days = (month_end - today).days

    for offset in range(total_days + 1):
        date_value = today + timedelta(days=offset)
        if work_days and str(date_value.weekday()) not in work_days:
            continue
        free_slots = _fetch_free_slots(
            master_id,
            date_value,
            min_start_at=min_start_at if date_value == today else None,
        )
        if _available_starts(free_slots, needed):
            available_dates.append(date_value.isoformat())
    return available_dates


@bp.get("/available-dates")
@login_required
def available_dates():
    master_id = request.args.get("master_id", type=int)
    if not master_id:
        return jsonify({"dates": []})
    return jsonify({"dates": _master_available_dates(master_id)})


@bp.route("/", methods=["GET", "POST"])
@login_required
def step1():
    _seed_car_makes_if_needed()
    masters = db.session.execute(
        db.select(Master).where(Master.is_active.is_(True)).order_by(Master.name.asc())
    ).scalars().all()
    works = db.session.execute(
        db.select(Work).where(Work.is_active.is_(True)).order_by(Work.title.asc())
    ).scalars().all()

    form = BookingStep1Form()
    form.master_id.choices = [(m.id, m.name) for m in masters]
    makes = db.session.execute(db.select(CarMake).order_by(CarMake.name.asc())).scalars().all()
    form.car_make_id.choices = [(m.id, m.name) for m in makes] + [(0, "Другая марка…")]

    if form.validate_on_submit():
        date_value = form.date.data
        # Проверка рабочих дней (0=Пн, 6=Вс в БД, weekday() в Python тоже 0=Пн, 6=Вс)
        settings = OrganizationSettings.get_settings()
        if settings.work_days is not None:
            work_days = settings.work_days.split(",")
            if str(date_value.weekday()) not in work_days:
                flash("Извините, в выбранный день мы не работаем. Пожалуйста, выберите другой день.", "warning")
                return render_template("booking/step1.html", form=form, today_str=datetime.now().date().isoformat())

        if date_value.isoformat() not in _master_available_dates(form.master_id.data):
            flash("На выбранную дату нет свободного времени у мастера. Пожалуйста, выберите другой день.", "warning")
            return render_template("booking/step1.html", form=form, today_str=datetime.now().date().isoformat())

        car_make_value = ""
        selected_make_id = int(form.car_make_id.data or 0)
        if selected_make_id > 0:
            make_obj = db.session.get(CarMake, selected_make_id)
            car_make_value = make_obj.name if make_obj else ""
        else:
            raw = form.car_make_custom.data or ""
            name = normalize_car_make(raw)
            key = car_make_key(name)
            if key:
                make_obj = db.session.execute(db.select(CarMake).where(CarMake.key == key)).scalar_one_or_none()
                if not make_obj:
                    make_obj = CarMake(name=name, key=key)
                    db.session.add(make_obj)
                    try:
                        db.session.commit()
                    except IntegrityError:
                        db.session.rollback()
                        make_obj = db.session.execute(db.select(CarMake).where(CarMake.key == key)).scalar_one_or_none()
                car_make_value = make_obj.name if make_obj else name
            else:
                car_make_value = raw.strip()

        return redirect(
            url_for(
                "booking.slots",
                master_id=form.master_id.data,
                date=form.date.data.isoformat(),
                car_make=car_make_value,
                car_model=form.car_model.data,
                car_year=form.car_year.data or "",
                car_number=form.car_number.data or "",
                win_number=normalize_win_number(form.win_number.data),
                problem_description=form.problem_description.data,
            )
        )

    return render_template("booking/step1.html", form=form, today_str=datetime.now().date().isoformat())


@bp.get("/slots")
@login_required
def slots():
    master_id = request.args.get("master_id", type=int)
    date_str = request.args.get("date", type=str)
    
    # Данные из step1
    car_make = request.args.get("car_make", "")
    car_model = request.args.get("car_model", "")
    car_year = request.args.get("car_year", "")
    car_number = request.args.get("car_number", "")
    win_number = request.args.get("win_number", "")
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
        total_duration,
        int(current_app.config.get("DEFAULT_DURATION_MIN", 60)),
        _slot_minutes(),
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
        win_number=win_number,
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
    win_number = request.form.get("win_number", "")
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
    needed = _slots_needed(total_duration, default_duration_min, _slot_minutes())

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
                win_number=win_number,
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
                win_number=win_number,
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
        win_number=normalize_win_number(win_number) or None,
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
                win_number=win_number,
                problem_description=problem_description,
            )
        )

    flash(f"Заявка создана: №{appointment.id}", "success")
    return redirect(url_for("cabinet.appointment_detail", appointment_id=appointment.id))
