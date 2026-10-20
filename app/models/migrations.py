"""Версионируемые миграции схемы БД поверх SQLAlchemy (без внешних зависимостей).

Механизм:
- Таблица `schema_version` хранит одну строку с текущей версией схемы.
- Список MIGRATIONS упорядочен; индекс i -> миграция переходит из версии i в i+1.
- Каждая миграция идемпотентна (проверяет существование колонок/индексов),
  поэтому безопасна и на новых БД, и на старых файлах .db.
- Запуск: автоматически при init_db() либо вручную `python -m app.models.migrations [database_url]`.

Правило для разработчиков: ЛЮБОЕ изменение модели (новая колонка/таблица/индекс)
обязательно сопровождается записью в конец списка MIGRATIONS — номер = версия,
в которую это изменение превращает схему.
"""
from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import Engine, inspect, text

log = logging.getLogger("app.migrations")

MIGRATION_TABLE = "schema_version"


def _table_exists(engine: Engine, name: str) -> bool:
    return name in set(inspect(engine).get_table_names())


def _columns(engine: Engine, table: str) -> set[str]:
    if not _table_exists(engine, table):
        return set()
    return {c["name"] for c in inspect(engine).get_columns(table)}


def _indexes(engine: Engine, table: str) -> set[str]:
    if not _table_exists(engine, table):
        return set()
    return {i["name"] for i in inspect(engine).get_indexes(table)}


# --------------------------------------------------------------------------- #
# Собственно миграции. Версия N = длина списка (все применены).               #
# --------------------------------------------------------------------------- #

def _v01_baseline(engine: Engine) -> None:
    """v1: базовые поля связей ответов и мягкого удаления сообщений.

    Покрывает исторические ad-hoc ALTER'ы (deleted_at, reply_to_author_id,
    reference_id + индекс, oldest_message_id в channel_states): на новых БД
    их уже создал create_all, проверка «если нет — добавить» делает шаг
    идемпотентным.
    """
    with engine.begin() as conn:
        mcols = _columns(engine, "messages")
        if "deleted_at" not in mcols:
            conn.execute(text("ALTER TABLE messages ADD COLUMN deleted_at DATETIME"))
        if "reply_to_author_id" not in mcols:
            conn.execute(text("ALTER TABLE messages ADD COLUMN reply_to_author_id BIGINT"))
        if "reference_id" not in mcols:
            conn.execute(text("ALTER TABLE messages ADD COLUMN reference_id BIGINT"))
        if "ix_messages_reply_to_author_id" not in _indexes(engine, "messages"):
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_messages_reply_to_author_id "
                    "ON messages (reply_to_author_id)"
                )
            )
        if "oldest_message_id" not in _columns(engine, "channel_states"):
            conn.execute(
                text("ALTER TABLE channel_states ADD COLUMN oldest_message_id BIGINT")
            )


def _v02_audit_log(engine: Engine) -> None:
    """v2: таблица audit_log — журнал действий админки (admin_app.py).

    Создаётся через SQLAlchemy Core (метаданные моделей), идемпотентно.
    """
    from .entities import AuditLog  # локальный импорт: модель -> её же DDL

    AuditLog.__table__.create(engine, checkfirst=True)


MIGRATIONS: list[tuple[str, Callable[[Engine], None]]] = [
    ("baseline: связи ответов (reference_id/reply_to_author_id), deleted_at, курсоры", _v01_baseline),
    ("audit_log: журнал действий админки", _v02_audit_log),
    # Пример добавления новой миграции (следующий номер!):
    # ("messages: колонка pinned", lambda eng: eng.execute(...)),
]

LATEST_VERSION = len(MIGRATIONS)


# --------------------------------------------------------------------------- #
# Исполнитель                                                                 #
# --------------------------------------------------------------------------- #

