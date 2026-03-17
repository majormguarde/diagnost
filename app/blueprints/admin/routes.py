from functools import wraps
from datetime import datetime, time, timedelta
import os

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for, current_app, send_from_directory
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from ...extensions import db
from ...models import (
    Master, Work, WorkCategory, TimeSlot, Appointment, WorkOrder, 
    WorkOrderDocument, AppointmentSlot, OrganizationSettings, User, 
    Banner, Review, WorkOrderItem, WorkOrderPart, AppointmentItem,
    Competency, CashFlow
)
from ...utils import normalize_phone
from .forms import (
    MasterForm, WorkForm, CategoryForm, AppointmentForm, WorkOrderForm, 
    DocumentUploadForm, OrganizationSettingsForm, AdminCredentialsForm, 
    BannerForm, ReviewForm, WorkOrderItemForm, WorkOrderPartForm,
    AppointmentItemForm, CompetencyForm
)

bp = Blueprint("admin", __name__)


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if getattr(current_user, "role", None) != "admin":
            abort(403)
        return fn(*args, **kwargs)

    return wrapper


@bp.get("/")
@admin_required
def index():
    appointments_count = db.session.query(Appointment).count()
    active_masters_count = db.session.query(Master).filter_by(is_active=True).count()
    return render_template("admin/index.html", appointments_count=appointments_count, active_masters_count=active_masters_count)

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
    all_competencies = db.session.execute(db.select(Competency).order_by(Competency.title)).scalars().all()
    return render_template("admin/competencies/index.html", competencies=all_competencies)

@bp.route("/competencies/add", methods=["GET", "POST"])
@bp.route("/competencies/edit/<int:competency_id>", methods=["GET", "POST"])
@admin_required
def competency_edit(competency_id=None):
    competency = db.session.get(Competency, competency_id) if competency_id else Competency()
    form = CompetencyForm(obj=competency)
    if form.validate_on_submit():
        form.populate_obj(competency)
        if not competency_id:
            db.session.add(competency)
        db.session.commit()
        flash("Специализация сохранена", "success")
        return redirect(url_for("admin.competencies"))
    return render_template("admin/competencies/edit.html", form=form, competency=competency)

@bp.route("/competencies/delete/<int:competency_id>", methods=["POST"])
@admin_required
def competency_delete(competency_id):
    competency = db.session.get(Competency, competency_id)
    if not competency:
        abort(404)
    db.session.delete(competency)
    db.session.commit()
    flash("Специализация удалена", "info")
    return redirect(url_for("admin.competencies"))

# --- Услуги (Работы) ---

@bp.get("/works")
@admin_required
def works():
    all_works = db.session.execute(db.select(Work).order_by(Work.title)).scalars().all()
    return render_template("admin/works/index.html", works=all_works)

@bp.route("/works/add", methods=["GET", "POST"])
@bp.route("/works/edit/<int:work_id>", methods=["GET", "POST"])
@admin_required
def work_edit(work_id=None):
    work = db.session.get(Work, work_id) if work_id else Work()
    form = WorkForm(obj=work)
    
    categories = db.session.execute(db.select(WorkCategory).order_by(WorkCategory.title)).scalars().all()
    form.category_id.choices = [(c.id, c.title) for c in categories]
    
    if form.validate_on_submit():
        form.populate_obj(work)
        if not work_id:
            db.session.add(work)
        db.session.commit()
        flash("Специализация сохранена", "success")
        return redirect(url_for("admin.works"))
    
    return render_template("admin/works/edit.html", form=form, work=work)

# --- Категории ---

@bp.get("/categories")
@admin_required
def categories():
    all_cats = db.session.execute(db.select(WorkCategory).order_by(WorkCategory.title)).scalars().all()
    return render_template("admin/categories/index.html", categories=all_cats)

@bp.route("/categories/add", methods=["GET", "POST"])
@bp.route("/categories/edit/<int:cat_id>", methods=["GET", "POST"])
@admin_required
def category_edit(cat_id=None):
    cat = db.session.get(WorkCategory, cat_id) if cat_id else WorkCategory()
    form = CategoryForm(obj=cat)
    if form.validate_on_submit():
        form.populate_obj(cat)
        if not cat_id:
            db.session.add(cat)
        db.session.commit()
        flash("Категория сохранена", "success")
        return redirect(url_for("admin.categories"))
    return render_template("admin/categories/edit.html", form=form, category=cat)

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
    
    return render_template("admin/schedule/view.html", master=master, slots=slots)

