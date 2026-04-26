from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from flask import Flask
from sqlalchemy import text

from .commands import register_commands
from .extensions import csrf, db, login_manager
from .models import User


def _ensure_runtime_schema() -> None:
    # На новом окружении таблиц может ещё не быть: сначала создаём базовую схему,
    # затем ниже аккуратно добавляем недостающие колонки для старых SQLite-баз.
    db.create_all()

    rows = db.session.execute(text("PRAGMA table_info(organization_settings)")).mappings().all()
    columns = {r["name"] for r in rows}
    if "slot_minutes" not in columns:
        db.session.execute(text("ALTER TABLE organization_settings ADD COLUMN slot_minutes INTEGER DEFAULT 60"))
        db.session.commit()

    category_rows = db.session.execute(text("PRAGMA table_info(work_categories)")).mappings().all()
    category_columns = {r["name"] for r in category_rows}
    if "competency_id" not in category_columns:
        db.session.execute(text("ALTER TABLE work_categories ADD COLUMN competency_id INTEGER"))
        db.session.commit()
    if "sort_order" not in category_columns:
        db.session.execute(text("ALTER TABLE work_categories ADD COLUMN sort_order INTEGER DEFAULT 0"))
        db.session.commit()

    competency_rows = db.session.execute(text("PRAGMA table_info(competencies)")).mappings().all()
    competency_columns = {r["name"] for r in competency_rows}
    if "sort_order" not in competency_columns:
        db.session.execute(text("ALTER TABLE competencies ADD COLUMN sort_order INTEGER DEFAULT 0"))
        db.session.commit()

    work_rows = db.session.execute(text("PRAGMA table_info(works)")).mappings().all()
    work_columns = {r["name"] for r in work_rows}
    if "sort_order" not in work_columns:
        db.session.execute(text("ALTER TABLE works ADD COLUMN sort_order INTEGER DEFAULT 0"))
        db.session.commit()

    appointment_item_rows = db.session.execute(text("PRAGMA table_info(appointment_items)")).mappings().all()
    appointment_item_columns = {r["name"] for r in appointment_item_rows}
    if "k1" not in appointment_item_columns:
        db.session.execute(text("ALTER TABLE appointment_items ADD COLUMN k1 REAL DEFAULT 1.0"))
        db.session.commit()
    if "k2" not in appointment_item_columns:
        db.session.execute(text("ALTER TABLE appointment_items ADD COLUMN k2 REAL DEFAULT 1.0"))
        db.session.commit()
    if "extra" not in appointment_item_columns:
        db.session.execute(text("ALTER TABLE appointment_items ADD COLUMN extra VARCHAR(255)"))
        db.session.commit()
    if "declined_by_client" not in appointment_item_columns:
        db.session.execute(text("ALTER TABLE appointment_items ADD COLUMN declined_by_client INTEGER DEFAULT 0"))
        db.session.commit()

    appt_rows = db.session.execute(text("PRAGMA table_info(appointments)")).mappings().all()
    appt_columns = {r["name"] for r in appt_rows}
    for col, ddl in (
        ("engine_type", "VARCHAR(16)"),
        ("has_turbo", "INTEGER"),
        ("engine_volume_l", "REAL"),
        ("transmission_type", "VARCHAR(24)"),
        ("mileage_km", "INTEGER"),
    ):
        if col not in appt_columns:
            db.session.execute(text(f"ALTER TABLE appointments ADD COLUMN {col} {ddl}"))
            db.session.commit()

    user_rows = db.session.execute(text("PRAGMA table_info(users)")).mappings().all()
    user_columns = {r["name"] for r in user_rows}
    for col, ddl in (
        ("client_whatsapp", "VARCHAR(32)"),
        ("client_telegram", "VARCHAR(64)"),
        ("client_email", "VARCHAR(120)"),
    ):
        if col not in user_columns:
            db.session.execute(text(f"ALTER TABLE users ADD COLUMN {col} {ddl}"))
            db.session.commit()

    org_rows = db.session.execute(text("PRAGMA table_info(organization_settings)")).mappings().all()
    org_columns = {r["name"] for r in org_rows}
    for col, ddl in (
        ("org_whatsapp", "VARCHAR(32)"),
        ("org_telegram", "VARCHAR(64)"),
        ("smtp_host", "VARCHAR(120)"),
        ("smtp_port", "INTEGER"),
        ("smtp_user", "VARCHAR(120)"),
        ("smtp_password", "VARCHAR(255)"),
        ("smtp_use_tls", "INTEGER DEFAULT 1"),
        ("smtp_from", "VARCHAR(120)"),
        ("telegram_bot_username", "VARCHAR(64)"),
        ("telegram_bot_token", "VARCHAR(255)"),
        ("site_public_url", "VARCHAR(255)"),
        ("sbp_phone", "VARCHAR(32)"),
        ("ai_provider", "VARCHAR(32)"),
        ("ai_base_url", "VARCHAR(255)"),
        ("ai_api_key", "VARCHAR(255)"),
        ("ai_model", "VARCHAR(120)"),
        ("ai_site_url", "VARCHAR(255)"),
        ("ai_app_name", "VARCHAR(120)"),
        ("ai_default_prompt_template_id_appt", "INTEGER"),
        ("ai_default_prompt_template_id_wo", "INTEGER"),
    ):
        if col not in org_columns:
            db.session.execute(text(f"ALTER TABLE organization_settings ADD COLUMN {col} {ddl}"))
            db.session.commit()

    # Создаем таблицы для деталей и материалов, если их нет
    db.session.execute(text("""
        CREATE TABLE IF NOT EXISTS work_order_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            work_order_id INTEGER NOT NULL,
            title VARCHAR(255) NOT NULL,
            quantity REAL NOT NULL DEFAULT 1.0,
            unit VARCHAR(20),
            price INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (work_order_id) REFERENCES work_orders(id)
        )
    """))
    db.session.execute(text("""
        CREATE TABLE IF NOT EXISTS work_order_materials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            work_order_id INTEGER NOT NULL,
            title VARCHAR(255) NOT NULL,
            quantity REAL NOT NULL DEFAULT 1.0,
            unit VARCHAR(20),
            price INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (work_order_id) REFERENCES work_orders(id)
        )
    """))
    db.session.commit()

    db.session.execute(
        text("""
        CREATE TABLE IF NOT EXISTS work_order_telegram_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            work_order_id INTEGER NOT NULL,
            code VARCHAR(24) NOT NULL UNIQUE,
            expires_at DATETIME NOT NULL,
            used_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (work_order_id) REFERENCES work_orders(id)
        )
    """)
    )
    db.session.commit()

    # Добавляем колонку complaint_description в work_orders, если её нет
    work_orders_rows = db.session.execute(text("PRAGMA table_info(work_orders)")).mappings().all()
    work_orders_columns = {r["name"] for r in work_orders_rows}
    if "complaint_description" not in work_orders_columns:
        db.session.execute(text("ALTER TABLE work_orders ADD COLUMN complaint_description TEXT"))
        db.session.commit()

    # Создаем таблицу для жалоб клиента, если её нет
    db.session.execute(text("""
        CREATE TABLE IF NOT EXISTS work_order_complaint_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            work_order_id INTEGER NOT NULL,
            description TEXT NOT NULL,
            is_done INTEGER NOT NULL DEFAULT 0,
            is_refused INTEGER NOT NULL DEFAULT 0,
            refusal_reason TEXT,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (work_order_id) REFERENCES work_orders(id)
        )
    """))
    db.session.commit()

    # Документы к заявкам (в т.ч. ответы ИИ в Markdown)
    db.session.execute(
        text(
            """
        CREATE TABLE IF NOT EXISTS appointment_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            appointment_id INTEGER NOT NULL,
            filename VARCHAR(255) NOT NULL,
            mime VARCHAR(100) NOT NULL,
            storage_path VARCHAR(500) NOT NULL,
            size_bytes INTEGER NOT NULL,
            uploaded_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (appointment_id) REFERENCES appointments(id)
        )
    """
        )
    )
    db.session.commit()

    # Шаблоны промптов для ИИ (Markdown)
    db.session.execute(
        text(
            """
        CREATE TABLE IF NOT EXISTS ai_prompt_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title VARCHAR(160) NOT NULL,
            body_md TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """
        )
    )
    db.session.commit()

    db.session.execute(
        text(
            """
        CREATE TABLE IF NOT EXISTS ai_request_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            appointment_id INTEGER,
            work_order_id INTEGER,
            template_id INTEGER,
            model VARCHAR(120),
            prompt_md TEXT,
            messages_json TEXT,
            answer_text TEXT,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (appointment_id) REFERENCES appointments(id),
            FOREIGN KEY (work_order_id) REFERENCES work_orders(id),
            FOREIGN KEY (template_id) REFERENCES ai_prompt_templates(id)
        )
    """
        )
    )
    db.session.commit()

    db.session.execute(
        text(
            """
        CREATE TABLE IF NOT EXISTS appointment_ai_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            appointment_id INTEGER NOT NULL,
            ai_request_log_id INTEGER,
            question TEXT NOT NULL,
            options_json TEXT NOT NULL DEFAULT '[]',
            client_answer TEXT,
            answered_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (appointment_id) REFERENCES appointments(id),
            FOREIGN KEY (ai_request_log_id) REFERENCES ai_request_logs(id)
        )
    """
        )
    )
    db.session.commit()

    # Справочник моделей ИИ (для комбобокса выбора актуальной модели)
    db.session.execute(
        text(
            """
        CREATE TABLE IF NOT EXISTS ai_models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title VARCHAR(160) NOT NULL,
            model_id VARCHAR(120) NOT NULL UNIQUE,
            context TEXT,
            price_in_per_1m REAL,
            price_out_per_1m REAL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """
        )
    )
    db.session.commit()

    ai_models_rows = db.session.execute(text("PRAGMA table_info(ai_models)")).mappings().all()
    ai_models_columns = {r["name"] for r in ai_models_rows}
    for col, ddl in (
        ("context", "TEXT"),
        ("price_in_per_1m", "REAL"),
        ("price_out_per_1m", "REAL"),
    ):
        if col not in ai_models_columns:
            db.session.execute(text(f"ALTER TABLE ai_models ADD COLUMN {col} {ddl}"))
            db.session.commit()


