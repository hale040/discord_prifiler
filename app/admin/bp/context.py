"""Общее состояние Flask-приложения админки и вспомогательные функции.

Модуль порождён декомпозицией монолита app/admin/app.py: сюда вынесены
списки колонок лент (VIEW_COLUMNS), утилиты разбора значений форм
(parse_field/editable_fields) и класс AdminContext — «живые» зависимости
(engine, пароль, лимитер входа) со всеми helper-методами (audit, подготовка
лент, форматирование ячеек, ссылки на пользователей, рендер страниц).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from flask import abort, current_app, render_template, url_for
from markupsafe import Markup
from sqlalchemy import Boolean, MetaData, Table, func, inspect, select as sa_select, text
from sqlalchemy.engine import Engine


def utcnow() -> datetime:
    """Текущее время UTC (совпадает с helpers.utcnow в моделях)."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Какие столбцы показывать в лентах таблиц («только нужные»).                 #
# Список — порядок отображения; всё остальное скрыто из ленты, но доступно    #
# целиком на странице записи. ENV-надстройки (формат "таблица:столбец"):      #
#   ADMIN_EXTRA_COLUMNS=messages:reactions_json,...  — добавить в ленту       #
#   ADMIN_HIDE_COLUMNS=user_profiles:username,...    — убрать из ленты        #
# --------------------------------------------------------------------------- #
VIEW_COLUMNS: dict[str, list[str]] = {
    "messages": [
        "id", "timestamp", "author_id", "clean_content", "reply_to_author_id",
        "edited_at", "deleted_at", "word_count", "has_attachment",
        "reaction_total",
    ],
    "user_profiles": [
        "id", "user_id", "username", "global_name", "dm_fetch",
        "dm_messages_total", "created_at_discord", "bot", "about",
        "premium_type", "last_updated",
    ],
    "guild_member_profiles": [
        "id", "user_id", "guild_id", "nick", "joined_at", "timeout_until",
        "top_role", "mute", "deaf",
    ],
    "user_roles": [
        "id", "user_id", "role_name", "guild_id", "position",
    ],
    "attachments": [
        "id", "message_id", "filename", "content_type", "size",
        "is_image", "downloaded",
    ],
    "channel_states": [
        "channel_id", "guild_id", "last_message_id", "oldest_message_id",
        "last_poll_at", "messages_total",
    ],
    "audit_log": [
        "id", "created_at", "action", "target", "result",
    ],
    # --- новый функционал (миграции v5..v8) ---------------------------------
    "user_games": [
        "id", "user_id", "name", "kind", "details", "state",
        "first_seen", "last_seen",
    ],
    "profile_snapshots": [
        "id", "user_id", "taken_at", "about", "status_text",
        "avatar_hash", "banner_hash", "changed",
    ],
    "psych_profiles": [
        "id", "user_id", "model", "mbti_type", "mbti_confidence",
        "enneagram_type", "confidence_overall", "severity_max",
        "messages_analyzed", "period_from", "period_to",
        "is_current", "created_at",
    ],
    "psych_signals": [
        "id", "user_id", "signal_key", "quote", "message_id",
        "severity", "created_at",
    ],
    "user_interests": [
        "id", "user_id", "topic", "evidence_count", "evidence",
        "first_seen_at", "last_seen_at",
    ],
    # --- динамические сигналы (миграция v10) --------------------------------
    "message_revisions": [
        "id", "message_id", "author_id", "edit_number", "edited_at",
        "prev_content", "new_content", "recorded_at",
    ],
    "reaction_events": [
        "id", "message_id", "user_id", "msg_author_id", "emoji",
        "action", "occurred_at",
    ],
    "presence_snapshots": [
        "id", "user_id", "status", "status_text", "activity_name",
        "activity_kind", "observed_at",
    ],
    "interaction_aggregates": [
        "id", "source_user_id", "target_user_id", "replies_given",
        "reactions_received", "updated_at",
    ],
}

# Ссылки «кто это?» — колонки, по которым строится ссылка на user_profiles.
USER_LINK_COLS = ("user_id",)


