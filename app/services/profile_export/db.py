"""app.services.profile_export.db — часть модуля экспорта «дел» для LLM.

Автономный экспорт из PostgreSQL (SQLAlchemy + stdlib), без Discord.
Сгенерирован разделением export_profiles.py; публичный CLI — export_profiles.py.
"""
from __future__ import annotations

import os
import re
import sys
try:
    from sqlalchemy import create_engine, inspect as sa_inspect, text
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.engine import Engine
except ImportError:  # pragma: no cover
    sys.exit("[FATAL] Не установлен SQLAlchemy: pip install 'SQLAlchemy>=2.0' "
             "'psycopg[binary]' (БД — PostgreSQL)")
from .env import load_json


class ReadOnlyDb:
    """Тонкий read-only слой поверх SQLAlchemy-engine (экс-sqlite3.Connection).

    .execute(sql) возвращает список dict-строк (как раньше sqlite3.Row);
    ошибки СУБД (в т.ч. отсутствующие таблицы/колонки в старых схемах)
    поднимаются как sqlalchemy.exc.SQLAlchemyError — вызывающий код ловит
    их для graceful-деградации.
    """

    def __init__(self, engine: Engine):
        self._engine = engine

    def execute(self, sql: str) -> list[dict]:
        with self._engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(text(sql))]

    def columns(self, table: str) -> set[str]:
        """Аналог PRAGMA table_info: имена колонок таблицы (пустое — нет таблицы)."""
        try:
            return {c["name"] for c in sa_inspect(self._engine).get_columns(table)}
        except SQLAlchemyError:
            return set()

    def has_table(self, table: str) -> bool:
        try:
            return sa_inspect(self._engine).has_table(table)
        except SQLAlchemyError:
            return False

    def close(self) -> None:
        self._engine.dispose()


def _to_psycopg_url(url: str) -> str:
    """Нормализация DATABASE_URL под sync-драйвер psycopg(3)."""
    url = re.sub(r"\+asyncpg$", "+psycopg", url)
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if not url.startswith(("postgresql:", "postgresql+")):
        sys.exit(
            f"[FATAL] Ожидался PostgreSQL URL, получено: {url!r}. "
            "SQLite больше не поддерживается."
        )
    return url


def open_db(db_arg: str) -> tuple[ReadOnlyDb, str]:
    """Открыть БД по URL (или из env DATABASE_URL). Возвращает (conn, label)."""
    url = db_arg or os.getenv("DATABASE_URL")
    if not url:
        sys.exit("[FATAL] База не задана: передайте --db <postgresql://...> "
                 "или DATABASE_URL в окружении/.env.")
    engine = create_engine(_to_psycopg_url(url), future=True, pool_pre_ping=True)
    try:
        with engine.connect():
            pass
    except SQLAlchemyError as exc:
        sys.exit(f"[FATAL] Не удалось подключиться к PostgreSQL ({url}): {exc}")
    return ReadOnlyDb(engine), url


def fetch_users(conn) -> list[dict]:
    # приоритет мог ещё не появиться в старых схемах (миграция v3) —
    # подставляем FALSE, чтобы row_get("priority") работал всегда
    if conn.has_table("user_profiles") and "priority" not in conn.columns("user_profiles"):
        return conn.execute(
            "SELECT *, FALSE AS priority, NULL::varchar AS priority_note"
            " FROM user_profiles ORDER BY user_id")
    return conn.execute("SELECT * FROM user_profiles ORDER BY user_id")


def fetch_guild_profiles(conn) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for r in conn.execute("SELECT * FROM guild_member_profiles ORDER BY user_id"):
        out.setdefault(r["user_id"], []).append(r)
    return out


def fetch_role_defs(conn) -> dict[tuple[int, int], str]:
    """(guild_id, role_id) -> name из справочника roles (миграция a00000000004)."""
    out: dict[tuple[int, int], str] = {}
    try:
        if not conn.has_table("roles"):
            return out
        for r in conn.execute("SELECT guild_id, role_id, name FROM roles"):
            out[(int(r["guild_id"]), int(r["role_id"]))] = r["name"] or ""
    except Exception:  # noqa: BLE001 — старый дамп без справочника: пусто
        pass
    return out


def fetch_roles(conn) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for r in conn.execute("SELECT * FROM user_roles ORDER BY user_id, position DESC"):
        out.setdefault(r["user_id"], []).append(r)
    return out


