"""Инициализация БД (SQLite + SQLAlchemy 2.x)."""
from __future__ import annotations

import re

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models.entities import Base


def _to_sync_url(url: str) -> str:
    """Нам нужен синхронный SQLAlchemy поверх SQLite — драйвер aiosqlite не используем."""
    return re.sub(r"\+aiosqlite", "", url)


def init_db(settings: Settings) -> sessionmaker[Session]:
    sync_url = _to_sync_url(settings.database_url)
    engine = create_engine(sync_url, echo=False, future=True)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        # WAL лучше для конкурентного чтения/записи; нормальные таймауты блокировок
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    Base.metadata.create_all(engine)

    # Лёгкая миграция для существующих БД: create_all не добавляет колонки.
    with engine.begin() as conn:
        from sqlalchemy import inspect, text

        cols = {c["name"] for c in inspect(engine).get_columns("channel_states")}
        if "oldest_message_id" not in cols:
            conn.execute(
                text("ALTER TABLE channel_states ADD COLUMN oldest_message_id BIGINT")
            )
        mcols = {c["name"] for c in inspect(engine).get_columns("messages")}
        if "deleted_at" not in mcols:
            conn.execute(text("ALTER TABLE messages ADD COLUMN deleted_at DATETIME"))

    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
