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


# --- Alembic -----------------------------------------------------------------
# Единственный механизм миграций схемы с 2026-10-24. Исторические self-made
# шаги v1..v15 перенесены в две baseline-ревизии (app/migrations/versions):
#   a00000000001 — полная схема (эквивалент v1..v10 + dm-полей),
#   a00000000002 — уточнение типов dm-колонок.
ALEMBIC_INI = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "alembic.ini")
ALEMBIC_HEAD = "a00000000010"


def _alembic_config():
    from alembic.config import Config

    cfg = Config(ALEMBIC_INI)
    return cfg


def _alembic_version(engine) -> str | None:
    """Текущая ревизия Alembic в базе (None, если таблицы версий нет)."""
    from sqlalchemy import inspect as sa_inspect, text as _text

    if "alembic_version" not in sa_inspect(engine).get_table_names():
        return None
    with engine.connect() as conn:
        return conn.execute(_text("SELECT version_num FROM alembic_version")).scalar()


def _legacy_migration_state(engine) -> tuple[int, bool]:
    """(версия self-made миграций, есть ли данные в messages).

    Возвращает (0, False), если таблицы schema_version нет вовсе.
    """
    from sqlalchemy import inspect as sa_inspect, text as _text

    tables = set(sa_inspect(engine).get_table_names())
    if "schema_version" not in tables:
        return 0, ("messages" in tables and _has_rows(engine, "messages"))
    with engine.connect() as conn:
        ver = conn.execute(_text("SELECT MAX(version) FROM schema_version")).scalar() or 0
        has_data = False
        if "messages" in tables:
            has_data = bool(conn.execute(_text("SELECT 1 FROM messages LIMIT 1")).scalar())
    return int(ver), has_data


def _has_rows(engine, table: str) -> bool:
    from sqlalchemy import inspect as sa_inspect, text as _text

    if table not in sa_inspect(engine).get_table_names():
        return False
    with engine.connect() as conn:
        return bool(conn.execute(_text(f"SELECT 1 FROM {table} LIMIT 1")).scalar())


def _stamp_legacy_base(engine) -> None:
    """Перенос учёта версий со self-made схемы (schema_version) на Alembic.

    Живые базы (миграции v1..vN уже применены историческим механизмом)
    стемпятся базовой ревизией Alembic без переприменения DDL — это и есть
    «перенос существующих миграций» в Alembic. Пустые базы не стемпятся:
    для них штатно прогоняется upgrade().
    """
    from alembic import command

    from app.models.migrations import _columns, _table_exists

    ver, has_data = _legacy_migration_state(engine)
    if ver == 0 and not has_data:
        return  # чистый лист — пусть upgrade() создаст всё с нуля
    # Стемпим ровно на применённый уровень (не выше!): база с schema_version<10
    # физически не содержит таблиц v10, и «щедрый» стемп скрывал это до первого
    # обращения. Дальше штатный alembic upgrade доведёт недостающие ревизии.
    target = ALEMBIC_HEAD if ver >= 15 else ("a00000000002" if ver >= 10 else "a00000000001")
    command.stamp(_alembic_config(), revision=target)
    # Физическая сверка после стемпа: «живая» база может быть неполной
    # (например, упавший прогон миграций оставил schema_version=15 без
    # dm_* колонок). Стемп «на глаз» в таком случае запрещён — сразу
    # доливаем недостающее идемпотентным DDL, иначе первое же обращение
    # к user_profiles упадёт с UndefinedColumn.
    if _table_exists(engine, "user_profiles"):
        have = _columns(engine, "user_profiles")
        watch = {c.name for c in UserProfile.__table__.columns
                 if c.name.startswith("dm_")} | {"pending_dm"}
        missing = sorted(watch - have)
        if missing:
            log.warning(
                "Alembic: после стемпа %s физически отсутствуют колонки %s — "
                "доливаю прямой идемпотентный DDL.",
                target, ", ".join(missing),
            )
            _apply_dm_ddl(engine, missing)
    # Физическая сверка таблиц динамических сигналов v10: база могла получить
    # schema_version>=10 без самих таблиц (упавший прогон). Стемп «на веру»
    # запрещён — доливаем идемпотентным DDL из исторической функции v10.
    if _table_exists(engine, "user_profiles"):
        from app.models.migrations import _v10_dynamic_signals

        want = {"message_revisions", "reaction_events",
                "presence_snapshots", "interaction_aggregates"}
        absent = sorted(want - {t for t in want if _table_exists(engine, t)})
        if absent:
            log.warning(
                "Alembic: отсутствуют таблицы v10 (%s) — доливаю идемпотентным DDL.",
                ", ".join(absent),
            )
            _v10_dynamic_signals(engine)
    log.info(
        "Alembic: живая база (self-made v%s, данные=%s) стемплена в %s; "
        "исторические шаги v1..v15 повторно не применяются.",
        ver, has_data, target,
    )