@bp.route("/schedule/generate/<int:master_id>", methods=["POST"])
@admin_required
def schedule_generate(master_id):
    master = db.session.get(Master, master_id)
    if not master:
        abort(404)
        
    days = int(request.form.get("days", 7))
    start_time_str = request.form.get("start_time", "09:00")
    end_time_str = request.form.get("end_time", "18:00")
    
    start_time = time.fromisoformat(start_time_str)
    end_time = time.fromisoformat(end_time_str)
    
    today = datetime.now().date()
    count = 0
    
    for i in range(days):
        day = today + timedelta(days=i)
        current_dt = datetime.combine(day, start_time)
        day_end_dt = datetime.combine(day, end_time)
        
        while current_dt + timedelta(minutes=30) <= day_end_dt:
            slot_start = current_dt
            slot_end = current_dt + timedelta(minutes=30)
            
            # Проверяем, есть ли уже такой слот
            exists = db.session.execute(
                db.select(TimeSlot)
                .where(TimeSlot.master_id == master_id, TimeSlot.start_at == slot_start)
            ).scalar()
            
            if not exists:
                new_slot = TimeSlot(
                    master_id=master_id,
                    start_at=slot_start,
                    end_at=slot_end,
                    status="free"
                )
                db.session.add(new_slot)
                count += 1
            
            current_dt = slot_end
            
    db.session.commit()
    flash(f"Сгенерировано слотов: {count}", "success")
    return redirect(url_for("admin.schedule", master_id=master_id))

# --- Заявки (Requests) ---

@bp.get("/appointments")
@admin_required
def appointments():
    all_appointments = db.session.execute(
        db.select(Appointment).order_by(Appointment.created_at.desc())
    ).scalars().all()
    return render_template("admin/appointments/index.html", appointments=all_appointments)

@bp.route("/appointments/<int:appointment_id>", methods=["GET", "POST"])
@admin_required
def appointment_detail(appointment_id):
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment:
        abort(404)
        
    form = AppointmentForm(obj=appointment)
    item_form = AppointmentItemForm()
    
    # Загружаем список мастеров
    masters = db.session.execute(db.select(Master).where(Master.is_active == True)).scalars().all()
    form.master_id.choices = [(m.id, m.name) for m in masters]
    
    # Загружаем список специализаций для добавления
    works_list = db.session.execute(db.select(Work).where(Work.is_active == True).order_by(Work.title)).scalars().all()
    item_form.work_id.choices = [(w.id, f"{w.title} ({w.base_price} руб.)") for w in works_list]
    
    if form.validate_on_submit() and 'submit' in request.form:
        # Обновляем поля
        appointment.master_id = form.master_id.data
        appointment.start_at = form.start_at.data
        # Если изменилось время начала, обновим и время окончания (по умолчанию +60 мин)
        appointment.end_at = appointment.start_at + timedelta(minutes=60)
        appointment.status = form.status.data
        appointment.car_make = form.car_make.data
        appointment.car_model = form.car_model.data
        appointment.car_year = form.car_year.data
        appointment.car_number = form.car_number.data
        appointment.problem_description = form.problem_description.data
        
        db.session.commit()
        flash("Заявка успешно обновлена", "success")
        return redirect(url_for("admin.appointment_detail", appointment_id=appointment.id))
        
    return render_template("admin/appointments/detail.html", appointment=appointment, form=form, item_form=item_form)

@bp.post("/appointments/<int:appointment_id>/items/add")
@admin_required
def appointment_item_add(appointment_id):
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment:
        abort(404)
        
    item_form = AppointmentItemForm()
    works_list = db.session.execute(db.select(Work).where(Work.is_active == True)).scalars().all()
    item_form.work_id.choices = [(w.id, w.title) for w in works_list]
    
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
        total_amount=sum(item.price_snapshot or 0 for item in appointment.items)
    )
    db.session.add(order)
    
    # Переносим услуги из заявки в заказ-наряд
    for item in appointment.items:
        order_item = WorkOrderItem(
            work_order=order,
            title=item.work.title,
            duration=item.duration_snapshot or 0,
            price=item.price_snapshot or 0
        )
        db.session.add(order_item)
        
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
    
    # Заполняем список мастеров для выбора исполнителя работы
    masters_list = db.session.execute(db.select(Master).where(Master.is_active == True).order_by(Master.name)).scalars().all()
    item_form.master_id.choices = [(0, "-- Основной мастер --")] + [(m.id, m.name) for m in masters_list]
    
    part_form = WorkOrderPartForm()
    
    if form.validate_on_submit():
        order.status = form.status.data
        order.total_amount = form.total_amount.data
        order.inspection_results = form.inspection_results.data
        if order.status == "closed":
            order.closed_at = datetime.utcnow()
            order.is_paid = True # Автоматически считаем оплаченным при закрытии
            
            # Автоматическая запись в книгу приходов при закрытии заказ-наряда
            # Проверяем, нет ли уже записи для этого заказа
            existing_cash = db.session.execute(
                db.select(CashFlow).where(CashFlow.work_order_id == order.id, CashFlow.amount > 0)
            ).scalar()
            
            if not existing_cash:
                cash = CashFlow(
                    amount=order.total_amount or 0,
                    category="Оплата услуг",
                    description=f"Оплата заказ-наряда #{order.id}",
                    work_order_id=order.id
                )
                db.session.add(cash)
                
        db.session.commit()
        flash("Заказ-наряд обновлен", "success")
        return redirect(url_for("admin.work_order_detail", order_id=order.id))
        
    return render_template(
        "admin/work_orders/detail.html", 
        order=order, 
        form=form, 
        doc_form=doc_form,
        item_form=item_form,
        part_form=part_form
    )

