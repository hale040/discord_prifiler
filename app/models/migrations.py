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


def _v03_user_priority(engine: Engine) -> None:
    """v3: флаг приоритетного пользователя в user_profiles.

    priority      — 1/0 (булев); ставится вручную в админке, используется
                    фильтрами лент и экспортом (--priority-only).
    priority_note — короткий комментарий «почему приоритетный».
    Индекс ix_user_profiles_priority — для быстрых выборок фильтра.
    Идемпотентно: колонки/индекс создаются только если отсутствуют.
    """
    with engine.begin() as conn:
        ucols = _columns(engine, "user_profiles")
        if "priority" not in ucols:
            conn.execute(
                text("ALTER TABLE user_profiles ADD COLUMN priority BOOLEAN NOT NULL DEFAULT 0")
            )
        if "priority_note" not in ucols:
            conn.execute(text("ALTER TABLE user_profiles ADD COLUMN priority_note VARCHAR(512)"))
        if "ix_user_profiles_priority" not in _indexes(engine, "user_profiles"):
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_user_profiles_priority "
                    "ON user_profiles (priority)"
                )
            )


def _v04_compact_raw_json(engine: Engine) -> None:
    """v4: очистка messages.raw_json от полного дампа Discord-объектов.

    Исторически в каждую строку messages писался полный ``msg.to_dict()``
    (десятки КБ на сообщение) — база раздувалась в разы, а анализ использует
    только смапленные колонки. Теперь raw_json сообщений не сохраняется вовсе
    (STORE_RAW_JSON=0 по умолчанию), а старые дампы вычищаются этим шагом
    (остаются только строки длиннее 4096 байт — типичный размер полного дампa).

    Идемпотентно: повторный запуск ничего не меняет. Перед очисткой БД
    автоматически резервируется в data/backup_pre_vN_<ts>.db (копия whole-file,
    поэтому повторный прогон миграции на копии безопасен).
    """
    import os
    import shutil
    from datetime import datetime, timezone

    url = str(engine.url)
    if not url.startswith("sqlite"):
        return  # чистим только SQLite-базы коллектора
    path = url.split("///", 1)[1] or ":memory:"
    if path == ":memory:" or not os.path.exists(path):
        return
    with engine.begin() as conn:
        n = conn.execute(
            text("SELECT COUNT(*) FROM messages WHERE raw_json IS NOT NULL "
                 "AND LENGTH(raw_json) > 4096")
        ).scalar() or 0
        if not n:
            return
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup = os.path.join(os.path.dirname(path) or ".", f"backup_pre_v4_{ts}.db")
        # Бэкап делаем ДО транзакции: SQLite whole-file copy (файл + -wal),
        # чтобы на копии можно было повторно прогнать миграцию безопасно.
        shutil.copy2(path, backup)
        try:
            with engine.connect() as wc:
                wc.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001
            pass
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE messages SET raw_json = NULL "
                 "WHERE raw_json IS NOT NULL AND LENGTH(raw_json) > 4096")
        )
    log.info("Миграция v4: очищено raw_json в %s сообщениях; бэкап: %s", n, backup)


def _v05_user_games(engine: Engine) -> None:
    """Таблица user_games (игры/активности пользователя) — для старых БД,
    созданных до появления модели UserGame. Новая схема создаётся через
    Base.metadata.create_all автоматически."""
    if _table_exists(engine, "user_games"):
        return
    from app.models.entities import Base

    with engine.begin() as conn:
        # создаём только эту таблицу (остальные уже есть)
        table = Base.metadata.tables["user_games"]
        table.create(bind=conn, checkfirst=True)


def _v06_profile_snapshots(engine: Engine) -> None:
    """Таблица profile_snapshots (история изменений био/статуса/баннера/ссылок).

    Новая схема создаётся через Base.metadata.create_all; этот шаг добавляет
    таблицу в старые БД, созданные до появления модели."""
    if _table_exists(engine, "profile_snapshots"):
        return
    from app.models.entities import Base

    with engine.begin() as conn:
        table = Base.metadata.tables["profile_snapshots"]
        table.create(bind=conn, checkfirst=True)


