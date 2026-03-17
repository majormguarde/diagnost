from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ...extensions import db
from ...models import User
from ...utils import normalize_phone
from .forms import LoginForm, RegisterForm


bp = Blueprint("auth", __name__)


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("cabinet.index"))

    form = RegisterForm()
    if form.validate_on_submit():
        phone = normalize_phone(form.phone.data)
        if not phone:
            flash("Некорректный телефон", "danger")
            return render_template("auth/register.html", form=form)

        existing = db.session.execute(db.select(User).where(User.phone == phone)).scalar_one_or_none()
        if existing:
            flash("Пользователь с таким телефоном уже существует", "warning")
            return render_template("auth/register.html", form=form)

        user = User(phone=phone, name=form.name.data.strip(), role="client", is_active=True)
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        flash("Регистрация выполнена", "success")
        return redirect(url_for("cabinet.index"))

    return render_template("auth/register.html", form=form)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("cabinet.index"))

    form = LoginForm()
    if form.validate_on_submit():
        phone = normalize_phone(form.phone.data)
        user = None
        if phone:
            user = db.session.execute(db.select(User).where(User.phone == phone)).scalar_one_or_none()

        if not user or not user.is_active or not user.check_password(form.password.data):
            flash("Неверный телефон или пароль", "danger")
            return render_template("auth/login.html", form=form)

        login_user(user)
        next_url = request.args.get("next")
        flash("Вы вошли в систему", "success")
        return redirect(next_url or url_for("cabinet.index"))

    return render_template("auth/login.html", form=form)


@bp.get("/logout")
@login_required
def logout():
    logout_user()
    flash("Вы вышли из системы", "success")
    return redirect(url_for("public.index"))
