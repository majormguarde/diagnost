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
