"""Инициализация БД (PostgreSQL + SQLAlchemy 2.x)."""
from __future__ import annotations

import os
import re

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models.entities import Base

# Значение по умолчанию — PostgreSQL (SQLite больше не поддерживается).
DEFAULT_DATABASE_URL = "postgresql+psycopg://profiler:profiler@localhost:5432/profiling"


def default_database_url() -> str:
    """DATABASE_URL из окружения либо PG-дефолт (для CLI-скриптов)."""
    return os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL


def _to_sync_url(url: str) -> str:
    """Приводит URL к синхронному драйверу psycopg(3).

    asyncpg-вариант не используем: коллектор выполняет БД-операции
    в отдельных потоках (asyncio.to_thread), поэтому нужен sync-движок.
    """
    url = re.sub(r"\+asyncpg$", "+psycopg", url)
    if url.startswith("postgres://"):
        # устаревшая схема: SQLAlchemy требует postgresql://
        url = "postgresql://" + url[len("postgres://"):]
    return url


def require_postgres(url: str) -> str:
    """Fail-fast: SQLite/прочие диалекты больше не поддерживаются."""
    if not url.startswith(("postgresql://", "postgresql+", "postgres://")):
        raise SystemExit(
            f"[FATAL] DATABASE_URL должен быть PostgreSQL "
            f"(postgresql://... или postgresql+psycopg://...), получено: {url!r}. "
            f"SQLite больше не поддерживается."
        )
    return url


def create_db_engine(url: str):
    """Создать sync-engine для произвольного DATABASE_URL (используется CLI-скриптами)."""
    return create_engine(_to_sync_url(require_postgres(url)), echo=False, future=True, pool_pre_ping=True)


def init_db(settings: Settings) -> sessionmaker[Session]:
    sync_url = _to_sync_url(require_postgres(settings.database_url))
    engine = create_engine(
        sync_url,
        echo=False,
        future=True,
        # Пул соединений: коллектор пишет из нескольких потоков (to_thread),
        # админка читает параллельно — PG держит пул и переживает это спокойно.
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,   # отлов «убитых» сервером соединений до выдачи
        pool_recycle=1800,    # пересоздавать соединения раньше таймаута PG/LB
    )

    # Миграции намеренно отключены: база новая и схема создаётся целиком
    # из моделей (create_all). Модуль app.models.migrations сохранён для
    # будущих версий схемы — подключать вызов migrate()/stamp_baseline()
    # здесь, когда появятся данные, которые нужно доводить по шагам.
    Base.metadata.create_all(engine)

    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def inspect_tables(engine) -> list[str]:
    from sqlalchemy import inspect as sa_inspect

    return sa_inspect(engine).get_table_names()
