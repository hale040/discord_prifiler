"""Общий fixture для тестов анкет: in-memory SQLite поверх того же ReadOnlyDb,
что использует продакшен (app.services.profile_export.db).

Схема повторяет Postgres в части колонок, читаемых модулем
app.services.profile_export.questionnaire: chats.collects_qa,
chat_members.(channel_id, author_id, questionnaire, message_id),
messages.(id, channel_id, author_id, content, clean_content, timestamp,
reference_id, thread_root_id), user_profiles.

Три детали, без которых тесты врут:
  * btrim() навешивается на каждое DBAPI-соединение — в Postgres функция
    встроенная, в SQLite её нет, и запросы анкеты молча деградировали бы до [];
  * StaticPool, чтобы все сессии SQLAlchemy видели одну и ту же in-memory БД
    (иначе UPDATE из qa_posts_autofill «исчезает» между соединениями);
  * ATTACH-база information_schema с таблицей columns: прод-код определяет
    фактические колонки messages запросом
        SELECT column_name FROM information_schema.columns WHERE table_name=...
    Поэтому SQL остаётся Postgres-совместимым, а тесты подменяют только ДРАЙВЕР
    (VIEW тут невозможен: SQLite запрещает представлению ссылаться на другую
    базу, поэтому список колонок материализуется таблицей).
"""
from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool

from app.services.profile_export.db import ReadOnlyDb


class WritableDb(ReadOnlyDb):
    """Тот же API, что в проде, плюс запись для сиддинга тестов.

    execute() по-прежнему возвращает список dict-строк — значит проверяется
    реальный SQL модуля анкеты, а не его заглушка.
    """

    def execute_write(self, sql: str) -> int:
        with self._engine.begin() as conn:
            n = conn.execute(text(sql)).rowcount
            _sync_meta(conn)   # DROP/CREATE TABLE должны видеться метаданным
            return n

    def commit(self) -> None:  # совместимость с raw-API sqlite в тестах
        pass


def _register_funcs(dbapi_conn, _record):
    dbapi_conn.create_function("btrim", 1, lambda s: (s or "").strip())


DDL = [
    """CREATE TABLE chats (
           channel_id INTEGER PRIMARY KEY, guild_id INTEGER, name TEXT,
           collects_qa INTEGER DEFAULT 0, tracked INTEGER DEFAULT 0,
           dl_files INTEGER DEFAULT 0, messages_total INTEGER DEFAULT 0)""",
    """CREATE TABLE chat_members (
           channel_id INTEGER, author_id INTEGER, questionnaire TEXT,
           message_id INTEGER, updated_at TEXT,
           PRIMARY KEY (channel_id, author_id))""",
    """CREATE TABLE messages (
           id INTEGER PRIMARY KEY, channel_id INTEGER, guild_id INTEGER,
           author_id INTEGER, content TEXT, clean_content TEXT,
           timestamp TEXT, reference_id INTEGER, thread_root_id INTEGER,
           type INTEGER DEFAULT 0)""",
    """CREATE TABLE user_profiles (
           user_id INTEGER PRIMARY KEY, username TEXT, global_name TEXT)""",
]


@pytest.fixture()
def conn():
    engine = create_engine("sqlite://", future=True, module=sqlite3,
                           poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    event.listen(engine, "connect", _register_funcs)

    with engine.connect() as db:
        for stmt in DDL:
            db.exec_driver_sql(stmt)
        db.exec_driver_sql("ATTACH DATABASE ':memory:' AS information_schema")
        db.exec_driver_sql("CREATE TABLE information_schema.columns "
                           "(table_name TEXT, column_name TEXT)")
        _sync_meta(db)
        db.commit()

    return WritableDb(engine)


def _sync_meta(db) -> None:
    """Заполнить information_schema.columns из sqlite_master (аналог PG-метаданных)."""
    rows = db.exec_driver_sql(
        "SELECT m.name, p.name FROM main.sqlite_master m, "
        "pragma_table_info(m.name) p WHERE m.type IN ('table','view')").fetchall()
    db.exec_driver_sql("DELETE FROM information_schema.columns")
    db.exec_driver_sql("INSERT INTO information_schema.columns VALUES (?, ?)",
                       [(t, c) for t, c in rows])