def _apply_dm_ddl(engine, missing: list[str]) -> None:
    """Прямой идемпотентный DDL для недостающих dm-колонок user_profiles.

    Страховка против ложных стемпов (упавшие прогоны миграций, ручные
    правки БД). Использует исторические функции v11..v15 целиком, затем
    точечно добавляет то, что они не покрыли.
    """
    from sqlalchemy import text as _text

    from app.models.migrations import (
        _columns,
        _table_exists,
        _v11_dm_collection,
        _v12_dm_diagnostics,
        _v13_dm_manual_channel,
        _v14_dm_status,
        _v15_dm_pending_flag,
    )

    for fix in (_v11_dm_collection, _v12_dm_diagnostics,
                _v13_dm_manual_channel, _v14_dm_status, _v15_dm_pending_flag):
        fix(engine)
    have = _columns(engine, "user_profiles")
    still = sorted(set(missing) - have)
    if not still:
        return
    with engine.begin() as conn:
        for col in still:
            if col in ("dm_last_error", "dm_status"):
                conn.execute(_text(
                    f"ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS {col} TEXT"))
            elif col in ("dm_fail_count", "dm_messages_total"):
                conn.execute(_text(
                    f"ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS {col} "
                    f"INTEGER DEFAULT 0"))
            elif col in ("dm_fetch", "dm_channel_manual", "pending_dm"):
                conn.execute(_text(
                    f"ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS {col} BOOLEAN"
                    + (" DEFAULT FALSE" if col != "dm_fetch" else "")))
            elif col == "dm_synced_at":
                conn.execute(_text(
                    "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS dm_synced_at "
                    "TIMESTAMP WITH TIME ZONE"))
            else:
                conn.execute(_text(
                    f"ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS {col} BIGINT"))