@dataclass
class AdminContext:
    """Зависимости админки, общие для всех Blueprint'ов.

    Исторически весь этот код жил в замыканиях create_app(); при декомпозиции
    на Blueprint'ы helpers стали методами контекста — логика перенесена
    дословно, без изменений поведения.
    """

    engine: Engine
    password: str
    login_limiter: object  # app.admin.security.LoginRateLimiter
    env_extra: dict = field(default_factory=dict)
    env_hide: dict = field(default_factory=dict)

    # --- мелкие утилиты ------------------------------------------------------

    def _json_pretty(self, v) -> str:
        """Красивый JSON для textarea в карточке записи (если значение — JSON).

        Колонки *_json на PostgreSQL — JSONB: psycopg возвращает уже разобранные
        list/dict; строковые значения поддержаны для совместимости.
        """
        if v is None:
            return ""
        if isinstance(v, (dict, list)):
            try:
                return json.dumps(v, ensure_ascii=False, indent=2, default=str)
            except Exception:
                return str(v)
        s = str(v)
        try:
            return json.dumps(json.loads(s), ensure_ascii=False, indent=2)
        except Exception:
            return s

    def _is_json_field(self, name: str) -> bool:
        return name.endswith("_json") or name in ("result_json", "connections_json")

    def sync_url(self, url: str) -> str:
        # PostgreSQL-only: нормализация драйвера + fail-fast на чужих диалектах.
        from app.models.db import _to_sync_url, require_postgres

        return _to_sync_url(require_postgres(url))

    def _env_pairs(self, name: str) -> dict[str, set[str]]:
        """ADMIN_EXTRA_COLUMNS / ADMIN_HIDE_COLUMNS: 'таблица:столбец,таблица:столбец2'."""
        out: dict[str, set[str]] = {}
        for item in (os.getenv(name) or "").split(","):
            item = item.strip()
            if not item or ":" not in item:
                continue
            t, c = item.split(":", 1)
            out.setdefault(t.strip(), set()).add(c.strip())
        return out

    # --- аудит ---------------------------------------------------------------

    def audit(self, action: str, target: str = "", details: str = "", result: str = "ok") -> None:
        try:
            with self.engine.begin() as conn:
                # created_at передаём явно: default=utcnow в модели работает
                # только через ORM/Core-insert, а мы пишем raw SQL — иначе
                # колонка остаётся NULL и журнал выглядит пустым.
                conn.execute(
                    text(
                        "INSERT INTO audit_log (created_at, actor, action, target, details, result) "
                        "VALUES (:ts, 'admin', :a, :t, :d, :r)"
                    ),
                    {
                        "ts": utcnow(),
                        "a": action,
                        "t": target[:256],
                        "d": (details or "")[:4000],
                        "r": result[:512],
                    },
                )
        except Exception:  # журнал не должен ронять админку
            pass

    # --- helpers --------------------------------------------------------------

    def tables(self) -> list[str]:
        return sorted(inspect(self.engine).get_table_names())

    def columns_of(self, t: str) -> list[str]:
        return [c["name"] for c in inspect(self.engine).get_columns(t)]

    def pk_of(self, t: str) -> str:
        pk = inspect(self.engine).get_pk_constraint(t)["constrained_columns"]
        if pk:
            return pk[0]
        # Fallback для таблиц без явного PK (исторические ad-hoc БД):
        # выбираем столбец с самой высокой селективностью — иначе ссылки на
        # записи и UPDATE по "PK" будут работать неверно.
        cols = self.columns_of(t)
        try:
            sa_tbl = Table(t, MetaData(), autoload_with=self.engine)
            with self.engine.connect() as conn:
                best, best_n = cols[0], -1
                for c in cols:
                    n = conn.execute(
                        sa_select(func.count()).select_from(sa_tbl).where(
                            sa_tbl.columns[c].is_not(None)
                        ).distinct()
                    ).scalar() or 0
                    if n > best_n:
                        best, best_n = c, n
            return best
        except Exception:
            return cols[0]

    def safe_ident(self, name: str) -> str:
        """Разрешаем только валидные SQL-идентификаторы (защита от инъекций имён)."""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
            abort(400, description=f"Некорректное имя: {name!r}")
        return name

    def fmt_cell(self, v) -> tuple[str, bool]:
        """(текст, is_json) для отображения; длинные значения сворачиваются."""
        if v is None:
            return "—", False
        if isinstance(v, (dict, list)):  # JSONB-колонки приходят уже разобранными
            s = json.dumps(v, ensure_ascii=False, default=str)
            if len(s) > 400:
                return s[:400] + "…", True
            return s, True
        s = str(v)
        if len(s) > 400:
            return s[:400] + "…", True
        return s, False

    def view_cols_of(self, table: str) -> list[str]:
        """Столбцы для ленты: только нужные (VIEW_COLUMNS) + ENV-надстройки.

        Порядок: PK первым, затем список VIEW_COLUMNS (+ENV extra), минус ENV hide.
        Для неизвестных таблиц — все столбцы, но без громоздких *_json/raw.
        """
        all_cols = self.columns_of(table)
        pk = self.pk_of(table)
        base = VIEW_COLUMNS.get(table)
        if base is None:
            base = [c for c in all_cols
                    if not (c.endswith("_json") or c == "raw_json" or c == "details")]
        chosen = [pk] + [c for c in base if c != pk and c in all_cols]
        for c in self.env_extra.get(table, ()):  # явные дополнения из .env
            if c in all_cols and c not in chosen:
                chosen.append(c)
        hidden = self.env_hide.get(table, set())
        return [c for c in chosen if c not in hidden]

    # --- ссылки на пользователя и связанные записи ----------------------------

    def user_names_for(self, ids: set) -> dict:
        """user_id -> 'username' для подписи ссылок в лентах (graceful)."""
        ids = {i for i in ids if i is not None}
        if not ids or "user_profiles" not in self.tables():
            return {}
        try:
            up = Table("user_profiles", MetaData(), autoload_with=self.engine)
            with self.engine.connect() as conn:
                rows = conn.execute(
                    sa_select(up.columns.user_id, up.columns.username,
                              up.columns.global_name)
                    .where(up.columns.user_id.in_(list(ids)))
                ).all()
            return {r[0]: (r[1] or r[2] or str(r[0])) for r in rows}
        except Exception:
            return {}

    def related_counts(self, user_id) -> dict:
        """Сколько связанных записей в новых таблицах (для кнопки в ленте)."""
        out = {}
        if user_id is None:
            return out
        for t in ("psych_profiles", "psych_signals", "user_interests",
                  "user_games", "profile_snapshots"):
            if t not in self.tables():
                continue
            try:
                tb = Table(t, MetaData(), autoload_with=self.engine)
                with self.engine.connect() as conn:
                    n = conn.execute(
                        sa_select(func.count()).select_from(tb).where(
                            tb.columns.user_id == user_id
                        )
                    ).scalar() or 0
                if n:
                    out[t] = n
            except Exception:
                continue
        return out

    def profile_view_url(self, pid) -> str | None:
        """Ссылка на просмотр результата LLM (если есть tab/profile)."""
        if pid is None:
            return None
        try:
            return url_for("profile_view", pid=int(pid))
        except Exception:
            return None

    def current_psych(self, user_id) -> dict | None:
        """Актуальная версия психопрофиля пользователя: {id, model, created_at}."""
        if user_id is None or "psych_profiles" not in self.tables():
            return None
        try:
            pp = Table("psych_profiles", MetaData(), autoload_with=self.engine)
            with self.engine.connect() as conn:
                r = conn.execute(
                    sa_select(pp.columns.id, pp.columns.model, pp.columns.created_at)
                    .where(pp.columns.user_id == int(user_id),
                           pp.columns.is_current.is_(True))
                    .order_by(pp.columns.created_at.desc())
                    .limit(1)
                ).first()
            return {"id": r[0], "model": r[1], "created_at": r[2]} if r else None
        except Exception:
            return None

    def editable_fields(self, sa_tbl: Table) -> list[dict]:
        """Редактируемые поля записи по схеме SQLAlchemy (тип/nullable/PK)."""
        fields = []
        for col in sa_tbl.columns:
            t = col.type
            cls = type(t).__name__
            if cls in ("DateTime", "TIMESTAMP"):
                kind = "datetime"
            elif isinstance(t, Boolean):
                # Boolean может быть обёрнут в TypeDecorator (например, Coalesce) —
                # проверяем через dialect_impl + процессоры, а не только по имени.
                kind = "bool"
            else:
                impl_name = ""
                try:
                    impl = t.dialect_impl(self.engine.dialect)
                    impl_name = type(impl).__name__
                except Exception:
                    pass
                if impl_name in ("BOOLEAN", "BOOL") or "BOOL" in impl_name.upper():
                    kind = "bool"
                elif cls in ("Integer", "SmallInteger"):
                    kind = "int"
                elif cls in ("BigInteger", "BIGINT", "BigInt"):
                    # PG BIGINT приходит с autoload и как dialect-класс BIGINT
                    # (не наследник sqlalchemy.BigInteger) — snowflake не влезает
                    # в JS-число, ввод текстом -> int.
                    kind = "bigint"
                elif cls in ("Float", "Numeric", "DECIMAL"):
                    kind = "float"
                elif impl_name.upper().startswith("JSONB") or cls == "JSON":
                    kind = "json"        # JSONB: ввод/вывод в виде JSON-текста
                elif cls in ("Text", "CLOB"):
                    kind = "textarea"
                elif getattr(t, "length", None) and t.length > 64:
                    kind = "textarea"
                else:
                    kind = "text"
            fields.append({
                "name": col.name, "kind": kind, "pk": bool(col.primary_key),
                "nullable": bool(col.nullable),
            })
        return fields

    def parse_field(self, kind: str, raw: str, nullable: bool):
        """Строка формы -> значение по типу колонки; ValueError при неверном типе.

        Пустая строка: NULL если nullable, иначе ошибка ('0'/False для чисел).
        """
        raw = (raw or "").strip()
        if raw == "":
            if nullable:
                return None
            raise ValueError("поле обязательное (не nullable), пустое значение запрещено")
        if kind == "bool":
            return raw.lower() in ("1", "true", "yes", "on", "да")
        if kind in ("int", "bigint"):
            try:
                return int(raw)
            except ValueError:
                raise ValueError(f"ожидается целое число, получено {raw!r}")
        if kind == "float":
            try:
                return float(raw)
            except ValueError:
                raise ValueError(f"ожидается число, получено {raw!r}")
        if kind == "json":
            # JSONB-колонки: принимаем JSON-текст из textarea, храним объект
            try:
                return json.loads(raw)
            except ValueError:
                raise ValueError(f"ожидается корректный JSON, получено {raw[:60]!r}")
        if kind == "datetime":
            from datetime import datetime as _dt
            # Принимает и naive ('2026-10-24 12:59'), и вывод PostgreSQL
            # ('2023-10-03 15:52:28.201000+00:00'): изометричный T->пробел,
            # Z->+00:00, затем fromisoformat (3.11+ понимает смещения),
            # запасные strptime-форматы без смещения.
            s = raw.strip().replace("T", " ")
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            try:
                return _dt.fromisoformat(s)
            except ValueError:
                pass
            s2 = s.split("+")[0].strip()  # отбросить смещение как последний шанс
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    return _dt.strptime(s2, fmt)
                except ValueError:
                    continue
            raise ValueError(f"ожидается дата 'ГГГГ-ММ-ДД [ЧЧ:ММ[:СС]]', получено {raw!r}")
        return raw

    def render(self, tpl: str, **ctx):
        """Страница = layout.html с подставленным содержимым templates/admin/<tpl>.html."""
        return render_template(
            "admin/layout.html",
            content=Markup(render_template(f"admin/{tpl}.html", **ctx)),
        )


def make_context(engine, password: str, login_limiter) -> AdminContext:
    """Сборка контекста админки (аналог прежнего тела create_app до маршрутов)."""
    ctx = AdminContext(engine=engine, password=password, login_limiter=login_limiter)
    ctx.env_extra = ctx._env_pairs("ADMIN_EXTRA_COLUMNS")
    ctx.env_hide = ctx._env_pairs("ADMIN_HIDE_COLUMNS")
    return ctx


def get_ctx() -> AdminContext:
    """Текущий контекст админки из Flask application context."""
    return current_app.config["ADMIN_CTX"]