@bp.post("/work-orders/<int:order_id>/pay")
@admin_required
def work_order_pay(order_id):
    order = db.session.get(WorkOrder, order_id)
    if not order:
        abort(404)
        
    if order.is_paid:
        flash("Заказ-наряд уже оплачен", "info")
        return redirect(url_for("admin.work_order_detail", order_id=order.id))
        
    order.is_paid = True
    
    # Создаем запись в CashFlow если её нет
    existing_cash = db.session.execute(
        db.select(CashFlow).where(CashFlow.work_order_id == order.id, CashFlow.amount > 0)
    ).scalar()
    
    if not existing_cash:
        cash = CashFlow(
            amount=order.total_amount or 0,
            category="Оплата услуг",
            description=f"Оплата заказ-наряда #{order.id}",
            work_order_id=order.id
        )
        db.session.add(cash)
    
    db.session.commit()
    flash("Оплата зафиксирована", "success")
    return redirect(url_for("admin.work_order_detail", order_id=order.id))

@bp.get("/work-orders/<int:order_id>/print")
@admin_required
def work_order_print(order_id):
    order = db.session.get(WorkOrder, order_id)
    if not order:
        abort(404)
        
    settings = OrganizationSettings.get_settings()
    return render_template("admin/work_orders/print.html", order=order, settings=settings)

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
    
    form = WorkOrderPartForm()
    if form.validate_on_submit():
        part = WorkOrderPart(
            work_order=order,
            title=form.title.data,
            quantity=form.quantity.data,
            unit=form.unit.data,
            price=form.price.data
        )
        db.session.add(part)
        
        # Автоматический пересчет суммы
        total_part_price = int(part.price * part.quantity)
        if order.total_amount is None:
            order.total_amount = 0
        order.total_amount += total_part_price
        
        db.session.commit()
        flash("Запчасть добавлена", "success")
    
    return redirect(url_for("admin.work_order_detail", order_id=order.id))

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
    
    # Получаем все использованные материалы за период
    materials = db.session.execute(
        db.select(WorkOrderPart)
        .join(WorkOrder)
        .where(WorkOrder.created_at >= start_date)
        .where(WorkOrder.created_at <= end_date)
        .order_by(WorkOrder.created_at.desc())
    ).scalars().all()
    
    # Группируем материалы по наименованию
    materials_by_title = {}
    total_cost = 0
    
    for material in materials:
        if material.title not in materials_by_title:
            materials_by_title[material.title] = {
                'quantity': 0,
                'unit': material.unit or 'шт.',
                'total_cost': 0,
                'items': []
            }
        
        material_cost = material.quantity * material.price
        materials_by_title[material.title]['quantity'] += material.quantity
        materials_by_title[material.title]['total_cost'] += material_cost
        materials_by_title[material.title]['items'].append(material)
        total_cost += material_cost
    
    return render_template(
        "admin/materials_report.html", 
        materials_by_title=materials_by_title,
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
            .where(WorkOrder.created_at >= start_date)
            .where(WorkOrder.created_at <= end_date)
        ).scalars().all()
        order_items = db.session.execute(
            db.select(WorkOrderItem)
            .join(WorkOrder)
            .where(WorkOrder.master_id == master.id)
            .where(WorkOrderItem.master_id == None)
            .where(WorkOrderItem.is_done == True)
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
    materials = db.session.execute(
        db.select(WorkOrderPart)
        .join(WorkOrder)
        .where(WorkOrder.created_at >= start_date)
        .where(WorkOrder.created_at <= end_date)
        .order_by(WorkOrder.created_at.desc())
    ).scalars().all()
    groups = {}
    total_cost = 0
    for material in materials:
        if material.title not in groups:
            groups[material.title] = {
                "title": material.title,
                "quantity": 0,
                "unit": material.unit or "шт.",
                "total_cost": 0,
                "items": []
            }
        material_cost = material.quantity * material.price
        groups[material.title]["quantity"] += material.quantity
        groups[material.title]["total_cost"] += material_cost
        groups[material.title]["items"].append(material)
        total_cost += material_cost
    materials_groups = sorted(groups.values(), key=lambda x: x["title"].lower())
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