def ensure_schema(engine) -> None:
    """Гарант целостности схемы перед первым запросом приложения.

    Порядок:
    1. Первичная инициализация полной схемы из моделей выполняется ТОЛЬКО
       для пустых баз (нет ни таблиц, ни alembic_version). Для живых баз
       create_all не вызывается — единственным источником структуры БД после
       перехода на Alembic являются ревизии.
    2. Перенос учёта версий: живые базы со self-made schema_version стемпятся
       базовой Alembic-ревизией (их DDL уже применён исторически).
    3. alembic upgrade head — применяет все недостающие ревизии.

    Страховка: если после upgrade физически отсутствуют dm_* колонки
    (ложный стемп, ручные правки БД) — прогоняется прямой идемпотентный DDL.
    """
    from sqlalchemy import text as _text

    from app.models.migrations import _columns, _table_exists

    try:
        tables_now = set(__import__("sqlalchemy").inspect(engine).get_table_names())
        alembic_ver = _alembic_version(engine)

        # 1. Первичная инициализация полной схемы — только для пустых баз.
        if not tables_now and alembic_ver is None:
            Base.metadata.create_all(engine)
            tables_now = set(__import__("sqlalchemy").inspect(engine).get_table_names())

        # 2. Перенос учёта версий со self-made миграций на Alembic:
        #    живые базы стемпятся, их исторические ALTER'ы не повторяются.
        if alembic_ver is None and tables_now:
            _stamp_legacy_base(engine)
            alembic_ver = _alembic_version(engine)

        # 3. Alembic — единственный механизм применения новых ревизий.
        #    На стемпленных живых бадах upgrade() no-op; на пустых создаёт всё.
        if _alembic_version(engine) != ALEMBIC_HEAD:
            from alembic import command

            command.upgrade(_alembic_config(), "head")
            log.info("Alembic: схема доведена до head (%s).", _alembic_version(engine))

        # 4. Страховка физической целостности (ложный стемп / ручные правки):
        #    при отсутствии dm_* колонок доливаем прямой идемпотентный DDL.
        if _table_exists(engine, "user_profiles"):
            have = _columns(engine, "user_profiles")
            watch = {c.name for c in UserProfile.__table__.columns
                     if c.name.startswith("dm_")} | {"pending_dm"}
            missing_dm = sorted(watch - have)
            if missing_dm:
                log.warning(
                    "ensure_schema: в живой БД отсутствуют колонки %s — "
                    "прямой идемпотентный DDL (ревизии a00000000001/2).",
                    ", ".join(missing_dm),
                )
                _apply_dm_ddl(engine, missing_dm)

        # 5. Сверка нормализации ролей (ревизия a00000000004): справочник roles
        #    мог не создаться при ложном стемпе — доливаем идемпотентно.
        #    ВАЖНО: без downgrade-stamp — он откатывал alembic_version на v3, и
        #    последующие ревизии (a00000000005) переприменялись поверх уже
        #    изменённой схемы. upgrade() сам по себе идемпотентен (IF NOT EXISTS).
        if _table_exists(engine, "user_roles") and not _table_exists(engine, "roles"):
            log.warning("ensure_schema: таблица-справочник roles отсутствует — "
                        "идемпотентный прогон ревизии a00000000004.")
            from alembic import command

            cfg = _alembic_config()
            command.upgrade(cfg, "head")

        # 6. Страховка против «тихого пропуска» Alembic: если alembic_version
        #    формально = head, но физически в user_profiles нет колонок из
        #    моделей (упавший halfway прогон миграций, ложный стемп, ручные
        #    правки БД) — приложение падает на первом же SELECT с
        #    UndefinedColumn (например qa_synced, rev a00000000011). Доливаем
        #    недостающие колонки прямым идемпотентным DDL из типов моделей.
        if _table_exists(engine, "user_profiles"):
            have_up = _columns(engine, "user_profiles")
            want_up = {c.name for c in UserProfile.__table__.columns}
            missing_up = sorted(want_up - have_up)
            if missing_up:
                log.warning(
                    "ensure_schema: в живой БД отсутствуют колонки user_profiles "
                    "%s при alembic_version=%s — прямой идемпотентный DDL.",
                    ", ".join(missing_up), _alembic_version(engine),
                )
                with engine.begin() as conn:
                    for colname in missing_up:
                        coltype = UserProfile.__table__.columns[colname].type.compile(
                            engine.dialect) or "TEXT"
                        conn.execute(_text(
                            f"ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS "
                            f"{colname} {coltype}"))
                # NOT NULL-колонки на непустой таблице получили NULL —
                # затираем дефолтами из модели, чтобы ORM-запросы не падали.
                for colobj in UserProfile.__table__.columns:
                    if colobj.name in missing_up and not colobj.nullable:
                        default = colobj.server_default
                        lit = ("FALSE" if default is None and colobj.type.python_type is bool
                               else "0" if default is None and colobj.type.python_type in (int,)
                               else "''")
                        if default is not None:
                            compiled = str(default.arg) if hasattr(default, "arg") else None
                            lit = compiled or lit
                        with engine.begin() as conn:
                            conn.execute(_text(
                                f"UPDATE user_profiles SET {colobj.name} = {lit} "
                                f"WHERE {colobj.name} IS NULL"))
    except Exception as exc:  # noqa: BLE001
        log.error(
            "Миграции схемы не прошли (%s). Проверьте DATABASE_URL и "
            "права; вручную: alembic upgrade head",
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

    # Единственный механизм миграций — Alembic (см. ensure_schema):
    # пустая база получает полную схему из ревизий, живая стемпится и
    # доводится недостающими ревизиями. create_all внутри ensure_schema
    # вызывается только для пустых баз (первичная инициализация).
    ensure_schema(engine)

    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def inspect_tables(engine) -> list[str]:
    from sqlalchemy import inspect as sa_inspect

    return sa_inspect(engine).get_table_names()
