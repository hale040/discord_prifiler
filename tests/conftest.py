"""Общий fixture для тестов анкет: in-memory SQLite поверх того же ReadOnlyDb,
что использует продакшен (app.services.profile_export.db).

Схема повторяет Postgres в части колонок, читаемых модулем
app.services.profile_export.questionnaire: chats.collects_qa,
chat_members.(channel_id, author_id, questionnaire, message_id),
messages.(id, channel_id, author_id, content, clean_content, timestamp,
reference_id, thread_root_id), user_profiles.

Три детали, без которых тесты врут:
  * btrim()/now() навешиваются на каждое DBAPI-соединение — в Postgres они
    встроенные, в SQLite их нет, и запросы анкеты молча деградировали бы до [];
  * StaticPool, чтобы все сессии SQLAlchemy видели одну и ту же in-memory БД
    (иначе UPDATE из qa_posts_autofill «исчезает» между соединениями);
  * синтаксис `UPDATE t cm SET ... FROM (SELECT DISTINCT ON ...)` транслируется
    в эквивалент, понятный SQLite (см. _sqlite_compatible) — подменяется только
    ДРАЙВЕР, прод-SQL модуля questionnaire не переписывается под тесты.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool

from app.services.profile_export.db import ReadOnlyDb


def _sqlite_compatible(sql: str) -> str:
    """Postgres-диалект прод-запросов -> эквивалент для SQLite.

    Подменяется только ДРАЙВЕР: прод-SQL в questionnaire.py остаётся
    Postgres-каноничным, а здесь транслируются ровно два расхождения:
      * `UPDATE t cm SET ... FROM (SELECT DISTINCT ON (...) ...) sub` — SQLite
        не знает ни алиаса в UPDATE, ни DISTINCT ON: разворачиваем в
        correlated-подзапросы (тот же выбор «самого раннего поста первого
        уровня» и тот же CASE «ручную анкету не перезаписывать»);
      * `updated_at = now()` — функция now() регистрируется ниже на коннекте.
    Всё остальное (btrim, JOIN'ы, CTE в fetch_qa_replies) выполняется как есть.
    """
    s = " ".join(sql.split())
    m = re.match(
        r"UPDATE chat_members cm SET message_id = sub\.pid,"
        r" questionnaire = CASE WHEN btrim\(COALESCE\(cm\.questionnaire, ''\)\)"
        r" = '' THEN COALESCE\(sub\.pcontent, ''\) ELSE cm\.questionnaire END,"
        r" updated_at = now\(\)"
        r" FROM \(SELECT DISTINCT ON \(cm2\.channel_id, cm2\.author_id\)"
        r" cm2\.channel_id AS ch, cm2\.author_id AS au, m\.id AS pid,"
        r" COALESCE\(m\.content, ''\) AS pcontent"
        r" FROM (.*?)"
        r" WHERE \(cm2\.questionnaire IS NULL OR btrim\(cm2\.questionnaire\) = ''\)"
        r" AND cm2\.message_id IS NULL"
        r"(.*?) ORDER BY cm2\.channel_id, cm2\.author_id, m\.timestamp ASC"
        r"\) sub WHERE cm\.channel_id = sub\.ch AND cm\.author_id = sub\.au$",
        s)
    if m:
        joins, extra = m.group(1), m.group(2)
        # уровень «пост первого уровня» прод строит по алиасу m в подзапросе;
        # в наружном correlated-подзапросе тот же алиас занят таблицей
        # chat_members — переименовываем сообщение в cpm синхронно везде
        def _renjoin(frag: str) -> str:
            return re.sub(r"\bm\.", "cpm.", frag.replace("cm2", "cm"))
        joins = _renjoin(joins)
        extra = _renjoin(extra)
        cand = (f"FROM chat_members cm {joins} {extra}"
                "AND cm.channel_id = chat_members.channel_id "
                "AND cm.author_id = chat_members.author_id "
                "AND (cm.questionnaire IS NULL OR btrim(cm.questionnaire) = '') "
                "AND cm.message_id IS NULL ")
        pick = f"(SELECT cpm.id {cand}ORDER BY cpm.timestamp ASC, cpm.id ASC LIMIT 1)"
        pick_txt = (f"(SELECT COALESCE(cpm.content, '') {cand}"
                    "ORDER BY cpm.timestamp ASC, cpm.id ASC LIMIT 1)")
        return ("UPDATE chat_members SET message_id = " + pick +
                ", questionnaire = CASE WHEN btrim(COALESCE(questionnaire, ''))"
                " = '' THEN COALESCE(" + pick_txt + ", '')"
                " ELSE questionnaire END, updated_at = now()"
                " WHERE EXISTS (SELECT 1 FROM chat_members qm"
                " WHERE qm.channel_id = chat_members.channel_id"
                " AND qm.author_id = chat_members.author_id"
                " AND (qm.questionnaire IS NULL"
                " OR btrim(qm.questionnaire) = '')"
                " AND qm.message_id IS NULL)")
    return s


class WritableDb(ReadOnlyDb):
    """Тот же API, что в проде, плюс запись для сиддинга тестов.

    execute()/write() сохраняют контракты прода (список dict-строк и
    rowcount соответственно) — значит проверяется реальный SQL модуля
    анкеты, а не его заглушка.
    """

    def execute(self, sql: str) -> list[dict]:
        with self._engine.connect() as conn:
            return [dict(r._mapping)
                    for r in conn.execute(text(_sqlite_compatible(sql)))]

    def write(self, sql: str) -> int:
        with self._engine.begin() as conn:
            return conn.execute(text(_sqlite_compatible(sql))).rowcount or 0

    def execute_write(self, sql: str) -> int:
        with self._engine.begin() as conn:
            return conn.execute(text(_sqlite_compatible(sql))).rowcount

    def commit(self) -> None:  # совместимость с raw-API sqlite в тестах
        pass


def _register_funcs(dbapi_conn, _record):
    dbapi_conn.create_function("btrim", 1, lambda s: (s or "").strip())
    dbapi_conn.create_function(
        "now", 0,
        lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))


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
        db.commit()

    return WritableDb(engine)
