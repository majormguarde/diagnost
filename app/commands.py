from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path

import click
from flask import current_app
from werkzeug.security import generate_password_hash

from .extensions import db
from .models import Master, TimeSlot, User, Work, WorkCategory, OrganizationSettings
from .utils import normalize_phone


def register_commands(app) -> None:
    @app.cli.command("init-db")
    def init_db() -> None:
        Path(current_app.config["DOCUMENTS_DIR"]).mkdir(parents=True, exist_ok=True)
        db.create_all()
        click.echo("DB initialized")

    @app.cli.command("create-admin")
    @click.option("--phone", required=True)
    @click.option("--name", required=True)
    @click.option("--password", required=True)
    def create_admin(phone: str, name: str, password: str) -> None:
        normalized = normalize_phone(phone)
        if not normalized:
            raise click.ClickException("Invalid phone")

        existing = db.session.execute(
            db.select(User).where(User.phone == normalized)
        ).scalar_one_or_none()
        if existing:
            raise click.ClickException("User with this phone already exists")

        user = User(
            role="admin",
            phone=normalized,
            name=name.strip(),
            password_hash=generate_password_hash(password),
            is_active=True,
            created_at=datetime.utcnow(),
        )
        db.session.add(user)
        db.session.commit()
        click.echo(f"Admin created: {normalized}")

    @app.cli.command("reset-admin")
    def reset_admin() -> None:
        db.create_all()

        admin = db.session.execute(
            db.select(User).where(User.role == "admin").order_by(User.id.asc())
        ).scalar_one_or_none()

        if not admin:
            start = 79990000000
            phone = ""
            for i in range(0, 1000):
                candidate = normalize_phone(f"+{start + i}")
                if not candidate:
                    continue
                exists = db.session.execute(
                    db.select(User.id).where(User.phone == candidate)
                ).scalar_one_or_none()
                if not exists:
                    phone = candidate
                    break
            if not phone:
                raise click.ClickException("Не удалось подобрать свободный телефон для admin-пользователя.")

            admin = User(
                role="admin",
                phone=phone,
                name="admin",
                password_hash=generate_password_hash("admin123"),
                is_active=True,
                created_at=datetime.utcnow(),
            )
            db.session.add(admin)
        else:
            admin.name = "admin"
            admin.is_active = True
            admin.password_hash = generate_password_hash("admin123")

        db.session.commit()
        click.echo("Admin reset: admin/admin123")

    @app.cli.command("seed-demo")
    @click.option("--days", default=7, show_default=True, type=int)
    @click.option("--start-hour", default=10, show_default=True, type=int)
    @click.option("--end-hour", default=18, show_default=True, type=int)
    @click.option("--slot-minutes", default=30, show_default=True, type=int)
    def seed_demo(days: int, start_hour: int, end_hour: int, slot_minutes: int) -> None:
        if days < 1 or days > 60:
            raise click.ClickException("days must be between 1 and 60")
        if slot_minutes not in (15, 20, 30, 60):
            raise click.ClickException("slot-minutes must be one of 15, 20, 30, 60")

        category = db.session.execute(
            db.select(WorkCategory).where(WorkCategory.title == "Диагностика")
        ).scalar_one_or_none()
        if not category:
            category = WorkCategory(title="Диагностика")
            db.session.add(category)
            db.session.flush()

        works_to_ensure = [
            ("Компьютерная диагностика", 60, 1500),
            ("Проверка АКБ/генератора", 30, 800),
            ("Поиск утечки тока", 60, 2000),
        ]
        for title, duration_min, base_price in works_to_ensure:
            existing = db.session.execute(
                db.select(Work).where(Work.title == title)
            ).scalar_one_or_none()
            if not existing:
                db.session.add(
                    Work(
                        category_id=category.id,
                        title=title,
                        duration_min=duration_min,
                        base_price=base_price,
                        is_active=True,
                    )
                )

        master = db.session.execute(
            db.select(Master).where(Master.name == "Мастер 1")
        ).scalar_one_or_none()
        if not master:
            master = Master(name="Мастер 1", is_active=True)
            db.session.add(master)
            db.session.flush()

        start_t = time(hour=start_hour, minute=0)
        end_t = time(hour=end_hour, minute=0)

        created = 0
        now = datetime.now()
        for day_offset in range(days):
            d = (now + timedelta(days=day_offset)).date()
            cursor = datetime.combine(d, start_t)
            end_dt = datetime.combine(d, end_t)
            while cursor < end_dt:
                next_dt = cursor + timedelta(minutes=slot_minutes)
                if next_dt > end_dt:
                    break
                exists = db.session.execute(
                    db.select(TimeSlot.id).where(
                        TimeSlot.master_id == master.id, TimeSlot.start_at == cursor
                    )
                ).scalar_one_or_none()
                if not exists:
                    db.session.add(
                        TimeSlot(
                            master_id=master.id,
                            start_at=cursor,
                            end_at=next_dt,
                            status="free",
                        )
                    )
                    created += 1
                cursor = next_dt

        db.session.commit()
        click.echo(f"Seeded demo data. Slots created: {created}")

    @app.cli.command("ensure-schema")
    def ensure_schema() -> None:
        """Добавляет недостающие колонки в SQLite (без отдельных миграций)."""
        from sqlalchemy import inspect, text

        engine = db.engine
        if engine.dialect.name != "sqlite":
            click.echo("ensure-schema: только для SQLite, пропуск.")
            return
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("appointments")}
        if "win_number" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE appointments ADD COLUMN win_number VARCHAR(32)"))
            click.echo("Добавлена колонка appointments.win_number")
        else:
            click.echo("Схема актуальна (appointments.win_number).")
