"""Ядро админки: Flask-приложение поверх PostgreSQL (см. admin_app.py — CLI)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import Boolean, MetaData, Table, Text, cast as sa_cast, create_engine, func, inspect, select as sa_select, text
from sqlalchemy import and_ as sa_and, or_ as sa_or, update as sa_update, insert as sa_insert
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import CHAR, VARCHAR
from markupsafe import Markup
from sqlalchemy.engine import Engine

BASE_DIR = Path(__file__).resolve().parents[2]


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
        "id", "user_id", "username", "global_name", "priority",
        "created_at_discord", "bot", "about", "premium_type", "last_updated",
    ],
    "guild_member_profiles": [
        "id", "user_id", "guild_id", "nick", "joined_at", "timeout_until",
        "top_role", "mute", "deaf",
    ],
    "user_roles": [
        "id", "user_id", "role_id", "guild_id", "position",
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
}

# Ссылки «кто это?» — колонки, по которым строится ссылка на user_profiles.
USER_LINK_COLS = ("user_id",)


def _json_pretty(v) -> str:
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


def _is_json_field(name: str) -> bool:
    return name.endswith("_json") or name in ("result_json", "connections_json")


def _sync_url(url: str) -> str:
    # PostgreSQL-only: нормализация драйвера + fail-fast на чужих диалектах.
    from app.models.db import _to_sync_url, require_postgres

    return _to_sync_url(require_postgres(url))


# --------------------------------------------------------------------------- #
# Приложение                                                                   #
# --------------------------------------------------------------------------- #


def create_app(database_url: str) -> Flask:
    load_dotenv(BASE_DIR / ".env")
    password = os.getenv("ADMIN_PASSWORD", "")
    if not password:
        raise SystemExit(
            "[FATAL] ADMIN_PASSWORD не задан в .env — админка не запущена (fail-closed)."
        )
    secret = os.getenv("SECRET_KEY") or hashlib.sha256(
        f"profile-admin::{password}".encode()
    ).hexdigest()

    app = Flask(
        __name__,
        template_folder=str(BASE_DIR / "app" / "admin" / "templates"),
    )
    app.secret_key = secret
    # context_processor csrf_token() (now no-op: CSRF выключен, проект локальный)
    # — нужен, чтобы {{ csrf_token() }} в record.html не падал 500.
    from app.admin.security import csrf_protect
    csrf_protect(app)
    app.config["DATABASE_URL"] = database_url
    engine: Engine = create_engine(_sync_url(database_url), future=True)
    app.config["ENGINE"] = engine

    # Схема могла остаться от версии до v10/v11 (create_all новые КОЛОНКИ не
    # добавляет) — доводим до LATEST перед первым запросом. Идемпотентно и
    # дёшево на актуальной базе; при ошибке не роняем админку (коллектор
    # тоже вызывает миграции при старте).
    try:
        from app.models.db import ensure_schema

        ensure_schema(engine)
    except Exception as _mig_exc:  # noqa: BLE001
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "Админка: миграции схемы пропущены (%s). Примените вручную: "
            "alembic upgrade head", _mig_exc)

    # --- аудит -------------------------------------------------------------
    def audit(action: str, target: str = "", details: str = "", result: str = "ok") -> None:
        try:
            with engine.begin() as conn:
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

    # --- helpers ------------------------------------------------------------
    def tables() -> list[str]:
        return sorted(inspect(engine).get_table_names())

    def columns_of(t: str) -> list[str]:
        return [c["name"] for c in inspect(engine).get_columns(t)]

    def pk_of(t: str) -> str:
        pk = inspect(engine).get_pk_constraint(t)["constrained_columns"]
        if pk:
            return pk[0]
        # Fallback для таблиц без явного PK (исторические ad-hoc БД):
        # выбираем столбец с самой высокой селективностью — иначе ссылки на
        # записи и UPDATE по "PK" будут работать неверно.
        cols = columns_of(t)
        try:
            sa_tbl = Table(t, MetaData(), autoload_with=engine)
            with engine.connect() as conn:
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

    def safe_ident(name: str) -> str:
        """Разрешаем только валидные SQL-идентификаторы (защита от инъекций имён)."""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
            abort(400, description=f"Некорректное имя: {name!r}")
        return name

    def fmt_cell(v) -> tuple[str, bool]:
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

    def _env_pairs(name: str) -> dict[str, set[str]]:
        """ADMIN_EXTRA_COLUMNS / ADMIN_HIDE_COLUMNS: 'таблица:столбец,таблица:столбец2'."""
        out: dict[str, set[str]] = {}
        for item in (os.getenv(name) or "").split(","):
            item = item.strip()
            if not item or ":" not in item:
                continue
            t, c = item.split(":", 1)
            out.setdefault(t.strip(), set()).add(c.strip())
        return out

    ENV_EXTRA = _env_pairs("ADMIN_EXTRA_COLUMNS")
    ENV_HIDE = _env_pairs("ADMIN_HIDE_COLUMNS")

    def view_cols_of(table: str) -> list[str]:
        """Столбцы для ленты: только нужные (VIEW_COLUMNS) + ENV-надстройки.

        Порядок: PK первым, затем список VIEW_COLUMNS (+ENV extra), минус ENV hide.
        Для неизвестных таблиц — все столбцы, но без громоздких *_json/raw.
        """
        all_cols = columns_of(table)
        pk = pk_of(table)
        base = VIEW_COLUMNS.get(table)
        if base is None:
            base = [c for c in all_cols
                    if not (c.endswith("_json") or c == "raw_json" or c == "details")]
        chosen = [pk] + [c for c in base if c != pk and c in all_cols]
        for c in ENV_EXTRA.get(table, ()):  # явные дополнения из .env
            if c in all_cols and c not in chosen:
                chosen.append(c)
        hidden = ENV_HIDE.get(table, set())
        chosen = [c for c in chosen if c not in hidden]
        # unknown-таблица без VIEW_COLUMNS может дать 20+ колонок — «каша»,
        # которую fixed-раскладка не спасает: режем до читаемых 12 (PK первый)
        if table not in VIEW_COLUMNS and len(chosen) > 12:
            chosen = chosen[:12]
        return chosen

    # --- ссылки на пользователя и связанные записи ---------------------------
    def user_names_for(ids: set) -> dict:
        """user_id -> 'username' для подписи ссылок в лентах (graceful)."""
        ids = {i for i in ids if i is not None}
        if not ids or "user_profiles" not in tables():
            return {}
        try:
            up = Table("user_profiles", MetaData(), autoload_with=engine)
            with engine.connect() as conn:
                rows = conn.execute(
                    sa_select(up.columns.user_id, up.columns.username,
                              up.columns.global_name)
                    .where(up.columns.user_id.in_(list(ids)))
                ).all()
            return {r[0]: (r[1] or r[2] or str(r[0])) for r in rows}
        except Exception:
            return {}

    def related_counts(user_id) -> dict:
        """Сколько связанных записей в новых таблицах (для кнопки в ленте)."""
        out = {}
        if user_id is None:
            return out
        for t in ("psych_profiles", "psych_signals", "user_interests",
                  "user_games", "profile_snapshots"):
            if t not in tables():
                continue
            try:
                tb = Table(t, MetaData(), autoload_with=engine)
                with engine.connect() as conn:
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

    def profile_view_url(pid) -> str | None:
        """Ссылка на просмотр результата LLM (если есть tab/profile)."""
        if pid is None:
            return None
        try:
            return url_for("profile_view", pid=int(pid))
        except Exception:
            return None

    def current_psych(user_id) -> dict | None:
        """Актуальная версия психопрофиля пользователя: {id, model, created_at}."""
        if user_id is None or "psych_profiles" not in tables():
            return None
        try:
            pp = Table("psych_profiles", MetaData(), autoload_with=engine)
            with engine.connect() as conn:
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

    def psych_map_for(user_ids: list) -> dict:
        """Один запрос: user_id -> актуальный профиль {id, model, mbti_type}.

        Для ленты профилей: бейдж 🧠 без N+1 (раньше на каждую строку шёл
        отдельный current_psych()). Возвращает {} при любой ошибке — лента
        должна переживать отсутствие таблицы/колок.
        """
        ids = [int(u) for u in user_ids if str(u).lstrip("-").isdigit()]
        if not ids or "psych_profiles" not in tables():
            return {}
        try:
            pp = Table("psych_profiles", MetaData(), autoload_with=engine)
            cols = set(pp.columns.keys())
            sel_cols = [pp.columns.user_id, pp.columns.id]
            if "mbti_type" in cols:
                sel_cols.append(pp.columns.mbti_type)
            q = sa_select(*sel_cols).where(pp.columns.user_id.in_(ids))
            if "is_current" in cols:
                q = q.where(pp.columns.is_current.is_(True))
            if "created_at" in cols:
                q = q.order_by(pp.columns.created_at.desc())
            out: dict[str, dict] = {}
            with engine.connect() as conn:
                for r in conn.execute(q).all():
                    # ключ — str(user_id): в шаблоне значения ячеек строки;
                    # первый на каждого пользователя = самый свежий (DESC)
                    k = str(r[0])
                    if k not in out:
                        out[k] = {"id": r[1], "mbti_type": (r[2] if len(r) > 2 else None)}
            return out
        except Exception:
            return {}

    def editable_fields(sa_tbl: Table) -> list[dict]:
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
                    impl = t.dialect_impl(engine.dialect)
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

    def parse_field(kind: str, raw: str, nullable: bool):
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
            # ('2023-10-03 15:52:28.201000+00:00'): T->пробел, Z->+00:00,
            # fromisoformat (понимает смещения), запасные strptime-форматы.
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

    def render(tpl: str, **ctx):
        """Страница = layout.html с подставленным содержимым templates/admin/<tpl>.html."""
        # current_psych доступен шаблонам (карточка пользователя показывает
        # кнопку 🧠 «Обновить», только если профиль уже строился)
        ctx.setdefault("current_psych", current_psych)
        return render_template(
            "admin/layout.html",
            content=Markup(render_template(f"admin/{tpl}.html", **ctx)),
        )

    # --- auth -----------------------------------------------------------------
    @app.before_request
    def require_login():
        if request.endpoint in ("login", "logout", "static"):
            return
        if not session.get("admin_ok"):
            return redirect(url_for("login"))

    # --- UI-навигация (дизайн v16): таблицы сгруппированы по смыслу, а не
    # вываливаются списком из 15 пунктов. Ключевые рабочие области — сверху:
    # профили и психо; сырые ленты собраны в раскрывающуюся группу «Данные».
    NAV_GROUPS = [
        ("Профили", [("user_profiles", "👤 Пользователи")]),
        ("Психо", [("psych_profiles", "🧠 Психопрофили")]),
        ("Мессенджер", [("messages", "💬 Сообщения"),
                        ("message_revisions", "✏️ Правки"),
                        ("reaction_events", "😀 Реакции")]),
        ("Контекст", [("guild_members", "🏛 Участники"),
                      ("presence_snapshots", "🟢 Присутствие"),
                      ("interaction_aggregates", "↔ Взаимодействия"),
                      ("channel_stats", "📊 Статы каналов")]),
    ]

    @app.context_processor
    def inject_nav():
        have = set(tables())
        groups = [(title, [(t, label) for t, label in items if t in have])
                  for title, items in NAV_GROUPS]
        groups = [(t, its) for t, its in groups if its]
        extra = [t for t in tables()
                 if t not in {x for _, its in groups for x, _ in its}]
        return {"nav_tables": tables(), "nav_groups": groups,
                "nav_extra": extra, "page": "",
                "has_psych": "psych_profiles" in have}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            pw = request.form.get("pw", "")
            # сравнение через secrets — против timing-атак
            if pw and secrets.compare_digest(pw.encode(), password.encode()):
                session["admin_ok"] = True
                audit("login", result="ok")
                return redirect(url_for("dashboard"))
            error = "Неверный пароль"
        return render("login", page="", error=error)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # --- дашборд ----------------------------------------------------------------
    @app.route("/")
    def dashboard():
        ts = tables()
        counts = []
        total = 0
        for t in ts:
            if t == "schema_version":
                continue
            with engine.connect() as conn:
                n = conn.execute(text(f"SELECT COUNT(*) FROM {safe_ident(t)}")).scalar() or 0
            counts.append((t, n))
            total += n
        recent = []
        if "audit_log" in ts:
            with engine.connect() as conn:
                recent = conn.execute(
                    text("SELECT created_at, action, target, result FROM audit_log "
                         "ORDER BY id DESC LIMIT 10")
                ).mappings().all()
        return render(
            "dashboard", page="dash", db=app.config["DATABASE_URL"],
            counts=counts, total=total, recent=recent,
        )

    # --- просмотр таблиц ----------------------------------------------------------
    @app.route("/browse/<table>")
    def browse(table: str):
        table = safe_ident(table)
        if table not in tables():
            abort(404)
        all_cols = columns_of(table)
        cols = view_cols_of(table)
        pk = pk_of(table)
        q = (request.args.get("q") or "").strip()
        col = safe_ident(request.args.get("col") or "") if request.args.get("col") else ""
        val = request.args.get("val") or ""
        prio = (request.args.get("prio") or "").strip()  # '1' только приоритетные
        cur = (request.args.get("cur") or "").strip()    # '1' только активные версии (psych_profiles)
        star_pk = request.args.get("star")              # быстрый переключатель ★
        page = max(1, int(request.args.get("page") or 1))
        per = 50

        sa_tbl = Table(table, MetaData(), autoload_with=engine)

        # Быстрая кнопка ★: переключить priority у одной записи (GET ?star=<pk>).
        if star_pk and "priority" in all_cols:
            with engine.begin() as conn:
                cur = conn.execute(
                    sa_select(sa_tbl.columns.priority).where(
                        sa_tbl.columns[pk] == star_pk
                    )
                ).scalar()
                newv = bool(cur) is False  # Boolean-колонка на PG
                conn.execute(
                    sa_update(sa_tbl)
                    .where(sa_tbl.columns[pk] == star_pk)
                    .values(priority=newv)
                )
            audit(
                "priority_toggle", f"{table}#{star_pk}",
                f"priority {'включён' if newv else 'выключен'}",
            )
            return redirect(url_for("browse", table=table, q=q or None,
                                    col=col or None, val=val or None,
                                    prio=prio or None, page=page))

        # Таблица через SQLAlchemy Core (autoload) — используется в выражениях ниже.
        where_parts = []
        if q:
            # LIKE осмысленна только для текстовых колонок; numeric/bool/timestamp/jsonb
            # в PG не поддерживают оператор ~~ (bigint ~~ text -> ошибка). Для них —
            # приведение к тексту через cast, чтобы поиск работал и по id/датам/флагам.
            like = "%" + q.replace("%", "\\%").replace("_", "\\_") + "%"
            texty = (String, Text, VARCHAR, CHAR)
            conds = []
            for c in cols:
                col_type = sa_tbl.columns[c].type
                if isinstance(col_type, texty):
                    conds.append(sa_tbl.columns[c].like(like, escape="\\"))
                else:
                    conds.append(
                        sa_cast(sa_tbl.columns[c], Text).like(like, escape="\\")
                    )
            where_parts.append(sa_or(*conds))
        if col and val:
            # Равнение по нетекстовым колонкам так же требует приведения строки из URL.
            ctype = sa_tbl.columns[col].type
            if isinstance(ctype, (String, Text, VARCHAR, CHAR)):
                where_parts.append(sa_tbl.columns[col] == val)
            elif isinstance(ctype, Boolean):
                where_parts.append(
                    sa_tbl.columns[col] == val.strip().lower() in ("1", "true", "да", "yes", "on")
                )
            else:
                where_parts.append(
                    sa_cast(sa_tbl.columns[col], Text) == val
                )
        # Фильтр «только приоритетные» (для таблиц с колонкой priority).
        if prio == "1" and "priority" in all_cols:
            where_parts.append(sa_tbl.columns.priority.is_(True))
        # psych_profiles: фильтр «только активная версия профиля».
        if cur == "1" and table == "psych_profiles" and "is_current" in all_cols:
            where_parts.append(sa_tbl.columns.is_current.is_(True))
        wclause = sa_and(*where_parts) if where_parts else None

        sel = sa_select(sa_tbl).order_by(sa_tbl.columns[pk].desc()).limit(per).offset((page - 1) * per)
        cnt = sa_select(func.count()).select_from(sa_tbl)
        if wclause is not None:
            sel = sel.where(wclause)
            cnt = cnt.where(wclause)
        with engine.connect() as conn:
            total = conn.execute(cnt).scalar() or 0
            rows_raw = conn.execute(sel).mappings().all()
        rows = [
            {"pk": r[pk], "cells": [fmt_cell(r[c]) for c in cols]}
            for r in rows_raw
        ]
        # «Обновить психо профиль»: user_id -> актуальная версия профиля (лента).
        # Один IN-запрос вместо N вызовов current_psych() на страницу.
        psych_map: dict[str, dict | None] = {}
        if table == "user_profiles" and "user_id" in cols:
            psych_map = psych_map_for([r["user_id"] for r in rows_raw])
        # Подписи user_id -> @username для колонок-ссылок (одним запросом).
        uids = {r["cells"][cols.index("user_id")][0] for r in rows} \
            if "user_id" in cols else set()
        uid_map = {}
        if uids:
            try:
                uid_map = user_names_for({int(u) for u in uids if u.isdigit()})
            except Exception:
                uid_map = {}

        def href(p: int) -> str:
            base = f"/browse/{quote_plus(table)}?page={p}"
            if q:
                base += "&q=" + quote_plus(q)
            if col:
                base += "&col=" + quote_plus(col) + "&val=" + quote_plus(val)
            if prio:
                base += "&prio=" + quote_plus(prio)
            if cur:
                base += "&cur=" + cur
            return base

        # Ширины колонок ленты: PK/статусы узкие, тексты — резиновые.
        # Один источник правды для <colgroup>: таблица никогда не расползается
        # за экран (главный жалобщик был горизонтальный скролл).
        WIDE = {"about", "content", "clean_content", "prev_content", "new_content",
                "quote", "evidence", "status_text", "raw_json", "details",
                "context", "filename", "activity_name", "top_role", "target",
                "result", "action"}
        NARROW = {pk: "70px"}
        for c in cols:
            if c == pk:
                continue
            if c in ("user_id", "author_id", "message_id", "channel_id", "guild_id",
                     "reply_to_author_id", "msg_author_id", "source_user_id",
                     "target_user_id"):
                NARROW[c] = "128px"
            elif c in ("timestamp", "created_at", "last_updated", "edited_at",
                       "occurred_at", "observed_at", "dm_synced_at", "joined_at",
                       "created_at_discord", "period_from", "period_to",
                       "first_seen", "last_seen", "updated_at", "taken_at",
                       "recorded_at", "last_poll_at"):
                NARROW[c] = "132px"
            elif c in ("priority", "bot", "is_system", "mute", "deaf", "emoji",
                       "action", "kind", "status", "mbti_type", "severity",
                       "severity_max", "confidence_overall", "mbti_confidence",
                       "enneagram_type", "premium_type", "dm_fetch",
                       "dm_messages_total", "word_count", "has_attachment",
                       "reaction_total", "is_current", "downloaded", "is_image",
                       "size", "position", "edit_number", "changed"):
                NARROW[c] = "64px"
        widths = [{"name": c, "w": NARROW.get(c),
                   "wide": (c in WIDE and c not in NARROW)} for c in cols]
        # карта «резиновых» колонок для td.clamp (подрезка в 2 строки): шаблон
        # не должен пересчитывать WIDE/NARROW — передаём готовый dict
        w_wide = {w["name"]: w["wide"] for w in widths}

        return render(
            "browse", page="browse", table=table, cols=cols, rows=rows,
            total=total, q=q, col=col, val=val, cur_page=page,
            pages=max(1, -(-total // per)), href=href,
            hidden_count=len(all_cols) - len(cols),
            prio=prio, has_priority="priority" in all_cols, pk_col=pk,
            cur=cur, is_psych_profiles=(table == "psych_profiles"),
            uid_map=uid_map, psych_map=psych_map, widths=widths, w_wide=w_wide,
        )

    # --- карточка записи: просмотр + редактирование -------------------------
    @app.route("/browse/<table>/<pk>", methods=["GET", "POST"])
    def record(table: str, pk: str):
        table = safe_ident(table)
        if table not in tables():
            abort(404)
        pk_col = pk_of(table)
        sa_tbl = Table(table, MetaData(), autoload_with=engine)
        fields = editable_fields(sa_tbl)
        kind_by_name = {f["name"]: f["kind"] for f in fields}
        null_by_name = {f["name"]: f["nullable"] for f in fields}

        # Загрузка строки (PK приводим к типу колонки).
        pk_kind = kind_by_name[pk_col]
        try:
            pk_val = parse_field(pk_kind, pk, nullable=False)
        except ValueError as e:
            abort(400, description=str(e))
        with engine.connect() as conn:
            row = conn.execute(
                sa_select(sa_tbl).where(sa_tbl.columns[pk_col] == pk_val)
            ).mappings().first()
        # Удобные ссылки вида /browse/user_profiles/<user_id>: если значение
        # не совпало с PK (id), пробуем найти запись по user_id — и дальше
        # работаем уже с настоящим PK, чтобы redirect/ссылки были корректны.
        if row is None and table == "user_profiles" \
                and pk_col != "user_id" and "user_id" in sa_tbl.columns:
            try:
                uid_val_ = int(pk)
            except ValueError:
                uid_val_ = None
            if uid_val_ is not None:
                with engine.connect() as conn:
                    row = conn.execute(
                        sa_select(sa_tbl).where(
                            sa_tbl.columns.user_id == uid_val_)
                    ).mappings().first()
                if row is not None:
                    pk = str(row[pk_col])
                    pk_val = row[pk_col]
        if row is None:
            abort(404, description=f"Запись {table}.{pk} не найдена")

        errors: dict[str, str] = {}
        if request.method == "POST":
            # Сначала парсим ВСЕ поля — UPDATE только если ошибок нет целиком.
            parsed: dict[str, object] = {}
            for name in kind_by_name:
                if name == pk_col:
                    continue  # PK не меняем
                input_name = f"fld_{name}"
                if input_name not in request.form:
                    continue  # поля нет в форме — не трогаем
                try:
                    parsed[name] = parse_field(
                        kind_by_name[name], request.form.get(input_name, ""),
                        null_by_name[name])
                except ValueError as e:
                    errors[name] = str(e)

            updates: dict[str, object] = {}
            if not errors:
                for name, new_val in parsed.items():
                    old_val = row[name]
                    # JSONB: сравниваем канонизированный JSON (dict/list),
                    # иначе textarea с тем же содержимым ложно «изменяется».
                    if kind_by_name.get(name) == "json":
                        def _canon(v):
                            if isinstance(v, str):
                                try:
                                    v = json.loads(v)
                                except ValueError:
                                    return v
                            return json.dumps(v, sort_keys=True, ensure_ascii=False, default=str) \
                                if v is not None else ""
                        if _canon(old_val) != _canon(new_val):
                            updates[name] = new_val
                        continue
                    # Сравнение по строковому представлению: из формы значения
                    # приходят строками, а bool/datetime приводятся parse_field.
                    if str(old_val if old_val is not None else "") != \
                       str(new_val if new_val is not None else ""):
                        updates[name] = new_val

            if errors:
                flash("Исправьте ошибки в полях: " + "; ".join(errors.values()), "error")
            elif not updates:
                flash("Изменений не обнаружено.", "ok")
            else:
                with engine.begin() as conn:
                    n = conn.execute(
                        sa_update(sa_tbl).values(**updates)
                        .where(sa_tbl.columns[pk_col] == pk_val)
                    ).rowcount
                changed = {k: {"old": str(row[k]), "new": str(v)} for k, v in updates.items()}
                audit("edit_row", target=f"{table}.{pk}",
                      details=str(changed)[:4000], result=f"updated={n}")
                flash(f"Сохранено: изменено полей — {len(updates)}.", "ok")
                return redirect(url_for("record", table=table, pk=pk))
            # при ошибке показываем форму с введёнными значениями
            values = {name: request.form.get(f"fld_{name}", row[name])
                      for name in kind_by_name}
        else:
            values = dict(row)

        form_fields = [
            {"name": f["name"], "kind": f["kind"], "pk": f["pk"],
             "nullable": f["nullable"],
             "type": str(sa_tbl.columns[f["name"]].type.compile()).split()[0],
             "value": (_json_pretty(values.get(f["name"]))
                       if f["kind"] == "textarea" and _is_json_field(f["name"])
                       else values.get(f["name"])),
             "error": errors.get(f["name"])}
            for f in fields
        ]

        # --- связанные данные (новый функционал v5..v8) -----------------------
        related: list[dict] = []
        uid_val = row.get("user_id")
        if table != "user_profiles" and uid_val is not None:
            # все таблицы-источники, где есть user_id
            for t in ("psych_profiles", "psych_signals", "user_interests",
                      "user_games", "profile_snapshots"):
                if t not in tables() or t == table:
                    continue
                try:
                    tb2 = Table(t, MetaData(), autoload_with=engine)
                    if "user_id" not in [c["name"] for c in inspect(engine).get_columns(t)]:
                        continue
                    with engine.connect() as conn:
                        rs = conn.execute(
                            sa_select(tb2).where(tb2.columns.user_id == uid_val)
                            .order_by(tb2.columns[pk_of(t)].desc()).limit(10)
                        ).mappings().all()
                    cols2 = view_cols_of(t)
                    related.append({
                        "table": t, "cols": cols2,
                        "rows": [[fmt_cell(r[c])[0] for c in cols2] for r in rs],
                        "pks": [r[pk_of(t)] for r in rs],
                    })
                except Exception:
                    continue
        elif table == "user_profiles":
            uid_val = row.get("user_id")
            for t in ("psych_profiles", "psych_signals", "user_interests",
                      "user_games", "profile_snapshots", "messages"):
                if t not in tables():
                    continue
                try:
                    tb2 = Table(t, MetaData(), autoload_with=engine)
                    with engine.connect() as conn:
                        n = conn.execute(
                            sa_select(func.count()).select_from(tb2).where(
                                tb2.columns.user_id == uid_val
                            )
                        ).scalar() or 0
                    related.append({"table": t, "count": n})
                except Exception:
                    continue

        return render("record", page="browse", table=table, pk=pk,
                      fields=form_fields, related=related,
                      values=values, uid=uid_val,
                      # превью покрытия данными для кнопки 🧠 (Data Quality Gate)
                      gate=(json.dumps(_psych_gate_pre(uid_val),
                                       ensure_ascii=False)
                            if table == "user_profiles" and uid_val else None),
                      # человекочитаемый заголовок карточки вместо «user_profiles · запись 12»
                      rec_title=_rec_title(table, row),
                      is_psych_profiles=(table == "psych_profiles"))

    def _rec_title(table: str, row) -> str:
        """Заголовок карточки: имя/тип объекта — а не только имя таблицы и PK."""
        try:
            if table == "user_profiles":
                name = row.get("global_name") or row.get("username") or ""
                uid = row.get("user_id")
                star = "★ " if row.get("priority") else ""
                return f"{star}{name or uid} · {uid}"
            if table == "messages":
                c = (row.get("clean_content") or row.get("content") or "").strip()
                return (c[:60] + "…") if len(c) > 60 else (c or "сообщение")
            if table == "psych_profiles":
                mbti = row.get("mbti_type")
                cur = "актуальная" if row.get("is_current") else "архивная"
                return f"психо · user {row.get('user_id')} · {mbti or '?'} · {cur}"
        except Exception:
            pass
        return ""

    @app.post("/dm/toggle/<int:user_id>")
    def dm_toggle(user_id: int):
        """Кнопка «💬 Собрать ЛС» / «💬 ЛС: стоп» в карточке пользователя.

        Просто ставим/снимаем dm_fetch; тяжёлую работу делает dm-loop в
        CollectionService (первый тик после включения — полная выгрузка
        истории диалога, далее — дешёвый инкремент новых сообщений).
        """
        enabled = request.form.get("enabled", "1") in ("1", "true", "on")
        # Engine не имеет has_table — только через Inspector (в SQLAlchemy 2.x)
        if "user_profiles" not in inspect(engine).get_table_names():
            abort(404)
        sa_tbl = Table("user_profiles", MetaData(), autoload_with=engine)
        if "dm_fetch" not in sa_tbl.columns:
            flash("Миграция v11 не применена (нет колонки dm_fetch) — обновите БД.", "error")
            return redirect(url_for("record", table="user_profiles", pk=user_id))
        with engine.begin() as conn:
            n = conn.execute(
                sa_update(sa_tbl).where(sa_tbl.columns.user_id == user_id)
                .values(
                    dm_fetch=bool(enabled),
                    # включение кнопки сбрасывает диагностику сбоев (v12):
                    # пользователь явно хочет продолжить — dm-loop возобновит
                    **({"dm_fail_count": 0, "dm_last_error": None} if enabled else {})
                )
            ).rowcount
        if n == 0 and enabled:
            # профиля ещё нет (пользователь виден только по id из сообщений) —
            # создаём заглушку, dm-loop догрузит данные сам
            with engine.begin() as conn:
                conn.execute(sa_insert(sa_tbl).values(user_id=user_id, dm_fetch=True))
        audit("dm_fetch", target=f"user={user_id}", result="on" if enabled else "off")
        flash(("💬 Сбор ЛС включён — история диалога начнёт выгружаться на следующем "
               "тике dm-loop (по умолчанию ≤ DM_POLL_INTERVAL_SEC секунд)."
               if enabled else "💬 Сбор ЛС остановлен."), "ok")
        return redirect(url_for("record", table="user_profiles", pk=user_id))

    @app.post("/dm/channel/<int:user_id>")
    def dm_channel(user_id: int):
        """Ручная привязка DM-канала (миграция v13).

        Когда ЛС закрыты от не-друзей, create_dm возвращает 403 и канал не
        находится. Но если диалог уже открыт в клиенте, id канала виден в
        адресной строке Discord: https://discord.com/channels/@me/<id>.
        Здесь мы фиксируем этот id; dm-loop читает историю только по нему
        и НЕ пытается создать новый канал. Поле «clear=1» снимает привязку.
        """
        if "user_profiles" not in inspect(engine).get_table_names():
            abort(404)
        sa_tbl = Table("user_profiles", MetaData(), autoload_with=engine)
        if "dm_channel_manual" not in sa_tbl.columns:
            flash("Миграция v13 не применена (нет колонки dm_channel_manual) — "
                  "обновите БД.", "error")
            return redirect(url_for("record", table="user_profiles", pk=user_id))
        clear = request.form.get("clear") == "1"
        raw = (request.form.get("channel_id") or "").strip()
        channel_id: int | None = None
        if not clear:
            if not raw.isdigit():
                flash("ID канала должен быть числом (из ссылки "
                      "discord.com/channels/@me/<id>).", "error")
                return redirect(url_for("record", table="user_profiles", pk=user_id))
            channel_id = int(raw)
        with engine.begin() as conn:
            n = conn.execute(
                sa_update(sa_tbl).where(sa_tbl.columns.user_id == user_id)
                .values(
                    dm_channel_id=channel_id,
                    dm_channel_manual=channel_id is not None,
                    # сброс диагностики — dm-loop сразу попробует снова
                    dm_last_error=None,
                    dm_fail_count=0,
                    # новый канал => старые курсоры невалидны
                    **({} if channel_id is None else {
                        "dm_last_message_id": None,
                        "dm_first_message_id": None,
                        "dm_messages_total": 0,
                    }),
                )
            ).rowcount
        if n == 0:
            # профиля нет — создаём заглушку с привязкой и включённым сбором
            with engine.begin() as conn:
                conn.execute(sa_insert(sa_tbl).values(
                    user_id=user_id, dm_fetch=True,
                    dm_channel_id=channel_id,
                    dm_channel_manual=channel_id is not None))
        audit("dm_channel", target=f"user={user_id}",
              result=str(channel_id) if channel_id else "cleared")
        flash(("🔗 Канал ЛС зафиксирован: dm-loop будет читать историю только "
               "по нему (создание нового канала не выполняется)."
               if channel_id else "Привязка канала снята — авто-режим."), "ok")
        return redirect(url_for("record", table="user_profiles", pk=user_id))

    def _dm_state_row(user_id: int) -> dict | None:
        """Снимок ЛС-полей профиля для виджета прогресса (v14)."""
        up = Table("user_profiles", MetaData(), autoload_with=engine)
        cols = ["dm_fetch", "dm_status", "dm_channel_id", "dm_channel_manual",
                "dm_messages_total", "dm_synced_at", "dm_last_error",
                "dm_fail_count", "dm_last_message_id"]
        have = [c for c in cols if c in up.columns]
        with engine.connect() as conn:
            row = conn.execute(
                sa_select(*[up.columns[c] for c in have])
                .where(up.columns.user_id == user_id)
            ).mappings().first()
        if row is None:
            return None
        state = dict(zip(have, tuple(row)))
        # «живой» признак выгрузки в текущем процессе недоступен из админки
        # (коллектор — отдельный процесс), поэтому ориентир — dm_status +
        # время синхронизации: running старше 30 минут считаем зависшим.
        st = state.get("dm_status")
        synced = state.get("dm_synced_at")
        stale = False
        if st == "running" and synced is not None:
            try:
                age = (datetime.now(timezone.utc) - synced).total_seconds()
                stale = age > 1800
            except Exception:  # noqa: BLE001
                pass
        state["stale_running"] = stale
        return state

    @app.get("/dm/status/<int:user_id>")
    def dm_status(user_id: int):
        """JSON для JS-виджета прогресса ЛС-сбора (карточка пользователя).

        Админка и коллектор — отдельные процессы, общего состояния нет;
        источник истины — БД (dm_status/dm_synced_at пишет dm-loop).
        """
        if "user_profiles" not in tables():
            abort(404)
        st = _dm_state_row(user_id)
        if st is None:
            abort(404)
        return jsonify(st)

    @app.post("/dm/run_now/<int:user_id>")
    def dm_run_now(user_id: int):
        """«▶ Выполнить сейчас»: немедленный проход без ожидания тика dm-loop.

        Коллектор живёт в отдельном процессе (main.py), прямого вызова к его
        event loop нет. Поэтому здесь — прямое указание через БД: сбрасываем
        счётчик сбоев и ставим dm_status=NULL, а живой dm-loop в цикле
        проверяет это поле и выполняет внеочередной проход диалога сразу
        (см. CollectionService._dm_loop). Если выгрузка уже идёт — вернём
        «already running», вторую параллельную скачку не запускаем.
        """
        if "user_profiles" not in tables():
            abort(404)
        up = Table("user_profiles", MetaData(), autoload_with=engine)
        if "dm_status" not in up.columns or "dm_fetch" not in up.columns:
            flash("Миграция v14 не применена — обновите код и перезапустите.", "error")
            return redirect(url_for("record", table="user_profiles", pk=user_id))
        with engine.connect() as conn:
            prof = conn.execute(
                sa_select(up.columns.dm_fetch, up.columns.dm_status,
                          up.columns.dm_synced_at)
                .where(up.columns.user_id == user_id)
            ).first()
        if prof is None:
            abort(404)
        enabled, status, synced = prof
        if not enabled:
            flash("Сначала включите «💬 Собрать ЛС».", "error")
            return redirect(url_for("record", table="user_profiles", pk=user_id))
        fresh_running = False
        if status == "running":
            try:
                fresh_running = ((datetime.now(timezone.utc) - synced).total_seconds()
                                 < 1800) if synced is not None else True
            except Exception:  # noqa: BLE001
                fresh_running = True
        if fresh_running:
            flash("⏳ Выгрузка уже идёт (dm-loop занят этим диалогом) — "
                  "дождитесь завершения, повторный запуск не требуется.", "ok")
            return redirect(url_for("record", table="user_profiles", pk=user_id))
        # сигнал внеочередного прохода: dm-loop увидит pending_dm=True
        with engine.begin() as conn:
            conn.execute(
                sa_update(up).where(up.columns.user_id == user_id).values(
                    pending_dm=True,
                    dm_fail_count=0,
                    dm_last_error=None,
                )
            )
        audit("dm_run_now", target=f"user={user_id}")
        flash("▶ Команда передана коллектору: dm-loop выполнит проход "
              "вне очереди (обычно в течение нескольких секунд). "
              "Прогресс — ниже на этой странице.", "ok")
        return redirect(url_for("record", table="user_profiles", pk=user_id))

    @app.post("/browse/<table>/delete")
    def delete_row(table: str):
        table = safe_ident(table)
        pk = pk_of(table)
        key = request.form.get("pk", "")
        with engine.begin() as conn:
            n = conn.execute(
                text(f"DELETE FROM {safe_ident(table)} WHERE {safe_ident(pk)} = :k"),
                {"k": key},
            ).rowcount
        audit("delete_row", target=f"{table}.{key}", result=f"deleted={n}")
        flash(f"Удалено строк: {n}" if n else "Строка не найдена", "ok" if n else "error")
        return redirect(url_for("browse", table=table))

    # --- SQL-редактор ---------------------------------------------------------------
    WRITE_KINDS = ("UPDATE", "DELETE")

    def classify(sql: str) -> str:
        s = sql.strip().upper().rstrip(";")
        for kw in ("DROP", "ALTER", "CREATE", "VACUUM", "PRAGMA", "ATTACH", "INSERT", "REPLACE"):
            if s.startswith(kw):
                return kw
        for kw in WRITE_KINDS + ("SELECT", "WITH"):
            if s.startswith(kw):
                return kw.split()[0]
        return "OTHER"

    @app.route("/sql", methods=["GET", "POST"])
    def sql_page():
        result = None
        preview = None
        raw = request.form.get("sql") if request.method == "POST" else None
        if request.method == "POST":
            sql = (raw or "").strip().rstrip(";")
            kind = classify(sql)
            if kind in ("DROP", "ALTER", "CREATE", "VACUUM", "PRAGMA", "ATTACH", "OTHER"):
                flash(f"Операция «{kind}» запрещена в редакторе.", "error")
                audit("sql_denied", details=sql, result=f"blocked:{kind}")
            elif kind == "INSERT":
                # INSERT выполняется только после подтверждения (как UPDATE/DELETE):
                # защита от случайной вставки дубликатов/мусора.
                token = request.form.get("confirm_token")
                stored = session.get("pending_sql")
                if token and stored and secrets.compare_digest(str(token), str(stored[1])) \
                        and stored[0] == sql:
                    with engine.begin() as conn:
                        cur = conn.execute(text(sql))
                        n = cur.rowcount
                    session.pop("pending_sql", None)
                    audit("sql_insert", details=sql, result=f"rows={n}")
                    flash(f"INSERT выполнен, добавлено строк: {n}.", "ok")
                else:
                    n_rows, err = _insert_preview(sql)
                    if err:
                        preview = {"sql": sql, "n": -1, "token": "", "error": err}
                    else:
                        tok = secrets.token_hex(16)
                        session["pending_sql"] = (sql, tok)
                        preview = {"sql": sql, "n": int(n_rows or 0), "token": tok, "error": None}
            elif kind in WRITE_KINDS:
                token = request.form.get("confirm_token")
                stored = session.get("pending_sql")
                if token and stored and secrets.compare_digest(str(token), str(stored[1])) \
                        and stored[0] == sql:
                    with engine.begin() as conn:
                        cur = conn.execute(text(sql))
                        n = cur.rowcount
                    session.pop("pending_sql", None)
                    audit(f"sql_{kind.lower()}", details=sql, result=f"rows={n}")
                    flash(f"{kind} выполнен, затронутых строк: {n}.", "ok")
                else:
                    if " WHERE " not in (" " + sql.upper() + " "):
                        flash(f"{kind} без WHERE запрещён (защита от стирания всей таблицы).", "error")
                        audit(f"sql_{kind.lower()}_denied", details=sql, result="no_where")
                    else:
                        # Подсчёт затрагиваемых строк через обёртку SELECT COUNT(*).
                        try:
                            count_sql = f"SELECT COUNT(*) FROM ({_select_for_count(kind, sql)}) AS _t"
                            tok = secrets.token_hex(16)
                            with engine.connect() as conn:
                                n = conn.execute(text(count_sql)).scalar()
                            session["pending_sql"] = (sql, tok)
                            preview = {"sql": sql, "n": int(n or 0), "token": tok, "error": None}
                        except Exception as e:  # показать синтаксическую ошибку до подтверждения
                            preview = {"sql": sql, "n": -1, "token": "", "error": str(e)}
            else:  # SELECT / WITH
                try:
                    with engine.connect() as conn:
                        rs = conn.execute(text(sql))
                        if rs.returns_rows:
                            result = [dict(r) for r in rs.mappings().all()]
                except Exception as e:
                    flash(f"Ошибка SQL: {e}", "error")
                audit("sql_select", details=sql[:2000], result="ok")
        return render(
            "sql", page="sql", raw=raw, result=result, preview=preview,
        )

    def _select_for_count(kind: str, sql: str) -> str:
        """Извлекает табличное выражение для подсчёта затрагиваемых строк."""
        if kind == "DELETE":
            m = re.match(r"(?is)^DELETE\s+FROM\s+(\S+)(.*)$", sql)
            return f"* FROM {m.group(1)}{m.group(2)}"
        m = re.match(r"(?is)^UPDATE\s+(\S+)\s+SET\s+.+?(\s+WHERE\s+.*)?$", sql)
        where = m.group(2) or ""
        return f"* FROM {m.group(1)}{where}"

    def _insert_preview(sql: str) -> tuple[int | None, str]:
        """Оценка последствий INSERT до подтверждения: (число вставляемых рядов, ошибка)."""
        m = re.match(
            r"(?is)^INSERT\s+(?:OVERWRITE\s+)?INTO\s+\S+\s*(.*)$", sql.strip())
        if not m:
            return None, "Не удалось разобрать INSERT для предпросмотра."
        body = m.group(1).strip()
        # отбрасываем список колонок перед VALUES/SELECT
        mm = re.match(r"(?is)^\((?:[^()]|\([^()]*\))*\)\s*", body)
        rest = body[mm.end():] if mm else body
        # VALUES (...), (...) — считаем число кортежей верхнего уровня
        vm = re.search(r"(?is)\bVALUES\b(.*)$", rest)
        pre = re.split(r"(?is)\bVALUES\b", rest)[0].strip()
        try:
            with engine.connect() as conn:
                if vm and not pre:  # чистый VALUES-форм
                    txt = vm.group(1)
                    depth, tuples, i = 0, 0, 0
                    while i < len(txt):
                        c = txt[i]
                        if c == "'":  # пропуск строковых литералов
                            i += 1
                            while i < len(txt) and txt[i] != "'":
                                i += 2 if txt[i] == "\\" else 1
                        elif c == "(":
                            depth += 1
                            if depth == 1:
                                tuples += 1
                        elif c == ")":
                            depth -= 1
                        i += 1
                    return max(tuples, 1), ""
                # INSERT ... SELECT — считаем через обёртку над SELECT-частью
                sm = re.search(r"(?is)\bSELECT\b(.*)$", rest)
                if not sm:
                    return None, "Не распознана форма INSERT (VALUES/SELECT)."
                n = conn.execute(text(f"SELECT COUNT(*) FROM (SELECT{sm.group(1)}) AS _t")).scalar()
                return int(n or 0), ""
        except Exception as e:  # показать синтаксическую ошибку до подтверждения
            return None, str(e)

    # --- аудит-страница --------------------------------------------------------------
    @app.route("/audit")
    def audit_page():
        if "audit_log" not in tables():
            flash("Таблица audit_log отсутствует — выполните миграции («Действия»).", "error")
            return render("audit", page="audit", rows=[], cur_page=1, pages=1)
        page = max(1, int(request.args.get("page") or 1))
        per = 100
        with engine.connect() as conn:
            total = conn.execute(text("SELECT COUNT(*) FROM audit_log")).scalar() or 0
            rows = conn.execute(
                text("SELECT * FROM audit_log ORDER BY id DESC LIMIT :l OFFSET :o"),
                {"l": per, "o": (page - 1) * per},
            ).mappings().all()
        return render(
            "audit", page="audit", rows=rows, cur_page=page, pages=max(1, -(-total // per)),
        )

    # --- импорт ответа нейросети (психопрофиль) ------------------------------
    @app.route("/profile/import", methods=["GET", "POST"])
    def profile_import():
        if "user_profiles" not in tables():
            flash("Таблица user_profiles отсутствует — сначала запустите сбор.", "error")
            return render("profile_import", page="profimport", users=[],
                          sel=None, model=None, period_from=None,
                          period_to=None, messages=None, answer=None)
        with engine.connect() as conn:
            users = conn.execute(text(
                "SELECT id, username, priority FROM user_profiles "
                "ORDER BY priority DESC, username"
            )).mappings().all()
        form = {k: request.form.get(k, "") for k in
                ("user_id", "model", "period_from", "period_to", "messages", "answer")}
        if request.method == "POST":
            try:
                from app.services.analysis_importer import (
                    import_analysis, parse_llm_answer,
                )

                parsed = parse_llm_answer(form["answer"])
                pid = import_analysis(
                    engine,
                    user_id=int(form["user_id"]),
                    answer=parsed,
                    model=form["model"] or None,
                    period_from=(parse_field("datetime", form["period_from"], True)
                                 if form["period_from"] else None),
                    period_to=(parse_field("datetime", form["period_to"], True)
                               if form["period_to"] else None),
                    messages_analyzed=(int(form["messages"])
                                       if form["messages"].isdigit() else None),
                )
                audit("psych_import", target=f"user={form['user_id']}",
                      details=f"profile_id={pid}")
                flash(f"Импортировано: профиль #{pid}.", "ok")
                return redirect(url_for("profile_view", pid=pid))
            except Exception as e:
                flash(f"Ошибка импорта: {e}", "error")
        return render("profile_import", page="profimport", users=users,
                      sel=form.get("user_id"), model=form.get("model"),
                      period_from=form.get("period_from"),
                      period_to=form.get("period_to"),
                      messages=form.get("messages"), answer=form.get("answer"))

    # --- автообновление психопрофиля через LLM API ----------------------------
    # Data Quality Gate: минимальные пороги данных для генерации профиля.
    # Ниже — не генерируем вовсе; между gate и warn — предупреждаем, но пускаем.
    PSYCH_GATE_MIN_MSGS = int(os.getenv("PSYCH_GATE_MIN_MSGS", "30"))
    PSYCH_GATE_MIN_DAYS = float(os.getenv("PSYCH_GATE_MIN_DAYS", "4"))
    PSYCH_WARN_MIN_MSGS = int(os.getenv("PSYCH_WARN_MIN_MSGS", "50"))
    PSYCH_WARN_MIN_DAYS = float(os.getenv("PSYCH_WARN_MIN_DAYS", "14"))

    def _msg_coverage(user_id) -> dict:
        """Дешёвая сводка покрытия данными (для превью в карточке)."""
        out = {"msgs": 0, "days": None, "dm": 0}
        if "messages" not in tables():
            return out
        try:
            mt = Table("messages", MetaData(), autoload_with=engine)
            with engine.connect() as conn:
                r = conn.execute(sa_select(
                    func.count(),
                    func.coalesce(
                        func.extract("epoch",
                                     func.max(mt.columns.timestamp)
                                     - func.min(mt.columns.timestamp)), 0),
                ).where(mt.columns.user_id == user_id)).one()
                out["msgs"] = int(r[0] or 0)
                out["days"] = round(float(r[1] or 0) / 86400, 2) or None
                if "dm_fetch" in [c["name"] for c in inspect(engine).get_columns("user_profiles")]:
                    upt = Table("user_profiles", MetaData(), autoload_with=engine)
                    out["dm"] = int(conn.execute(sa_select(upt.columns.dm_messages_total).where(
                        upt.columns.user_id == user_id)).scalar() or 0)
        except Exception:  # noqa: BLE001 — превью не должно ронять страницу
            pass
        return out

    def _psych_gate(msgs: int, days, dm: int) -> dict:
        """Вердикт гейта по числам покрытия. days может быть None/0 (1 сообщение)."""
        reasons = []
        if msgs < PSYCH_GATE_MIN_MSGS:
            reasons.append(f"сообщений {msgs} < минимума {PSYCH_GATE_MIN_MSGS}")
        if days is None or days < PSYCH_GATE_MIN_DAYS:
            reasons.append(f"окно наблюдения "
                           f"{days if days is not None else 0:.1f} дн. "
                           f"< минимума {PSYCH_GATE_MIN_DAYS:g} дн.")
        blocked = bool(reasons)
        warnings = []
        if not blocked:
            if msgs < PSYCH_WARN_MIN_MSGS:
                warnings.append(f"мало сообщений ({msgs} < {PSYCH_WARN_MIN_MSGS}) "
                                f"— вывод будет низкой надёжности")
            if days is not None and days < PSYCH_WARN_MIN_DAYS:
                warnings.append(f"короткое окно наблюдения ({days:.1f} < "
                                f"{PSYCH_WARN_MIN_DAYS:g} дн.) — выводы могут "
                                f"не отражать устойчивые черты")
            if dm == 0:
                warnings.append("нет ЛС-контекста — видна только «публичная маска»")
        return {"blocked": blocked, "reasons": reasons, "warnings": warnings,
                "gate_msgs": PSYCH_GATE_MIN_MSGS, "gate_days": PSYCH_GATE_MIN_DAYS,
                "warn_msgs": PSYCH_WARN_MIN_MSGS, "warn_days": PSYCH_WARN_MIN_DAYS,
                "msgs": msgs, "days": days, "dm": dm}

    @app.route("/psych/gate/<int:user_id>")
    def psych_gate(user_id: int):
        """JSON-превью покрытия данными для кнопки 🧠 (без сборки промпта)."""
        cov = _msg_coverage(user_id)
        return jsonify(_psych_gate(cov["msgs"], cov["days"], cov["dm"]))

    def _psych_gate_pre(user_id) -> dict:
        """Вердикт гейта по дешёвой сводке (для превью в карточке)."""
        cov = _msg_coverage(user_id)
        return _psych_gate(cov["msgs"], cov["days"], cov["dm"])

    @app.route("/psych/refresh/<int:user_id>", methods=["POST"])
    def psych_refresh(user_id: int):
        """Кнопка «Обновить психо профиль»: БД -> промпт -> бесплатный LLM API -> запись.

        1) profile_export.build_prompt_for_user собирает «дело» пользователя
           из PostgreSQL и готовый markdown-промпт (та же сборка, что и CLI);
        2) app.services.llm_client.analyze_prompt прогоняет его через OpenRouter
           (модель из LLM_MODEL, по умолчанию deepseek/deepseek-chat-v3.1:free);
        3) analysis_importer.import_analysis сохраняет ответ как новую версию
           psych_profiles (старая -> архив), сигналы и интересы — с дедупом.
        """
        if "user_profiles" not in tables():
            abort(404)
        up = Table("user_profiles", MetaData(), autoload_with=engine)
        with engine.connect() as conn:
            uname = conn.execute(sa_select(up.columns.username).where(
                up.columns.user_id == user_id)).scalar()
        if uname is None:
            abort(404)
        audit("psych_refresh_start", target=f"user={user_id}", result="start")
        try:
            from app.services import llm_client
            from app.services.profile_export import build_prompt_for_user

            info = build_prompt_for_user(
                app.config["DATABASE_URL"], user_id)

            # Data Quality Gate: блокируем генерацию на тощих данных ДО запроса
            # к LLM (сообщения/дни — из собранного дела, точные числа).
            gate = _psych_gate(info.get("messages_total") or 0,
                               info.get("span_days"),
                               info.get("dm_messages_total") or 0)
            if gate["blocked"]:
                audit("psych_refresh", target=f"user={user_id}",
                      result="blocked_by_gate")
                flash("Данных недостаточно для психо профиля: "
                      + "; ".join(gate["reasons"])
                      + f". Пороги: ≥{gate['gate_msgs']} сообщений и окно "
                        f"≥{gate['gate_days']:g} дн. Продолжайте сбор — "
                        f"циклы подтянут историю.", "error")
                return redirect(url_for("browse", table="user_profiles",
                                        col="user_id", val=str(user_id)))
            answer, model_used, ph = llm_client.analyze_prompt(info["prompt"])

            from app.services.analysis_importer import import_analysis

            # версионирование методики: хэш промпта + вес дела — чтобы на
            # вопрос «данные изменились или метод?» отвечал факт, а не память
            pid = import_analysis(
                engine, user_id, answer,
                model=model_used, prompt_hash=ph,
                source_lines=info["messages_total"],
                period_from=info["period_from"], period_to=info["period_to"],
                messages_analyzed=info["messages_total"],
                methodology={"prompt_sha256": ph,
                             "prompt_bytes": len(info["prompt"].encode("utf-8")),
                             "behavior_block": bool(info.get("behavior_present")),
                             "window_days": info.get("window_days")},
            )
            audit("psych_refresh", target=f"user={user_id}",
                  details=f"profile_id={pid} model={model_used} "
                          f"msgs={info['messages_total']}",
                  result="ok")
            flash(f"Психо профиль обновлён: запись #{pid} "
                  f"(модель {model_used}, сообщений {info['messages_total']}).",
                  "ok")
            # предупреждения гейта (между gate и warn-порогами) — после успеха
            if gate.get("warnings"):
                for w in gate["warnings"]:
                    flash(f"⚠ Низкая надёжность профиля: {w}", "warn")
            return redirect(url_for("profile_view", pid=pid))
        except Exception as e:  # noqa: BLE001
            audit("psych_refresh", target=f"user={user_id}", result=f"error: {e}")
            flash(f"Не удалось обновить психо профиль: {e}", "error")
            return redirect(url_for("browse", table="user_profiles",
                                    col="user_id", val=str(user_id)))

    # --- ручная вставка ответа модели ---------------------------------------- #
    PSCH_SAMPLE = ('{"big_five":{"openness":0,"conscientiousness":0,'
        '"extraversion":0,"agreeableness":0,"neuroticism":0},'
        '"mbti":{"type":"","alt":[],"confidence":0.0,"reasoning":""},'
        '"enneagram":{"type":"","alt":[],"confidence":0.0,"reasoning":""},'
        '"interests":[{"topic":"","evidence":""}],'
        '"risk_flags":[{"flag":"","evidence":"","severity":"low|med|high"}],'
        '"evidence":[{"signal":"","quote":""}],'
        '"confidence_overall":0.0,"disclaimer":""}')

    @app.route("/psych/manual/<int:user_id>")
    def psych_manual(user_id: int):
        """Форма ручной вставки ответа модели с проверкой структуры."""
        if "user_profiles" not in tables():
            abort(404)
        up = Table("user_profiles", MetaData(), autoload_with=engine)
        with engine.connect() as conn:
            r = conn.execute(sa_select(up.columns.id, up.columns.username).where(
                up.columns.user_id == user_id)).first()
        if r is None:
            abort(404)
        return render("psych_manual", page="browse", uid=user_id, upk=r[0],
                      answer=None, model_name=None, result=None, do_import="no", ph_sample=PSCH_SAMPLE)

    def _psch_validate_form():
        """Общий разбор POST-параметров формы ручной вставки.

        Возвращает (user_id, answer_text, model_name, do_action, result|None, err|None);
        result — dict для шаблона, err — сообщение о неверном user_id (уже готовый flash-редирект).
        """
        try:
            uid = int(request.form.get("user_id") or "")
        except ValueError:
            return None, "", "", "validate", None, "Не указан корректный user_id."
        answer_text = request.form.get("answer") or ""
        model_name = (request.form.get("model") or "").strip() or None
        do_action = "import" if request.form.get("do") == "import" else "validate"
        result = None
        try:
            from app.services.llm_client import extract_json
            from app.services.analysis_importer import parse_llm_answer, validate_answer

            data = parse_llm_answer(extract_json(answer_text))
            errs, warns = validate_answer(data)
            result = {
                "ok": not errs,
                "errors": errs,
                "warns": warns,
                "mbti": ((data.get("mbti") or {}).get("type")) or "—",
                "enneagram": ((data.get("enneagram") or {}).get("type")) or "—",
                "signals": len(data.get("evidence") or []),
                "interests": len(data.get("interests") or []),
                "risks": len(data.get("risk_flags") or []),
                "conf": data.get("confidence_overall"),
            }
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "errors": [f"JSON не разобран: {e}"],
                      "warns": [], "mbti": "—", "enneagram": "—",
                      "signals": 0, "interests": 0, "risks": 0, "conf": None}
        return uid, answer_text, model_name, do_action, result, None

    @app.route("/psych/validate", methods=["POST"])
    def psych_validate():
        """Проверить (и при do=import — импортировать) вставленный ответ модели."""
        uid, answer_text, model_name, do_action, result, err = _psch_validate_form()
        if err:
            flash(err, "error")
            return redirect(url_for("dashboard"))
        if do_action != "import":
            # только проверка структуры — остаёмся на форме
            return render("psych_manual", page="browse", uid=uid, upk=uid,
                          answer=answer_text, model_name=model_name,
                          result=result, do_import="no", ph_sample=PSCH_SAMPLE)
        # ---- импорт новой версии профиля ----
        if not result["ok"]:
            audit("psych_import_manual", target=f"user={uid}",
                  details=f"rejected: {result['errors'][0][:200]}", result="error")
            flash("Импорт отклонён: структура ответа некорректна "
                  f"(первая ошибка: {result['errors'][0]}).", "error")
            return render("psych_manual", page="browse", uid=uid, upk=uid,
                          answer=answer_text, model_name=model_name,
                          result=result, do_import="yes", ph_sample=PSCH_SAMPLE)
        try:
            import hashlib

            from app.services.analysis_importer import parse_llm_answer, import_analysis

            data = parse_llm_answer(answer_text)
            ph = hashlib.sha256(answer_text.encode("utf-8")).hexdigest()[:32]
            pid = import_analysis(engine, uid, data,
                                  model=model_name or "manual-paste",
                                  prompt_hash=ph)
            audit("psych_import_manual", target=f"user={uid}",
                  details=f"profile_id={pid} model={model_name or 'manual-paste'}",
                  result="ok")
            flash(f"Психо профиль импортирован: новая версия #{pid}.", "ok")
            return redirect(url_for("profile_view", pid=pid))
        except Exception as e:  # noqa: BLE001
            audit("psych_import_manual", target=f"user={uid}", result=f"error: {e}")
            flash(f"Ошибка импорта: {e}", "error")
            return render("psych_manual", page="browse", uid=uid, upk=uid,
                          answer=answer_text, model_name=model_name,
                          result=result, do_import="yes", ph_sample=PSCH_SAMPLE)

    @app.route("/profile/<int:pid>")
    def profile_view(pid: int):
        if "psych_profiles" not in tables():
            abort(404)
        pp = Table("psych_profiles", MetaData(), autoload_with=engine)
        with engine.connect() as conn:
            p = conn.execute(sa_select(pp).where(pp.columns.id == pid)).mappings().first()
        if p is None:
            abort(404)
        try:
            # result_json — JSONB: psycopg отдаёт dict напрямую; строка поддержана
            if isinstance(p.result_json, dict):
                res = p.result_json
            else:
                res = json.loads(p.result_json) if p.result_json else {}
        except Exception:
            res = {}
        bf = {
            "openness": p.big_five_openness,
            "conscientiousness": p.big_five_conscientiousness,
            "extraversion": p.big_five_extraversion,
            "agreeableness": p.big_five_agreeableness,
            "neuroticism": p.big_five_neuroticism,
        }
        username = None
        try:
            up = Table("user_profiles", MetaData(), autoload_with=engine)
            with engine.connect() as conn:
                r = conn.execute(sa_select(up.columns.username).where(
                    up.columns.user_id == p.user_id)).scalar()
            username = r
        except Exception:
            pass
        signals, interests = [], []
        if "psych_signals" in tables():
            t2 = Table("psych_signals", MetaData(), autoload_with=engine)
            with engine.connect() as conn:
                signals = conn.execute(
                    sa_select(t2).where(t2.columns.profile_id == pid)
                    .order_by(t2.columns.id)
                ).mappings().all()
        if "user_interests" in tables():
            t3 = Table("user_interests", MetaData(), autoload_with=engine)
            with engine.connect() as conn:
                interests = conn.execute(
                    sa_select(t3).where(t3.columns.user_id == p.user_id)
                    .order_by(t3.columns.evidence_count.desc())
                ).mappings().all()
        risks = (res.get("risk_flags") or []) if isinstance(res, dict) else []
        return render(
            "profile_view", page="browse", p=p, res=res, bf=bf,
            username=username, signals=signals, interests=interests, risks=risks,
            raw_json_pretty=_json_pretty(p.result_json),
            can_refresh=("user_profiles" in tables()),
        )

    # --- действия ---------------------------------------------------------------------
    @app.route("/actions", methods=["GET", "POST"])
    def actions_page():
        last = session.get("last_action")
        if request.method == "POST":
            op = request.form.get("op", "vacuum")
            if op == "vacuum":
                # PostgreSQL: VACUUM нельзя выполнять внутри транзакции —
                # отдельное соединение в режиме AUTOCOMMIT.
                with engine.connect().execution_options(
                    isolation_level="AUTOCOMMIT"
                ) as conn:
                    conn.execute(text("VACUUM"))
                    conn.execute(text("ANALYZE"))
                audit("vacuum_analyze", result="ok")
                last = "VACUUM/ANALYZE выполнены."
            session["last_action"] = last
            flash(last, "ok")
            return redirect(url_for("actions_page"))
        return render("actions", page="actions", last=last)

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="Веб-админка поверх БД профайлера")
    ap.add_argument("--db", default=None, help="PostgreSQL URL (иначе ENV DATABASE_URL)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8777)
    args = ap.parse_args()

    load_dotenv(BASE_DIR / ".env")
    from app.models.db import DEFAULT_DATABASE_URL

    db = args.db or os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
    app = create_app(db)  # внутри — fail-fast, если это не PostgreSQL
    print(f"Admin UI: http://{args.host}:{args.port}  (db={db})")
    app.run(host=args.host, port=args.port, debug=False)