def create_app() -> Flask:
    load_dotenv()

    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.from_object("config.Config")

    Path(app.config["DOCUMENTS_DIR"]).mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    with app.app_context():
        _ensure_runtime_schema()

    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "warning"

    @login_manager.user_loader
    def load_user(user_id: str):
        if not user_id:
            return None
        return db.session.get(User, int(user_id))

    from .blueprints.admin.routes import bp as admin_bp
    from .blueprints.auth.routes import bp as auth_bp
    from .blueprints.booking.routes import bp as booking_bp
    from .blueprints.cabinet.routes import bp as cabinet_bp
    from .blueprints.public.routes import bp as public_bp
    from .blueprints.telegram.routes import bp as telegram_bp
    from .models import OrganizationSettings

    app.register_blueprint(public_bp)
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(booking_bp, url_prefix="/booking")
    app.register_blueprint(cabinet_bp, url_prefix="/cabinet")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(telegram_bp, url_prefix="/telegram")

    @app.context_processor
    def inject_settings():
        from .telegram_bot import get_telegram_bot_username

        org_settings = OrganizationSettings.get_settings()
        return dict(
            org_settings=org_settings,
            telegram_bot_name=get_telegram_bot_username(),
        )

    register_commands(app)

    from .telegram_poller import start_telegram_poller

    start_telegram_poller(app)

    return app
