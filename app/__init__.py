from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from flask import Flask

from .commands import register_commands
from .extensions import csrf, db, login_manager
from .models import User


def create_app() -> Flask:
    load_dotenv()

    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.from_object("config.Config")

    Path(app.config["DOCUMENTS_DIR"]).mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

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
        return dict(org_settings=OrganizationSettings.get_settings())

    register_commands(app)

    return app