def fetch_psych_profiles(conn) -> dict[int, dict]:
    """user_id -> компактный активный психопрофиль от LLM (psych_profiles +
    signals + interests). Таблиц может не быть в старых БД — пусто."""
    try:
        rows = conn.execute(
            "SELECT user_id, model, mbti_type, enneagram_type, confidence_overall,"
            " period_from, period_to, messages_analyzed, result_json"
            " FROM psych_profiles WHERE is_current = TRUE")
    except SQLAlchemyError:
        return {}
    out: dict[int, dict] = {}
    for r in rows:
        uid = int(r["user_id"])
        try:
            res = load_json(r["result_json"]) or {}
        except (ValueError, TypeError):
            res = {}
        entry = {
            "model": r["model"], "mbti": r["mbti_type"],
            "enneagram": r["enneagram_type"], "conf": r["confidence_overall"],
            "period": [str(r["period_from"])[:10], str(r["period_to"])[:10]]
                      if r["period_from"] and r["period_to"] else None,
            "msgs": r["messages_analyzed"],
            "big_five": (res.get("big_five") or {}),
            "style": res.get("style"),
        }
        out[uid] = {k: v for k, v in entry.items() if v not in (None, {}, [])}
    try:
        sig = conn.execute(
            "SELECT user_id, signal_key, quote FROM psych_signals s"
            " WHERE s.user_id IN (SELECT user_id FROM psych_profiles WHERE is_current = 1)"
            " ORDER BY created_at DESC LIMIT 200")
        for r in sig:
            e = out.setdefault(int(r["user_id"]), {})
            e.setdefault("signals", [])
            if len(e["signals"]) < 6:
                e["signals"].append({"k": r["signal_key"], "q": r["quote"][:80]})
    except SQLAlchemyError:
        pass
    try:
        it = conn.execute(
            "SELECT user_id, topic, evidence_count FROM user_interests i"
            " WHERE i.user_id IN (SELECT user_id FROM psych_profiles WHERE is_current = 1)"
            " ORDER BY evidence_count DESC LIMIT 200")
        for r in it:
            e = out.setdefault(int(r["user_id"]), {})
            e.setdefault("interests", [])
            if len(e["interests"]) < 12:
                e["interests"].append({"t": r["topic"], "n": int(r["evidence_count"] or 1)})
    except SQLAlchemyError:
        pass
    return out


def fetch_games(conn) -> dict[int, list[str]]:
    """user_id -> список названий игр/активностей (таблица user_games).
    Таблицы может не быть в старых БД до миграции v5 — тогда пустой словарь."""
    out: dict[int, list[str]] = {}
    try:
        rows = conn.execute(
            "SELECT user_id, name, kind FROM user_games ORDER BY user_id, last_seen DESC"
        )
    except SQLAlchemyError:
        return out
    for r in rows:
        label = str(r["name"])
        kind = str(r["kind"] or "")
        if kind and kind not in ("playing",):
            label = f"{label} ({kind})"
        lst = out.setdefault(int(r["user_id"]), [])
        if label not in lst:
            lst.append(label)
    return out


def fetch_messages(conn) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    q = """SELECT id, channel_id, guild_id, author_id, content, clean_content,
                  type, reference_id, reply_to_author_id, timestamp, edited_at,
                  deleted_at, pinned,
                  word_count, char_count, has_attachment, embed_count,
                  mention_count, hashtag_count, reaction_total, reactions_json,
                  mention_users_json, stickers_json
           FROM messages WHERE deleted_at IS NULL ORDER BY timestamp ASC"""
    for r in conn.execute(q):
        d = r
        # старые БД без колонки reply_to_author_id -> None (достроим позже)
        d.setdefault("reply_to_author_id", None)
        out.setdefault(d["author_id"], []).append(d)
    return out


def fetch_all_messages_flat(conn) -> list[dict]:
    """Все сообщения канала хронологически (для сборки диалогов с собеседниками)."""
    q = """SELECT id, channel_id, guild_id, author_id, content, clean_content,
                  type, reference_id, reply_to_author_id, timestamp, edited_at,
                  deleted_at, pinned,
                  word_count, char_count, has_attachment, embed_count,
                  mention_count, hashtag_count, reaction_total, reactions_json,
                  mention_users_json, stickers_json
           FROM messages WHERE deleted_at IS NULL ORDER BY timestamp ASC, id ASC"""
    rows = list(conn.execute(q))
    for d in rows:
        d.setdefault("reply_to_author_id", None)
    return rows