def _v07_role_dynamics_and_attach_kind(engine: Engine) -> None:
    """Динамика ролей + классификация вложений:
    - user_roles: first_seen / last_seen / last_lost_at (потерянные роли больше
      не удаляются молча — видно, когда роль выдали и когда сняли);
    - attachments: kind (image|meme|screenshot|document|archive|audio|video).
    Идемпотентно; существующие строки получают first/last_seen = now."""
    cols = _columns(engine, "user_roles")
    if "first_seen" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE user_roles ADD COLUMN first_seen DATETIME"))
            conn.execute(text("ALTER TABLE user_roles ADD COLUMN last_seen DATETIME"))
            conn.execute(text("ALTER TABLE user_roles ADD COLUMN last_lost_at DATETIME"))
            conn.execute(text(
                "UPDATE user_roles SET first_seen = CURRENT_TIMESTAMP, "
                "last_seen = CURRENT_TIMESTAMP WHERE first_seen IS NULL"
            ))
    acols = _columns(engine, "attachments")
    if "kind" not in acols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE attachments ADD COLUMN kind VARCHAR(32)"))
        # разовая классификация уже собранных вложений по имени/MIME
        from sqlalchemy import text as _t  # noqa: PLC0415
        with engine.begin() as conn:
            rows = conn.execute(_t(
                "SELECT id, filename, content_type FROM attachments "
                "WHERE kind IS NULL LIMIT 200000"
            )).fetchall()
        from app.services.mappers import classify_attachment  # noqa: PLC0415
        with engine.begin() as conn:
            for rid, fname, ctype in rows:
                k = classify_attachment(fname, ctype, None, None)
                conn.execute(_t("UPDATE attachments SET kind=:k WHERE id=:i"),
                             {"k": k, "i": rid})


def _v08_psych_profiles(engine: Engine) -> None:
    """Психологические профили от нейросети-аналитика.

    - psych_profiles: полный JSON ответа (result_json) + ключевые метрики
      отдельными колонками для фильтрации; история версий, активна одна;
    - psych_signals: атомарные evidence с дедупом по нормализованной цитате
      (уникальный индекс user_id+signal_key+quote_norm) — повторный импорт
      не раздувает базу;
    - user_interests: интересы накапливаются между срезами диалога
      (уникальный индекс user_id+topic_norm, evidence_count).
    Идемпотентно: таблицы создаются через модели, индексы — если их нет."""
    from app.models.entities import Base

    created = []
    for tname in ("psych_profiles", "psych_signals", "user_interests"):
        if not _table_exists(engine, tname):
            with engine.begin() as conn:
                Base.metadata.tables[tname].create(bind=conn, checkfirst=True)
            created.append(tname)
    # частичный индекс «активный профиль» (одна активная версия на пользователя)
    if _table_exists(engine, "psych_profiles") and "ix_psych_profiles_current" not in _indexes(engine, "psych_profiles"):
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE INDEX ix_psych_profiles_current ON psych_profiles (user_id, is_current)"
            ))
    if created:
        log.info("Миграция v8: созданы таблицы %s", ", ".join(created))


MIGRATIONS: list[tuple[str, Callable[[Engine], None]]] = [
    ("baseline: связи ответов (reference_id/reply_to_author_id), deleted_at, курсоры", _v01_baseline),
    ("audit_log: журнал действий админки", _v02_audit_log),
    ("user_profiles: флаг priority + priority_note + индекс", _v03_user_priority),
    ("messages: очистка раздутого raw_json (с бэкапом БД перед запуском)", _v04_compact_raw_json),
    ("user_games: таблица игр/активностей пользователя (FK на user_profiles)", _v05_user_games),
    ("profile_snapshots: история изменений профиля (bio/status/banner/connections)", _v06_profile_snapshots),
    ("user_roles: first/last_seen + last_lost_at; attachments: kind", _v07_role_dynamics_and_attach_kind),
    ("psych_profiles/psych_signals/user_interests: психопрофиль от LLM (миграция v8)", _v08_psych_profiles),
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
