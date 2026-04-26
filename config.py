from __future__ import annotations

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{(BASE_DIR / 'diagnost.sqlite3').as_posix()}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    DOCUMENTS_DIR = os.environ.get(
        "DOCUMENTS_DIR", str((BASE_DIR / "var" / "documents").resolve())
    )
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", str(25 * 1024 * 1024)))

    MIN_LEAD_TIME_MIN = int(os.environ.get("MIN_LEAD_TIME_MIN", "30"))
    CANCEL_BEFORE_HOURS = int(os.environ.get("CANCEL_BEFORE_HOURS", "4"))
    DEFAULT_DURATION_MIN = int(os.environ.get("DEFAULT_DURATION_MIN", "60"))

    TELEGRAM_TOKEN_TTL_MIN = int(os.environ.get("TELEGRAM_TOKEN_TTL_MIN", "10"))
    # Telegram-бот: токен от @BotFather, имя без @ (для ссылок t.me и sendMessage)
    TELEGRAM_BOT_TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    TELEGRAM_BOT_NAME = (os.environ.get("TELEGRAM_BOT_NAME") or "AutoDiagBot").strip().lstrip("@")
    # polling — фоновый getUpdates при старте приложения; webhook — только POST /telegram/webhook
    TELEGRAM_UPDATES_MODE = (os.environ.get("TELEGRAM_UPDATES_MODE") or "polling").strip().lower()

    # AI integration (admin-only helpers). If key is empty, AI features will be disabled.
    OPENAI_API_KEY = (os.environ.get("OPENAI_API_KEY") or "").strip()
    OPENAI_MODEL = (os.environ.get("OPENAI_MODEL") or "gpt-4o-mini").strip()
    OPENAI_BASE_URL = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip().rstrip("/")
