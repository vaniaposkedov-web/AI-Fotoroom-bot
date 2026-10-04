from __future__ import annotations

from bot.db import Database
from bot.config import Settings

_db: Database | None = None
_settings: Settings | None = None


def set_context(db: Database, settings: Settings) -> None:
    global _db, _settings
    _db = db
    _settings = settings


def get_db() -> Database:
    if _db is None:
        raise RuntimeError("VK context DB is not initialized")
    return _db


def get_settings() -> Settings:
    if _settings is None:
        raise RuntimeError("VK context settings are not initialized")
    return _settings
