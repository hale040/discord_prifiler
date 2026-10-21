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
        # Ограничение размера WAL-файла (64 МБ): без него -wal разрастался
        # бесконечно при долгих чтениях админки на фоне пишущего коллектора.
        cursor.execute("PRAGMA journal_size_limit=67108864")
        cursor.close()

    fresh = not inspect_tables(engine)  # фиксируем ДО создания таблиц
    Base.metadata.create_all(engine)

    # Версионируемые миграции (app.models.migrations): доводят старые БД,
    # созданные ещё ad-hoc ALTER'ами, до актуальной схемы; новые БД просто
    # помечаются текущей версией. Любое изменение моделей требует записи
    # в список MIGRATIONS — см. модуль migrations.
    from app.models.migrations import LATEST_VERSION, migrate, stamp_baseline

    if fresh:
        stamp_baseline(engine)
    else:
        n = migrate(engine)
        if n:
            import logging

            logging.getLogger("app.db").info(
                "Схема БД обновлена: применено миграций: %d (версия v%s)",
                n,
                LATEST_VERSION,
            )

    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def inspect_tables(engine) -> list[str]:
    from sqlalchemy import inspect as sa_inspect

    return sa_inspect(engine).get_table_names()
