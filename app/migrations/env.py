"""Alembic-окружение discord_prifiler.

DATABASE_URL берётся из окружения (тот же источник, что и приложение);
синхронный драйвер psycopg(3) — коллектор и админка работают с sync-engine.

Запуск вручную:  alembic upgrade head
Запуск из кода: app.models.db.ensure_schema() -> alembic.command.upgrade()
"""
from __future__ import annotations

import os
import re
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Корень проекта должен быть в sys.path: env.py исполняется из каталога
# app/migrations/, а пакеты приложения лежат на уровень выше.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.models.entities import Base  # noqa: E402


def _sync_url(url: str | None) -> str:
    """Нормализация DATABASE_URL к sync-psycopg (как в app/models/db.py)."""
    url = url or "postgresql+psycopg://profiler:profiler@localhost:5682/profiling"
    url = re.sub(r"\+asyncpg$", "+psycopg", url)
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Единственный источник URL для миграций — переменная окружения.
config.set_main_option("sqlalchemy.url", _sync_url(os.getenv("DATABASE_URL")))

# target_metadata нужен только для autogenerate; ручные ревизии пишутся руками.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
