"""Инициализация БД (PostgreSQL + SQLAlchemy 2.x)."""
from __future__ import annotations

import logging
import os
import re

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models.entities import Base, UserProfile

log = logging.getLogger(__name__)

# Значение по умолчанию — PostgreSQL (SQLite больше не поддерживается).
DEFAULT_DATABASE_URL = "postgresql+psycopg://profiler:profiler@localhost:5682/profiling"


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


def ensure_schema(engine) -> None:
    """Довести схему существующей базы до LATEST_VERSION.

    Base.metadata.create_all создаёт только отсутствующие ТАБЛИЦЫ и никогда
    не добавляет новые КОЛОНКИ в уже существующие таблицы. Без этого шага
    база, созданная до v10/v11, падает на первом же запросе к новым полям
    (psycopg.errors.UndefinedColumn: column user_profiles.dm_fetch does not
    exist). Миграционный модуль идемпотентен: сам читает версию схемы из
    таблицы schema_migrations и применяет только недостающие шаги.
    """
    from app.models.migrations import (
        LATEST_VERSION,
        _columns,
        _table_exists,
        get_current_version,
        migrate,
        run_migrations,
        stamp_baseline,
    )

    try:
        # Сначала — таблицы из актуальных моделей (идемпотентно, checkfirst).
        # Без этого шага ensure_schema на пустой базе стемпил бы версию и
        # не создал НИ ОДНОЙ таблицы (баг fresh-БД: stamp_baseline без
        # create_all). В существующей БД create_all — дешёвый no-op.
        Base.metadata.create_all(engine)

        # Порядок важен: migrate() сам читает версию схемы. Если таблицу версий
        # стемпить LATEST до проверки (stamp_baseline это делает безусловно),
        # база, созданная до v10/v11, будет считаться актуальной и недостающие
        # шаги никогда не применятся — отсюда UndefinedColumn на dm_fetch.
        version = get_current_version(engine)
        if version > 0:
            migrate(engine)     # старая БД: ALTER'ом добавить новые таблицы/колонки
        elif _table_exists(engine, "messages"):
            # Stray-БД: таблицы есть, но версии нулевые (миграции включены
            # позже create_all). run_migrations сам определяет физическое
            # состояние схемы и применит недостающие шаги (v10/v11).
            run_migrations(engine)
        else:
            stamp_baseline(engine)  # fresh-БД: создать таблицы версий + v9-нормы
        # ГАРАНТИЯ ФИЗИЧЕСКОЙ ЦЕЛОСТНОСТИ (независимо от таблицы версий):
        # версия в schema_migrations может быть ложной (например, stamp_baseline
        # выполнился частично, или строка версии осталась от запуска, который
        # упал до ALTER'ов). Поэтому сверяем РЕАЛЬНУЮ схему с моделями и при
        # расхождении доливаем недостающее напрямую идемпотентным DDL.
        # Это исключает psycopg.errors.UndefinedColumn: user_profiles.dm_fetch.
        if _table_exists(engine, "user_profiles"):
            missing_dm = [
                c.name for c in UserProfile.__table__.columns
                if c.name.startswith("dm_")
                and c.name not in _columns(engine, "user_profiles")
            ]
            if missing_dm:
                log.warning(
                    "ensure_schema: в живой БД отсутствуют колонки %s — "
                    "принудительный прогон run_migrations (доведёт схему до v%s).",
                    ", ".join(missing_dm), LATEST_VERSION,
                )
                run_migrations(engine)
                still = [c for c in missing_dm
                         if c not in _columns(engine, "user_profiles")]
                if still:
                    # Страховка последнего рубежа: тот же DDL, что в шаге v11,
                    # но точечно по недостающим колонкам. Иначе коллектор и
                    # админка падают 500/UndefinedColumn на каждом запросе.
                    from app.models.migrations import _v11_dm_collection

                    log.warning(
                        "ensure_schema: run_migrations не долил %s — прямой DDL.",
                        ", ".join(still),
                    )
                    _v11_dm_collection(engine)
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).error(
            "Миграции схемы не прошли (%s). Если используются новые поля — "
            "примените вручную: python -m app.models.migrations \"$DATABASE_URL\"",
            exc,
        )
        raise


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

    # create_all создаёт только ОТСУТСТВУЮЩИЕ таблицы — новые колонки в
    # уже существующих таблицах он НЕ добавляет. Поэтому после него
    # обязательно прогоняем миграционный модуль: он идемпотентен, сам
    # определит версию схемы и ALTER'ом доведёт базу до LATEST_VERSION
    # (например, v11 dm_* колонки в user_profiles на базе, созданной до v11).
    Base.metadata.create_all(engine)

    ensure_schema(engine)

    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def inspect_tables(engine) -> list[str]:
    from sqlalchemy import inspect as sa_inspect

    return sa_inspect(engine).get_table_names()
