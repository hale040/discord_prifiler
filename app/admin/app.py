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
from zoneinfo import ZoneInfo, available_timezones
from urllib.parse import quote_plus, urlencode

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
from sqlalchemy import and_ as sa_and, or_ as sa_or, update as sa_update, insert as sa_insert, delete as sa_delete
from sqlalchemy import text as sa_text
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import CHAR, VARCHAR
from markupsafe import Markup
from sqlalchemy.engine import Engine

BASE_DIR = Path(__file__).resolve().parents[2]


def utcnow() -> datetime:
    """Текущее время UTC (совпадает с helpers.utcnow в моделях)."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Часовой пояс отображения (настройка в шапке, сохраняется в session['tz']).   #
# В БД всё хранится в UTC; преобразование — только на слое отображения.        #
# --------------------------------------------------------------------------- #
TZ_DEFAULT = "Europe/Moscow"
TZ_POPULAR = [
    "Europe/Moscow", "Europe/Kyiv", "Europe/Berlin", "Europe/London",
    "Asia/Almaty", "Asia/Tashkent", "Asia/Dubai", "Asia/Shanghai",
    "Asia/Tokyo", "America/New_York", "America/Chicago",
    "America/Los_Angeles", "America/Sao_Paulo", "Australia/Sydney",
    "UTC",
]


def tz_list() -> list[str]:
    """Популярные пояса первыми + полный список IANA для поиска браузером."""
    full = sorted(t for t in available_timezones() if t not in TZ_POPULAR)
    return TZ_POPULAR + full


# вычисляется один раз при импорте (~600 элементов, поиск в браузере нативный)
TZ_ALL: list[str] = tz_list()



# единый формат отображения времени в админке: ДД.ММ.ГГГГ ЧЧ:ММ:СС
DT_FMT = "%d.%m.%Y %H:%M:%S"


def to_local(v, tzname: str | None):
    """datetime (aware или naive-UTC) -> строка 'ДД.ММ.ГГГГ ЧЧ:ММ:СС' в поясе tzname.

    Naive-значения трактуются как UTC (в БД timestamp with tz, но часть путей
    отдаёт naive). Пустой/не-дата — без изменений. Без выбора пояса — как раньше.
    """
    if v is None or not isinstance(v, datetime) or not tzname:
        return v
    try:
        z = ZoneInfo(tzname)
    except Exception:
        return v
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(z).strftime(DT_FMT)


# --------------------------------------------------------------------------- #
# Какие столбцы показывать в лентах таблиц («только нужные»).                 #
# Список — порядок отображения; всё остальное скрыто из ленты, но доступно    #
# целиком на странице записи. ENV-надстройки (формат "таблица:столбец"):      #
#   ADMIN_EXTRA_COLUMNS=messages:reactions_json,...  — добавить в ленту       #
#   ADMIN_HIDE_COLUMNS=user_profiles:username,...    — убрать из ленты        #
# --------------------------------------------------------------------------- #
# Ленты админки — UI-контракт: НЕ больше 6 видимых колонок на таблицу.
# Всё остальное видно в карточке записи («открыть ▸»). Надстройка/скрытие —
# ENV ADMIN_EXTRA_COLUMNS / ADMIN_HIDE_COLUMNS ('таблица:колонка,...').
VIEW_COLUMNS: dict[str, list[str]] = {
    "messages": ["id", "timestamp", "author_id", "clean_content"],
    "user_profiles": ["id", "user_id", "username", "last_updated"],
    "guild_member_profiles": ["id", "user_id", "nick", "joined_at"],
    "user_roles": ["id", "user_id", "role_id"],
    "attachments": ["id", "message_id", "filename", "size"],
    "channel_states": ["channel_id", "messages_total", "last_poll_at"],
    "audit_log": ["id", "created_at", "action", "target", "result"],
    # --- новый функционал (миграции v5..v8) ---------------------------------
    "user_games": ["id", "user_id", "name", "last_seen"],
    "profile_snapshots": ["id", "user_id", "taken_at", "status_text"],
    "psych_profiles": ["id", "user_id", "mbti_type", "severity_max",
                       "created_at"],
    "psych_signals": ["id", "user_id", "signal_key", "severity"],
    "user_interests": ["id", "user_id", "topic", "evidence_count"],
    # --- серверы / чаты / анкеты (миграция a00000000006) --------------------
    "guilds": ["guild_id", "name"],
    "chats": ["channel_id", "name", "collects_qa", "messages_total"],
    "chat_members": ["author_id", "questionnaire", "updated_at"],
}

# Человекочитаемые заголовки колонок в лентах (сырые имена — в title-th).
COL_LABELS: dict[str, str] = {
    "id": "#", "pk": "#",
    "user_id": "user", "author_id": "автор", "message_id": "msg",
    "role_id": "роль", "guild_id": "гильдия", "channel_id": "канал",
    "username": "ник", "nick": "ник", "global_name": "имя",
    "clean_content": "сообщение", "content": "текст", "filename": "файл",
    "name": "название", "topic": "топик", "title": "заголовок",
    "size": "размер", "kind": "тип", "action": "действие", "target": "объект",
    "result": "результат", "severity": "sev", "severity_max": "max sev",
    "signal_key": "сигнал", "mbti_type": "MBTI", "status_text": "статус",
    "messages_total": "всего", "evidence_count": "evid",
    "timestamp": "время", "created_at": "создано", "last_updated": "обновлён",
    "joined_at": "вступил", "last_seen": "виден", "taken_at": "снимок",
    "last_poll_at": "поллинг",
    "guild_id": "сервер", "channel_id": "чат", "collects_qa": "анкеты",
    "questionnaire": "анкета", "updated_at": "обновлено",
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
        """(текст, is_json) для отображения; длинные значения сворачиваются.

        datetime приводится к формату ДД.ММ.ГГГГ ЧЧ:ММ:СС в выбранном в шапке
        поясу (session['tz'] или ENV ADMIN_TZ); без пояса — тоже нормализуем
        формат (UTC), чтобы нигде не осталось '2026-10-27 03:18:41.008798+00:00'."""
        if isinstance(v, datetime):
            _tzf = session.get("tz") or os.getenv("ADMIN_TZ", "") or "UTC"
            v = to_local(v, _tzf)
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
        # UI-контракт: максимум 6 видимых колонок (PK + 5). unknown-таблица
        # без VIEW_COLUMNS дала бы 20+ колонок — «каша», которую никакая
        # раскладка не спасает. ADMIN_EXTRA_COLUMNS тоже под этим лимитом.
        if len(chosen) > 6:
            chosen = chosen[:6]
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
            sel_cols = [pp.columns.id, pp.columns.model, pp.columns.created_at]
            # summary берём из result_json (JSONB), если колонка есть — чтобы
            # в ленте профилей показывать фрагмент «Итогового резюме» без N+1.
            has_rj = "result_json" in pp.columns
            if has_rj:
                sel_cols.append(pp.columns.result_json)
            with engine.connect() as conn:
                r = conn.execute(
                    sa_select(*sel_cols)
                    .where(pp.columns.user_id == int(user_id),
                           pp.columns.is_current.is_(True))
                    .order_by(pp.columns.created_at.desc())
                    .limit(1)
                ).first()
            if not r:
                return None
            out = {"id": r[0], "model": r[1], "created_at": r[2]}
            summary = ""
            if has_rj and isinstance(r[3], dict):
                summary = str(r[3].get("summary") or "")
            out["summary"] = summary
            return out
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
            # summary для карточки пользователя: берём фрагмент из result_json
            has_rj = "result_json" in cols
            if has_rj:
                sel_cols.append(pp.columns.result_json)
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
                        entry = {"id": r[1], "mbti_type": (r[2] if len(r) > 2 else None)}
                        if has_rj and isinstance(r[-1], dict):
                            entry["summary"] = str(r[-1].get("summary") or "")[:140]
                        out[k] = entry
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
            # Принимает отображаемый формат 'ДД.ММ.ГГГГ ЧЧ:ММ[:СС]', и naive
            # ('2026-10-24 12:59'), и вывод PostgreSQL ('2023-10-03 15:52:28.201000+00:00'):
            # T->пробел, Z->+00:00, fromisoformat (понимает смещения), strptime-форматы.
            s = raw.strip().replace("T", " ")
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            try:
                return _dt.fromisoformat(s)
            except ValueError:
                pass
            s2 = s.split("+")[0].strip()  # отбросить смещение как последний шанс
            for fmt in ("%d.%m.%Y %H:%M:%S.%f", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
                        "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    return _dt.strptime(s2, fmt)
                except ValueError:
                    continue
            raise ValueError(f"ожидается дата 'ДД.ММ.ГГГГ ЧЧ:ММ[:СС]', получено {raw!r}")
        return raw

    def render(tpl: str, **ctx):
        """Страница = layout.html с подставленным содержимым templates/admin/<tpl>.html."""
        # current_psych доступен шаблонам (карточка пользователя показывает
        # кнопку 🧠 «Обновить», только если профиль уже строился)
        ctx.setdefault("current_psych", current_psych)
        # локальное время для шаблонов: {{ fmtdt(...) }} — по поясу из шапки;
        # без пояса — формат ДД.ММ.ГГГГ ЧЧ:ММ:СС в UTC (не сырая строка БД)
        _tzv = session.get("tz") or os.getenv("ADMIN_TZ", "") or "UTC"
        ctx.setdefault("fmtdt", lambda v: to_local(v, _tzv))
        # безопасный рендер значения в ячейку: None -> '—' (иначе Jinja печатает
        # 'None', и колонки с разными «пустыми» выглядят разъехавшимися)
        ctx.setdefault("v_or", lambda v: v if v not in (None, "") else "—")

        # снегфлейк Discord -> дата создания объекта (snowflake epoch 2015-01-01).
        # Экономит колонку created_at_discord в ленте user_profiles: никнейм/id
        # остаются, а возраст аккаунта виден прямо в id по наведению.
        _SNOW_EPOCH_MS = 1_420_070_400_000

        def snow_dt(v):
            try:
                ms = (int(v) >> 22) + _SNOW_EPOCH_MS
                return to_local(
                    datetime.fromtimestamp(ms / 1000, tz=timezone.utc), _tzv
                )[:10]
            except Exception:
                return ""

        ctx.setdefault("snowdt", snow_dt)
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
        ("Серверы", [("guilds", "🖥 Серверы"), ("chats", "💬 Чаты")]),
        ("Профили", [("user_profiles", "👤 Пользователи")]),
        ("Психо", [("psych_profiles", "🧠 Психопрофили"),
                  ("psych_signals", "📡 Сигналы"),
                  ("profile_snapshots", "📸 Снимки профилей")]),
        ("Мессенджер", [("messages", "💬 Сообщения"),
                        ("message_revisions", "✏️ Правки"),
                        ("reaction_events", "😀 Реакции"),
                        ("attachments", "📎 Вложения")]),
        ("Контекст", [("guild_member_profiles", "🏛 Участники"),
                      ("presence_snapshots", "🟢 Присутствие"),
                      ("interaction_aggregates", "↔ Взаимодействия"),
                      ("channel_states", "📊 Каналы"),
                      ("user_games", "🎮 Игры"),
                      ("user_interests", "❤️ Интересы"),
                      ("roles", "🛡 Роли"),
                      ("user_roles", "👥 Роли пользователей")]),
    ]

    @app.context_processor
    def inject_nav():
        have = set(tables())
        # ссылки навигации: таблицы -> обычный browse; спец-маршруты (guilds,
        # chats) -> умные страницы, если таблицы новой схемы уже созданы
        href_map = {"guilds": url_for("guilds_page"),
                    "chats": url_for("chats_page")}

        def nav_href(t: str) -> str:
            return href_map.get(t) or url_for("browse", table=t)
        groups = [(title, [(t, label) for t, label in items if t in have])
                  for title, items in NAV_GROUPS]
        groups = [(t, its) for t, its in groups if its]
        grouped = {x for _, its in groups for x, _ in its}
        # служебные таблицы не показываем в «Ещё» даже если они есть в БД
        hidden = {"alembic_version", "schema_version", "audit_log"}
        extra = [t for t in tables() if t not in grouped and t not in hidden]
        return {"nav_tables": tables(), "nav_groups": groups,
                "nav_href": nav_href,
                "nav_extra": extra, "page": "",
                "has_psych": "psych_profiles" in have,
                # выбор часового пояса в шапке (по умолчанию — из ENV ADMIN_TZ)
                "tz_cur": session.get("tz") or os.getenv("ADMIN_TZ", ""),
                "tz_all": TZ_ALL}

    # --- настройка пояса: ?tz=Europe/Moscow | tz=UTC | tz=- (сброс) ---------
    @app.before_request
    def apply_tz_param():
        v = request.args.get("tz")
        if v is None:
            return
        if v in ("-", "none"):
            session.pop("tz", None)
        elif v in TZ_ALL:
            session["tz"] = v
        ref = request.referrer or url_for("dashboard")
        # чтобы tz-параметр не «наследовался» при пагинации/фильтрах —
        # пересобираем referrer с текущими параметрами запроса без tz
        try:
            base, _, _old = ref.partition("?")
            qs = [(k, val) for k, vals in request.args.items()
                  if k != "tz" for val in vals]
            ref = f"{base}?{urlencode(qs)}" if qs else base
        except Exception:
            pass
        return redirect(ref)


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
        # Индикатор «коллектор жив»: последний timestamp guild-сообщений.
        last_msg_ts = None
        health = {}
        try:
            with engine.connect() as conn:
                if "messages" in ts:
                    last_msg_ts = conn.execute(text(
                        "SELECT max(timestamp) FROM messages WHERE guild_id IS NOT NULL"
                    )).scalar()
                if "user_profiles" in ts and "pending_dm" in columns_of("user_profiles"):
                    health["dm_pending"] = conn.execute(text(
                        "SELECT count(*) FROM user_profiles WHERE pending_dm IS TRUE"
                    )).scalar() or 0
                if "user_profiles" in ts and "dm_status" in columns_of("user_profiles"):
                    health["dm_stuck"] = conn.execute(text(
                        "SELECT count(*) FROM user_profiles WHERE dm_status IN ('error','failed')"
                    )).scalar() or 0
                if "psych_profiles" in ts and "is_current" in columns_of("psych_profiles"):
                    health["psych_current"] = conn.execute(text(
                        "SELECT count(*) FROM psych_profiles WHERE is_current IS TRUE"
                    )).scalar() or 0
        except Exception:
            pass  # diag-индикаторы не должны ронять дашборд
        return render(
            "dashboard", page="dash", db=app.config["DATABASE_URL"],
            counts=counts, total=total, recent=recent,
            last_msg_ts=last_msg_ts, health=health, now=utcnow(),
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
        uids = set()
        for _c in ("user_id", "author_id"):
            if _c in cols:
                uids |= {r["cells"][cols.index(_c)][0] for r in rows}
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
                "result"}
        NARROW = {pk: "64px"}
        for c in cols:
            if c == pk:
                continue
            if c in ("user_id", "author_id", "message_id", "channel_id", "guild_id",
                     "reply_to_author_id", "msg_author_id", "source_user_id",
                     "target_user_id"):
                NARROW[c] = "150px"   # snowflake 19-значный + ссылка/бейдж
            elif c in ("timestamp", "created_at", "last_updated", "edited_at",
                       "occurred_at", "observed_at", "dm_synced_at", "joined_at",
                       "created_at_discord", "period_from", "period_to",
                       "first_seen", "last_seen", "updated_at", "taken_at",
                       "recorded_at", "last_poll_at"):
                NARROW[c] = "148px"   # фикс. формат ДД.ММ.ГГГГ ЧЧ:ММ:СС
            elif c in ("priority", "bot", "is_system", "mute", "deaf", "emoji",
                       "kind", "status", "mbti_type", "severity",
                       "severity_max", "confidence_overall", "mbti_confidence",
                       "enneagram_type", "premium_type", "dm_fetch",
                       "word_count", "has_attachment", "reaction_total",
                       "is_current", "downloaded", "is_image",
                       "size", "position", "edit_number", "changed",
                       "messages_total", "evidence_count"):
                NARROW[c] = "72px"
            elif c in ("username", "nick", "name", "topic", "action",
                       "signal_key"):
                NARROW[c] = "110px"
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
            prio=prio, has_priority=("priority" in all_cols
                                     and "priority" not in cols),
            pk_col=pk,
            cur=cur, is_psych_profiles=(table == "psych_profiles"),
            uid_map=uid_map, psych_map=psych_map, widths=widths, w_wide=w_wide,
            labels=COL_LABELS,
        )

    # --- карточка записи: просмотр + редактирование -------------------------
    @app.route("/browse/<table>/<pk>", methods=["GET", "POST"])
    def record(table: str, pk: str):
        table = safe_ident(table)
        if table not in tables():
            abort(404)
        _tzsel = session.get("tz") or os.getenv("ADMIN_TZ", "") or "UTC"  # пояс отображения
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
                raw_val = request.form.get(input_name, "")
                # datetime-поля показаны в локальном поясе (см. шапку) —
                # при сохранении переводим обратно в UTC, иначе сдвиг
                # «приморозится» к значению при каждом редактировании
                if kind_by_name[name] == "datetime" and _tzsel:
                    try:
                        _d = parse_field("datetime", raw_val, True)
                        if isinstance(_d, datetime):
                            if _d.tzinfo is None:
                                _d = _d.replace(tzinfo=ZoneInfo(_tzsel))
                            _d = _d.astimezone(timezone.utc)
                            raw_val = _d.strftime("%Y-%m-%d %H:%M:%S.%f")
                    except ValueError:
                        pass  # пусть обычная ветка выдаст понятную ошибку
                try:
                    parsed[name] = parse_field(
                        kind_by_name[name], raw_val,
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
                       else (to_local(values.get(f["name"]), _tzsel)
                             if f["kind"] == "datetime"
                             else values.get(f["name"]))),
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
        # JS-виджет показывает это поле как текст — отдаём локальное время
        # (пояс из шапки), иначе после live-обновления строка возвращается к UTC.
        if isinstance(state.get("dm_synced_at"), datetime):
            state["dm_synced_at"] = to_local(
                state["dm_synced_at"],
                session.get("tz") or os.getenv("ADMIN_TZ", "") or "UTC")
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
                             "questionnaires": int(info.get("questionnaires_injected") or 0),
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
    PSCH_SAMPLE = ('{"summary":"",'
        '"big_five":{"openness":0,"conscientiousness":0,'
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

    # --- серверы → чаты → анкеты (миграция a00000000006) ---------------------- #
    def _qa_tables_ok(*names: str) -> bool:
        have = set(tables())
        return all(t in have for t in names)

    @app.route("/guilds", methods=["GET", "POST"])
    def guilds_page():
        """ID сервера → список чатов. Ручной ввод дополняет перенос из БД."""
        if not _qa_tables_ok("guilds"):
            abort(404)
        g = Table("guilds", MetaData(), autoload_with=engine)
        c = Table("chats", MetaData(), autoload_with=engine)
        with engine.connect() as conn:
            if request.method == "POST":
                raw = (request.form.get("guild_id") or "").strip()
                if raw.isdigit():
                    gid = int(raw)
                    exists = conn.execute(sa_select(g.columns.guild_id).where(
                        g.columns.guild_id == gid)).scalar()
                    if not exists:
                        conn.execute(sa_insert(g).values(
                            guild_id=gid, name=(request.form.get("name") or "").strip()[:256],
                            created_at=utcnow()))
                        audit("guild_add", target=f"guild={gid}", result="ok")
                    conn.commit()
                return redirect(url_for("guilds_page"))
            rows = conn.execute(
                sa_select(g, sa_select(func.count(c.columns.channel_id))
                          .where(c.columns.guild_id == g.columns.guild_id)
                          .correlate(g).scalar_subquery().label("chats_n"),
                          sa_select(func.count())
                          .select_from(c.join(g, g.columns.guild_id == c.columns.guild_id))
                          .where(c.columns.collects_qa.is_(True))
                          .scalar_subquery().label("qa_chats_n"))
                .order_by(g.columns.guild_id.desc())).mappings().all()
        return render("guilds", page="guilds", rows=rows)

    def _qa_add_members(conn, channel_id: int) -> int:
        """Завести участников чата из авторов его сообщений (идемпотентно).

        Вставка с '' вместо NULL: иначе ON CONFLICT DO NOTHING не засчитывает
        уже существующих участников (rowcount=0 выглядело как «галка не
        включается»), а пустая анкета при включении заполняется ниже по
        правилу «первый пост автора = анкета».
        """
        try:
            return conn.execute(sa_text(
                "INSERT INTO chat_members (channel_id, author_id, "
                "questionnaire) "
                "SELECT DISTINCT m.channel_id, m.author_id, '' "
                "  FROM messages m WHERE m.channel_id = :cid "
                "   AND m.author_id IS NOT NULL "
                " ON CONFLICT (channel_id, author_id) DO NOTHING"),
                {"cid": channel_id}).rowcount or 0
        except Exception:  # noqa: BLE001 — старые схемы без таблицы участников
            conn.rollback()
            return -1

    def _qa_autofill(conn, channel_id: int | None = None) -> int:
        """Привязка постов-анкет к участникам чата (см. questionnaire.py).

        Анкета = ПЕРВЫЙ пост автора в чате (ни реплай, ни сообщение треда);
        с ним связываются комментарии из треда. Идемпотентно, ошибки старых
        схем проглатываются.

        Две ловушки, из-за которых галка «анкеты» выглядела снятой:
          * список колонок нельзя брать из information_schema — этот запрос
            вне транзакции с UPDATE на psycopg3 падает, и функция молча
            возвращала 0 (галка есть, анкет нет → сборщик «сбрасывал» чат);
            берём через SQLAlchemy inspect (columns());
          * условие «первый уровень» не имеет права быть пустым, иначе
            анкету ставили первому попавшемуся ответу автора.
        """
        from app.services.profile_export.questionnaire import _first_level_posts

        lvl = _first_level_posts(conn)
        if not lvl:
            return 0
        where_ch = f" AND cm.channel_id = {int(channel_id)}" \
            if channel_id is not None else ""
        sql = sa_text(
            "UPDATE chat_members cm SET message_id = sub.pid, "
            "questionnaire = sub.pcontent, updated_at = now() FROM ("
            "  SELECT DISTINCT ON (cm2.channel_id, cm2.author_id)"
            "         cm2.channel_id AS ch, cm2.author_id AS au,"
            "         m.id AS pid, COALESCE(m.content, '') AS pcontent"
            "    FROM chat_members cm2"
            "    JOIN chats c ON c.channel_id = cm2.channel_id"
            "             AND c.collects_qa"
            "    JOIN messages m ON m.channel_id = cm2.channel_id"
            "             AND m.author_id = cm2.author_id"
            "   WHERE (cm2.questionnaire IS NULL OR btrim(cm2.questionnaire) = '')"
            "     AND cm2.message_id IS NULL"
            f"     {lvl}{where_ch}"
            "   ORDER BY cm2.channel_id, cm2.author_id, m.timestamp ASC"
            ") sub WHERE cm.channel_id = sub.ch AND cm.author_id = sub.au")
        try:
            return conn.execute(sql).rowcount
        except Exception:  # noqa: BLE001 — старые схемы
            return 0

    def _fetch_guild_channels(gid: int) -> list:
        """Один короткий коннект к Discord ради списка каналов сервера.

        Возвращает [(channel_id, name), ...]. Исключения — на совести
        вызывающего (страница чатов показывает предупреждение, не 500).
        """
        import asyncio

        from app.services.discord_gateway import UserbotClient

        async def _run():
            got: list = []
            err: list = []

            async def ready(_cl):
                try:
                    chs = await _cl.fetch_guild_channels(gid)
                except TypeError:
                    # discord.py (или py-self без user-метода) — через guild
                    g = _cl.get_guild(gid)
                    if g is None:
                        err.append(f"сервер {gid} не виден аккаунту")
                        return
                    chs = list(g.channels)
                got.extend((int(getattr(ch, "id", 0)),
                            getattr(ch, "name", "?")) for ch in chs)

            token = os.environ["DISCORD_TOKEN"]
            cl = UserbotClient(token,
                               gateway_callback=None, ready_callback=ready)
            try:
                # Адаптер start() сам выбирает сигнатуру родительского метода
                # (discord.py-self: start(self); старые форки: start(self,token))
                # — это лечит TypeError "start() takes 1 positional argument
                # but 2 were given".
                await asyncio.wait_for(cl.start(), timeout=30)
            finally:
                try:
                    await cl.close()
                except Exception:  # noqa: BLE001
                    pass
            if not got and err:
                raise RuntimeError(err[0])
            return got

        try:
            return asyncio.run(_run())      # чистый цикл для Flask-потока
        except RuntimeError:                # цикл уже крутится — новый поток
            import threading

            box: dict = {}

            def worker():
                try:
                    box["v"] = asyncio.run(_run())
                except Exception as exc:    # noqa: BLE001
                    box["e"] = exc
            t = threading.Thread(target=worker, daemon=True)
            t.start()
            t.join(45)
            if t.is_alive():
                raise TimeoutError("Discord не ответил за 45 с")
            if "e" in box:
                raise box["e"]
            return box.get("v", [])

    def _sync_guild_channels(conn, gid: int, c, m) -> tuple[int, str]:
        """Подтянуть список каналов сервера из Discord в таблицу chats.

        Возвращает (добавлено_новых, сообщение_для_flash). Без этого шага в
        «чатах сервера» видны только каналы, откуда уже собирались сообщения.
        Ошибки сети/токена не валят страницу — только предупреждение.
        """
        token = os.getenv("DISCORD_TOKEN", "")
        if not token:
            return 0, "DISCORD_TOKEN не задан в .env — список каналов из Discord недоступен."
        try:
            channels = _fetch_guild_channels(gid)
            if not channels:
                return 0, ("Каналы не получены: проверьте, что аккаунт user-токена "
                           "состоит на сервере и видит его каналы.")
            existing = {r[0] for r in conn.execute(
                sa_select(c.columns.channel_id).where(c.columns.guild_id == gid))}
            msg_counts = dict(conn.execute(
                sa_select(m.columns.channel_id, func.count())
                .where(m.columns.guild_id == gid).group_by(m.columns.channel_id)))
            new_n = upd_n = 0
            for cid, cname in channels:
                if not cid:
                    continue
                if cid in existing:
                    # подтянуть название, если раньше было заглушкой из БД
                    upd_n += conn.execute(sa_update(c).where(
                        c.columns.channel_id == cid,
                        c.columns.name.is_(None)).values(
                        name=str(cname)[:256])).rowcount
                    continue
                conn.execute(sa_insert(c).values(
                    channel_id=cid, guild_id=gid, name=str(cname)[:256],
                    collects_qa=False, messages_total=msg_counts.get(cid, 0),
                    created_at=utcnow()))
                new_n += 1
            conn.commit()
            audit("chats_sync", target=f"guild={gid}",
                  details=f"new={new_n} total={len(channels)} renamed={upd_n}",
                  result="ok")
            extra = f", названий обновлено {upd_n}" if upd_n else ""
            return new_n, (f"Синхронизировано: каналов {len(channels)}, "
                           f"новых {new_n}{extra}. Отметьте галками чаты, "
                           f"которые нужно обрабатывать.")
        except Exception as exc:  # noqa: BLE001
            return 0, f"Не удалось получить каналы из Discord: {exc}"

    @app.route("/guilds/<int:guild_id>/chats", methods=["GET", "POST"])
    @app.route("/chats/<int:guild_id>", methods=["GET", "POST"])
    @app.route("/chats")
    def chats_page(guild_id: int | None = None):
        """Чаты внутри сервера; галка «анкеты» включает подмешивание анкет."""
        if not _qa_tables_ok("chats", "chat_members"):
            abort(404)
        c = Table("chats", MetaData(), autoload_with=engine)
        cm = Table("chat_members", MetaData(), autoload_with=engine)
        pred = (c.columns.guild_id == guild_id) if guild_id is not None else None
        with engine.connect() as conn:
            if request.method == "POST" and guild_id is not None:
                # Отложенный запрос: НЕ вешаем HTTP-цикл на коннект к Discord.
                # Ставим сигнал sync_requested — сборщик (channels-sync loop)
                # выполнит получение и напишет статус «получено/ошибка».
                if "sync_requested" in set(c.columns.keys()):
                    n = conn.execute(sa_update(c).where(
                        c.columns.guild_id == guild_id).values(
                        sync_requested=True, sync_error=None)).rowcount
                    conn.commit()
                    audit("chats_sync_request",
                          target=f"guild={guild_id}",
                          details=f"rows={n}", result="ok")
                    if n:
                        flash("Запрошено: каналы сервера будут обновлены "
                              "сборщиком в течение ~30 секунд. Статус — "
                              "в колонке «синхронизация».", "ok")
                    else:
                        flash("Запрошено: чатов этого сервера в списке ещё нет "
                              "— после получения сборщик создаст их сам "
                              "(галки анкет расставьте потом).", "warn")
                else:  # старая схема без колонок статуса — прежний путь
                    m = Table("messages", MetaData(), autoload_with=engine)
                    _new, msg = _sync_guild_channels(conn, guild_id, c, m)
                    flash(msg, "ok" if "Синхронизировано" in msg else "warn")
                return redirect(url_for("chats_page", guild_id=guild_id))
            sel = (sa_select(
                c,
                sa_select(func.count()).select_from(cm)
                .where(cm.columns.channel_id == c.columns.channel_id)
                .correlate(c).scalar_subquery().label("members_n"),
                sa_select(func.count()).select_from(cm)
                .where(cm.columns.channel_id == c.columns.channel_id,
                       cm.columns.questionnaire.isnot(None),
                       func.btrim(cm.columns.questionnaire) != "")
                .correlate(c).scalar_subquery().label("qa_n"))
                .order_by(c.columns.messages_total.desc()))
            if pred is not None:
                sel = sel.where(pred)
            rows = conn.execute(sel).mappings().all()
            gname = None
            if guild_id is not None and "guilds" in tables():
                g = Table("guilds", MetaData(), autoload_with=engine)
                gname = conn.execute(sa_select(g.columns.name).where(
                    g.columns.guild_id == guild_id)).scalar()
        return render("chats", page="chats", rows=rows, guild_id=guild_id,
                      guild_name=gname or "",
                      can_sync=(guild_id is not None
                                and bool(os.getenv("DISCORD_TOKEN"))))

    def _chats_return(channel_id: int | None = None):
        """Возврат после toggle/удаления БЕЗ редиректов и без перерендера.

        Корень бага «галка всё равно снимается / сбивается сортировка»:
        раньше обработчик делал redirect(referrer или url_for('chats_page')).
        При заходе в чаты по /chats/<ID> (ID сервера) url_for подставлял в
        путь НОМЕР КАНАЛА; маршрут /chats/<int:guild_id> принимал любой целый
        ID → страница фильтровала чаты по guild_id==channel_id → пустой/
        неверный список, визуально «галка снялась», порядок строк другой.

        Теперь POST-обработчики toggles возвращают 204: чекбокс в UI живёт
        только в браузере (state sync при загрузке страницы), БД обновляется
        напрямую, страница не перезагружается → галка остаётся, сортировка
        (порядок строк серверного SELECT messages_total DESC) не меняется.
        """
        try:
            c = Table("chats", MetaData(), autoload_with=engine)
            with engine.connect() as conn:
                gid = conn.execute(sa_select(c.columns.guild_id).where(
                    c.columns.channel_id == channel_id)).scalar() \
                    if channel_id is not None else None
        except Exception:  # noqa: BLE001
            gid = None
        return ("", 204) if gid or channel_id is None else ("", 204)

    @app.route("/chats/toggle/<int:channel_id>", methods=["POST"])
    def chat_toggle_collect(channel_id: int):
        """Галка «отслеживать»: коллектор собирает live-сообщения этого чата.

        Раньше этот маршрут писал в collects_qa (галку анкет) — из-за чего две
        галки синхронизировались. Теперь «отслеживать» = chats.tracked,
        «анкеты» = chats.collects_qa (chat_toggle_qa), независимо друг от друга.
        """
        if not _qa_tables_ok("chats"):
            abort(404)
        c = Table("chats", MetaData(), autoload_with=engine)
        on = request.form.get("on") == "1"
        with engine.connect() as conn:
            if "tracked" in set(c.columns.keys()):
                conn.execute(sa_update(c).where(
                    c.columns.channel_id == channel_id).values(tracked=on))
            else:  # старая схема без tracked — до миграции: включает обработку
                conn.execute(sa_update(c).where(
                    c.columns.channel_id == channel_id).values(collects_qa=on))
            conn.commit()
        audit("chat_tracked", target=f"channel={channel_id}",
              details="on" if on else "off", result="ok")
        return _chats_return(channel_id)

    @app.route("/chats/delete/<int:channel_id>", methods=["POST"])
    def chat_delete(channel_id: int):
        """Удалить чат из списка сервера (с подтверждением в UI).

        Сообщения в БД НЕ удаляются (это собранные данные); теряются только
        строка чата и анкеты участников (chat_members).
        """
        if not _qa_tables_ok("chats"):
            abort(404)
        confirm = (request.form.get("confirm") or "").strip()
        if confirm != str(channel_id):
            flash("Подтверждение не совпало: удалите, введя ID канала "
                  f"{channel_id} в поле подтверждения.", "error")
            return redirect(request.referrer or url_for("chats_page"))
        c = Table("chats", MetaData(), autoload_with=engine)
        gid = None
        with engine.connect() as conn:
            gid = conn.execute(sa_select(c.columns.guild_id).where(
                c.columns.channel_id == channel_id)).scalar()
            conn.execute(sa_text("DELETE FROM chat_members WHERE channel_id=:cid"),
                         {"cid": channel_id})
            n = conn.execute(sa_delete(c).where(
                c.columns.channel_id == channel_id)).rowcount
            conn.commit()
        audit("chat_delete", target=f"channel={channel_id}",
              details=f"guild={gid} removed={n}", result="ok")
        flash(f"Чат {channel_id} удалён из списка (сообщения сохранены)."
              if n else "Чат не найден.", "ok" if n else "warn")
        return redirect(url_for("chats_page", guild_id=gid)
                        if gid else url_for("chats_page"))

    @app.route("/chats/qa/<int:channel_id>", methods=["POST"])
    def chat_toggle_qa(channel_id: int):
        """Галка «анкеты»: чаты с collects_qa=TRUE отдают анкеты в промпт LLM.

        Корень бага «галка всё равно снимается»: форма отправлялась на
        /chats/qa/<ID>, а маршрут /chats/<int:guild_id> принимал ЛЮБОЙ
        целый ID — GET-редирект после POST попадал на страницу чатов, где
        guild_id совпадал с channel_id → фильтр `guild_id == channel_id`
        не находил ни одного чата → галка выглядела снятой (в БД она при
        этом стояла). Поэтому:
          * этот маршрут теперь сам рендерит страницу чатов этого канала
            (без редиректа через перехватывающий маршрут);
          * тот же обходной путь возвращают toggle «отслеживать» и «файлы».
        Включение — мгновенная полная обработка канала (по требованию):
        участники из авторов сообщений + посты-анкеты + привязка тредов.
        Быстрые повторные клики идемпотентны (ON CONFLICT / WHERE message_id
        IS NULL), так что гонка двух запросов не роняет маршрут.
        """
        if not _qa_tables_ok("chats", "chat_members"):
            abort(404)
        c = Table("chats", MetaData(), autoload_with=engine)
        m = Table("messages", MetaData(), autoload_with=engine)
        on = request.form.get("on") == "1"
        silent = request.headers.get("X-Silent") == "1"
        linked = 0
        added_members = 0
        filled = 0
        gid = None
        err = None
        # ── ШАГ 1. Состояние галки пишем ОТДЕЛЬНОЙ быстрой транзакцией и
        # сразу commit. Это корневой фикс бага «галка ставится, но после
        # перезагрузки страницы снимается»: раньше запись collects_qa и
        # долгий синхронный парсинг всего канала были в одной транзакции —
        # при любом сбое/таймауте она откатывалась целиком и БД возвращала
        # collects_qa=false. Теперь галка в БД зафиксирована до того, как
        # начнётся любая тяжёлая работа; её отменить может только явный
        # клик «выключить».
        try:
            with engine.connect() as conn:
                gid = conn.execute(sa_select(c.columns.guild_id).where(
                    c.columns.channel_id == channel_id)).scalar()
                upd = sa_update(c).where(
                    c.columns.channel_id == channel_id).values(collects_qa=on)
                if on and "tracked" in set(c.columns.keys()):
                    # «все которые привяжу должны обрабатываться»: анкеты
                    # имеют смысл только для собираемого чата — включаем и
                    # «отслеживать» в той же быстрой транзакции
                    upd = upd.values(tracked=True)
                conn.execute(upd)
                conn.commit()
        except Exception as exc:  # noqa: BLE001 — показываем причину во flash
            err = f"{type(exc).__name__}: {str(exc)[:200]}"
            audit("chat_collects_qa", target=f"channel={channel_id}",
                  details=f"save error: {type(exc).__name__}: {exc}"[:400],
                  result="fail")
        # ── ШАГ 2. Тяжёлая часть (привязка к серверу, участники, парсинг
        # всего канала) — в отдельной транзакции; её ошибка НЕ откатывает
        # уже сохранённую галку. При сбое данные догонит фоновый цикл
        # chats-refresh (каждые 5 минут).
        if on and not err:
            try:
                with engine.connect() as conn:
                    if gid is None and "guild_id" in set(m.columns.keys()) \
                            and "guild_id" in set(c.columns.keys()):
                        row = conn.execute(sa_select(m.columns.guild_id).where(
                            m.columns.channel_id == channel_id,
                            m.columns.guild_id.isnot(None)).limit(1)).first()
                        if row is not None:
                            linked = conn.execute(sa_update(c).where(
                                c.columns.channel_id == channel_id,
                                c.columns.guild_id.is_(None)).values(
                                guild_id=int(row[0]))).rowcount
                            if linked:
                                gid = int(row[0])
                    added_members = _qa_add_members(conn, channel_id)
                    filled = _qa_autofill(conn, channel_id)
                    conn.commit()
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {str(exc)[:200]}"
                audit("chat_collects_qa", target=f"channel={channel_id}",
                      details=f"parse error: {type(exc).__name__}: {exc}"[:400],
                      result="fail")
            else:
                audit("chat_collects_qa", target=f"channel={channel_id}",
                      details="on"
                              + (f" guild_linked={linked}" if linked else "")
                              + (f" members+{added_members}"
                                 if added_members >= 0 else "")
                              + (f" anket={filled}" if filled else ""),
                      result="ok")
        if err:
            audit("chat_collects_qa", target=f"channel={channel_id}",
                  details="off", result="ok")
        # тихий режим (fetch из UI): состояние галки уже в БД, страница не
        # перезагружается → сортировка цела; ошибки — в теле ответа
        if silent:
            return (err or "", 204 if not err else 500)
        if err:
            flash(f"Галка «анкеты» СОХРАНЕНА, но мгновенная обработка канала "
                  f"не завершена ({err}). Фоновый цикл сборщика догонит её "
                  f"в течение ~5 минут.", "warn")
        elif on:
            parts = []
            if added_members > 0:
                parts.append(f"участников добавлено: {added_members}")
            elif added_members < 0:
                parts.append("таблица участников недоступна (старая схема?)")
            if filled:
                parts.append(f"анкет собрано из первых постов: {filled}")
            if not parts:
                parts.append("новых участников/анкет нет — сообщения появятся"
                             " позже и будут обработаны сборщиком автоматически")
            flash(f"Анкеты включены для чата {channel_id}: "
                  + "; ".join(parts) + ".", "ok")
        else:
            flash(f"Анкеты выключены для чата {channel_id}.", "ok")
        # всегда перерендериваем страницу ТОГО ЖЕ сервера (без url_for через
        # перехватывающий маршрут /chats/<int:guild_id>) — на случай прямого
        # POST из формы без JS
        return _render_chats(guild_id=int(gid) if gid else None)

    def _toggle_back(target_channel_id: int):
        """См. _chats_return — единый возврат toggles без перезагрузки страницы."""
        return _chats_return(target_channel_id)

    def _render_chats(guild_id: int | None):
        """Общий рендер страницы чатов (для toggles без редиректов)."""
        if not _qa_tables_ok("chats", "chat_members"):
            abort(404)
        c = Table("chats", MetaData(), autoload_with=engine)
        cm = Table("chat_members", MetaData(), autoload_with=engine)
        pred = (c.columns.guild_id == guild_id) if guild_id is not None else None
        with engine.connect() as conn:
            sel = (sa_select(
                c,
                sa_select(func.count()).select_from(cm)
                .where(cm.columns.channel_id == c.columns.channel_id)
                .correlate(c).scalar_subquery().label("members_n"),
                sa_select(func.count()).select_from(cm)
                .where(cm.columns.channel_id == c.columns.channel_id,
                       cm.columns.questionnaire.isnot(None),
                       func.btrim(cm.columns.questionnaire) != "")
                .correlate(c).scalar_subquery().label("qa_n"))
                .order_by(c.columns.messages_total.desc()))
            if pred is not None:
                sel = sel.where(pred)
            rows = conn.execute(sel).mappings().all()
            gname = None
            if guild_id is not None and "guilds" in tables():
                g = Table("guilds", MetaData(), autoload_with=engine)
                gname = conn.execute(sa_select(g.columns.name).where(
                    g.columns.guild_id == guild_id)).scalar()
        return render("chats", page="chats", rows=rows, guild_id=guild_id,
                      guild_name=gname or "",
                      can_sync=(guild_id is not None
                                and bool(os.getenv("DISCORD_TOKEN"))))

    @app.route("/chats/dl/<int:channel_id>", methods=["POST"])
    def chat_toggle_dl(channel_id: int):
        """Галка «скачивать файлы»: чат выгружает все вложения <= 5 МБ.

        Работает без глобального DOWNLOAD_ATTACHMENTS — worker берёт список
        отмеченных чатов из chats.download_files (см. collection_service).
        """
        if not _qa_tables_ok("chats"):
            abort(404)
        c = Table("chats", MetaData(), autoload_with=engine)
        if "download_files" not in set(c.columns.keys()):
            abort(404)  # миграция a00000000010 не применена
        on = request.form.get("on") == "1"
        with engine.connect() as conn:
            conn.execute(sa_update(c).where(
                c.columns.channel_id == channel_id).values(
                download_files=on))
            conn.commit()
        audit("chat_download_files", target=f"channel={channel_id}",
              details="on" if on else "off", result="ok")
        return _toggle_back(channel_id)

    @app.route("/chats/members/<int:channel_id>")
    def chat_members_page(channel_id: int):
        """Участники чата + анкеты (ссылка на карточку анкетирования)."""
        if not _qa_tables_ok("chats", "chat_members"):
            abort(404)
        c = Table("chats", MetaData(), autoload_with=engine)
        cm = Table("chat_members", MetaData(), autoload_with=engine)
        up = Table("user_profiles", MetaData(), autoload_with=engine) \
            if "user_profiles" in tables() else None
        m = Table("messages", MetaData(), autoload_with=engine)
        with engine.connect() as conn:
            chat = conn.execute(sa_select(c).where(
                c.columns.channel_id == channel_id)).mappings().first()
            q = (request.args.get("q") or "").strip()
            sel = (sa_select(
                cm,
                up.columns.username.label("username"),
                up.columns.global_name.label("global_name"),
                sa_select(func.count()).select_from(m)
                .where(m.columns.channel_id == cm.columns.channel_id,
                       m.columns.author_id == cm.columns.author_id)
                .correlate(cm).scalar_subquery().label("msgs_n"))
                .select_from(cm)
                .outerjoin(up, up.columns.user_id == cm.columns.author_id)
                .where(cm.columns.channel_id == channel_id)
                .order_by(sa_text("msgs_n DESC NULLS LAST")))
            if q:
                like = f"%{q}%"
                sel = sel.where(sa_or(
                    cm.columns.questionnaire.ilike(like),
                    up.columns.username.ilike(like),
                    up.columns.global_name.ilike(like),
                    cast(cm.columns.author_id, String).like(f"{q}%")))
            members = conn.execute(sel).mappings().all()
        return render("chat_members", page="chats", chat=chat,
                      members=members, channel_id=channel_id, q=q)

    @app.route("/qa/<int:channel_id>/<int:author_id>", methods=["GET", "POST"])
    def questionnaire_edit(channel_id: int, author_id: int):
        """Анкета участника — только текстом. Сохранение помечает обновлённое
        время; психо-профиль пользователя пересчитывается кнопкой в карточке."""
        if not _qa_tables_ok("chat_members"):
            abort(404)
        cm = Table("chat_members", MetaData(), autoload_with=engine)
        up = Table("user_profiles", MetaData(), autoload_with=engine) \
            if "user_profiles" in tables() else None
        m = Table("messages", MetaData(), autoload_with=engine)
        with engine.connect() as conn:
            if request.method == "POST":
                text_v = (request.form.get("questionnaire") or "").strip()
                anchor_raw = (request.form.get("message_id") or "").strip()
                anchor = int(anchor_raw) if anchor_raw.isdigit() else None
                upd = {"questionnaire": text_v or None,
                       "message_id": anchor, "updated_at": utcnow()}
                res = conn.execute(sa_update(cm).where(
                    cm.columns.channel_id == channel_id,
                    cm.columns.author_id == author_id).values(**upd))
                if res.rowcount == 0:      # участника ещё нет в чате — создаём
                    conn.execute(sa_insert(cm).values(
                        channel_id=channel_id, author_id=author_id, **upd))
                conn.commit()
                audit("questionnaire_save",
                      target=f"channel={channel_id} user={author_id}",
                      details=f"len={len(text_v)}", result="ok")
                flash("Анкета сохранена. Для применения в психо профиле "
                      "нажмите «🧠 Обновить» в карточке пользователя.", "ok")
                return redirect(url_for("questionnaire_edit",
                                         channel_id=channel_id,
                                         author_id=author_id))
            row = conn.execute(sa_select(cm).where(
                cm.columns.channel_id == channel_id,
                cm.columns.author_id == author_id)).mappings().first()
            uname = None
            if up is not None:
                uname = conn.execute(sa_select(
                    up.columns.username, up.columns.global_name).where(
                    up.columns.user_id == author_id)).first()
            # последние сообщения автора в этом чате — кандидаты в якоря ленты
            anchors = conn.execute(
                sa_select(m.columns.id, m.columns.timestamp,
                          func.left(func.coalesce(m.columns.clean_content,
                                                  m.columns.content), 80)
                          .label("snippet"))
                .where(m.columns.channel_id == channel_id,
                       m.columns.author_id == author_id)
                .order_by(m.columns.timestamp.desc()).limit(15)).mappings().all()
        return render("questionnaire", page="chats", row=row,
                      channel_id=channel_id, author_id=author_id,
                      uname=uname, anchors=anchors)

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