def fetch_reaction_events(conn) -> dict[int, list[dict]]:
    """События реакций (таблица reaction_events, v10). author_id -> список.

    Пусто, если таблицы ещё нет (старые БД до миграции) — graceful degrade.
    """
    out: dict[int, list[dict]] = {}
    try:
        rows = conn.execute(
            "SELECT msg_author_id, user_id, emoji, action, occurred_at "
            "FROM reaction_events ORDER BY occurred_at ASC")
    except Exception:
        return out
    for r in rows:
        out.setdefault(r["msg_author_id"], []).append(r)
    return out


def fetch_presence_snapshots(conn) -> dict[int, list[dict]]:
    """Снимки присутствия (presence_snapshots, v10): user_id -> список."""
    out: dict[int, list[dict]] = {}
    try:
        rows = conn.execute(
            "SELECT user_id, status, status_text, activity_name, observed_at "
            "FROM presence_snapshots ORDER BY observed_at ASC")
    except Exception:
        return out
    for r in rows:
        out.setdefault(r["user_id"], []).append(r)
    return out


def build_names(conn, users: list[dict]) -> dict[int, str]:
    """user_id -> отображаемое имя (global_name/username, серверный ник в приоритете)."""
    names = {u["user_id"]: (u["global_name"] or u["username"] or f"id{u['user_id']}")
             for u in users}
    try:
        for g in conn.execute("SELECT user_id, nick FROM guild_member_profiles"):
            if g["nick"]:
                names[g["user_id"]] = g["nick"]
    except SQLAlchemyError:
        pass
    return names


def fetch_attachments(conn) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    try:
        rows = conn.execute(
            "SELECT message_id, filename, url, proxy_url, content_type, width, height,"
            " is_image, is_spoiler, downloaded, stored_path, kind FROM attachments"
        )
    except SQLAlchemyError:
        # старая БД без колонки kind (до миграции v7) — берём без неё
        rows = conn.execute(
            "SELECT message_id, filename, url, proxy_url, content_type, width, height,"
            " is_image, is_spoiler, downloaded, stored_path FROM attachments"
        )
    for r in rows:
        d = r
        d.setdefault("kind", None)
        out.setdefault(d["message_id"], []).append(d)
    return out


def fetch_profile_history(conn) -> dict[int, dict]:
    """user_id -> {'snaps': [...], 'roles_hist': [...]} из profile_snapshots и
    динамики user_roles. Таблиц/колонок может не быть в старых БД — пусто."""
    out: dict[int, dict] = {}
    try:
        snaps = conn.execute(
            "SELECT user_id, taken_at, about, status_text, connections_json, changed"
            " FROM profile_snapshots ORDER BY user_id, taken_at DESC"
        )
    except SQLAlchemyError:
        snaps = []
    for r in snaps:
        e = out.setdefault(int(r["user_id"]), {"snaps": [], "roles_hist": []})
        item: dict = {"at": str(r["taken_at"] or "")[:10]}
        if r["about"]:
            item["bio"] = r["about"]
        if r["status_text"]:
            item["status"] = r["status_text"]
        if r["connections_json"]:
            try:
                cs = load_json(r["connections_json"]) or []
                item["conns"] = [
                    f"{c.get('platform', '?')}: {c.get('name', '')}".strip(": ")
                    for c in cs if isinstance(c, dict)
                ]
            except Exception:  # noqa: BLE001
                pass
        e["snaps"].append(item)
    try:
        role_cols = conn.columns("user_roles")
        if "first_seen" in role_cols and "last_lost_at" in role_cols:
            # имя роли берём из справочника roles (JOIN), а не из связи:
            # переименование роли в Discord больше не требует правок данных.
            has_name_col = "role_name" in role_cols
            name_expr = ("COALESCE(r.name, ur.role_name)" if has_name_col
                         else "COALESCE(r.name, '')")
            rh = conn.execute(
                f"SELECT ur.user_id, {name_expr} AS role_name,"
                " ur.first_seen, ur.last_seen, ur.last_lost_at"
                " FROM user_roles ur"
                " LEFT JOIN roles r ON r.guild_id = ur.guild_id"
                " AND r.role_id = ur.role_id"
                " WHERE ur.last_lost_at IS NOT NULL OR ur.first_seen IS NOT NULL"
            )
            for r in rh:
                e = out.setdefault(int(r["user_id"]), {"snaps": [], "roles_hist": []})
                he: dict = {"role": r["role_name"]}
                if r["first_seen"]:
                    he["from"] = str(r["first_seen"])[:10]
                if r["last_lost_at"]:
                    he["lost"] = str(r["last_lost_at"])[:10]
                e["roles_hist"].append(he)
    except SQLAlchemyError:
        pass
    return out