def get_current_version(engine: Engine) -> int:
    """Текущая версия схемы: 0, если таблицы версий ещё нет."""
    if not _table_exists(engine, MIGRATION_TABLE):
        return 0
    with engine.connect() as conn:
        row = conn.execute(text(f"SELECT MAX(version) FROM {MIGRATION_TABLE}")).scalar()
    return int(row or 0)


def stamp_baseline(engine: Engine) -> None:
    """Для новых БД: создать таблицу версий и пометить её как LATEST.

    Схема уже полностью создана Base.metadata.create_all из актуальных моделей,
    прогонять исторические шаги бессмысленно (они и так идемпотентны — но
    быстрее и чище сразу проставить версию).
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} ("
                " version INTEGER PRIMARY KEY,"
                " description TEXT NOT NULL,"
                " applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
            )
        )
        cur = conn.execute(text(f"SELECT COUNT(*) FROM {MIGRATION_TABLE}")).scalar()
        if not cur:
            conn.execute(
                text(
                    f"INSERT INTO {MIGRATION_TABLE} (version, description) "
                    "VALUES (:v, :d)"
                ),
                {"v": LATEST_VERSION, "d": "initial schema via create_all"},
            )


def run_migrations(engine: Engine) -> int:
    """Применить все отсутствующие миграции; вернуть применённое число шагов."""
    current = get_current_version(engine)
    if current > LATEST_VERSION:
        raise RuntimeError(
            f"Версия схемы {current} новее кода (LATEST={LATEST_VERSION}). "
            "Обновите приложение — откатывать схему автоматически безопасно нельзя."
        )
    if current == LATEST_VERSION:
        log.debug("Миграции: схема уже в актуальной версии v%s.", current)
        return 0

    # Создаём таблицу версий, если её нет (миграция с ad-hoc ALTER'ов).
    with engine.begin() as conn:
        conn.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} ("
                " version INTEGER PRIMARY KEY,"
                " description TEXT NOT NULL,"
                " applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
            )
        )
        # Stray-БД: таблицы есть, но версии нулевые — проверим, что baseline
        # физически присутствует, и просто проставим версию без переприменения.
        if current == 0 and _table_exists(engine, "messages"):
            mcols = _columns(engine, "messages")
            cscols = _columns(engine, "channel_states")
            if (
                "reference_id" in mcols
                and "reply_to_author_id" in mcols
                and "deleted_at" in mcols
                and "oldest_message_id" in cscols
            ):
                conn.execute(
                    text(
                        f"INSERT INTO {MIGRATION_TABLE} (version, description) "
                        "VALUES (:v, :d)"
                    ),
                    {"v": 1, "d": "adopted pre-existing ad-hoc schema"},
                )
                current = 1

    applied = 0
    for idx in range(current, LATEST_VERSION):
        description, fn = MIGRATIONS[idx]
        target = idx + 1
        with engine.begin() as conn:
            fn(conn.engine if hasattr(conn, "engine") else engine)
            conn.execute(
                text(
                    f"INSERT OR REPLACE INTO {MIGRATION_TABLE} (version, description) "
                    "VALUES (:v, :d)"
                ),
                {"v": target, "d": description},
            )
        applied += 1
        log.info("Миграция v%s применена: %s", target, description)
    return applied


def migrate(engine: Engine) -> None:
    """Точка входа для init_db: довести схему до LATEST_VERSION."""
    run_migrations(engine)


if __name__ == "__main__":  # ручной CLI: python -m app.models.migrations [url]
    import sys

    from sqlalchemy import create_engine

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    url = sys.argv[1] if len(sys.argv) > 1 else "sqlite:///data/profiling.db"
    if url.startswith("sqlite+aiosqlite"):
        url = url.replace("sqlite+aiosqlite", "sqlite")
    eng = create_engine(url, future=True)
    before = get_current_version(eng)
    n = run_migrations(eng)
    print(f"Версия схемы: v{before} -> v{get_current_version(eng)} (применено шагов: {n})")
