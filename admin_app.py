#!/usr/bin/env python3
"""Админка поверх БД профайлера (Flask + SQLAlchemy Core).

Запуск:
    python admin_app.py [--db data/profiling.db] [--port 8777]

Вход по паролю из ENV/.env: ADMIN_PASSWORD (обязателен — иначе fail-closed).
Сессии подписываются itsdangerous-ключом из SECRET_KEY (или ADMIN_PASSWORD).

Возможности:
  * Дашборд: сводка по таблицам, последние события аудита.
  * Просмотр/поиск по всем таблицам (SQLAlchemy Core select + пагинация).
    Для каждой таблицы показывается только «нужное» число столбцов
    (VIEW_COLUMNS); длинные JSON/raw-поля скрыты из ленты — они видны
    целиком на странице записи. Настройка через ENV ADMIN_EXTRA_COLUMNS
    (доп. столбцы) и ADMIN_HIDE_COLUMNS (скрываемые), формат "таблица:столбец".
  * Карточка записи (/browse/<table>/<pk>): просмотр и РЕДАКТИРОВАНИЕ любой
    строки — все редактируемые поля с типами из схемы SQLAlchemy,
    валидация типов, UPDATE только по PK, запись старого значения в audit_log.
  * SQL-редактор: SELECT всегда; UPDATE/DELETE — только с WHERE и после
    подтверждения (count-before), в транзакции, с записью в audit_log.
  * Админ-действия: перезапуск миграций схемы, VACUUM/ANALYZE SQLite.

Миграции: любое изменение схемы идёт через app/models/migrations.py
(эта админка добавила v2 -> таблицу audit_log).

ВАЖНО: не выставляйте админку в интернет без HTTPS reverse-proxy и
настоящего SECRET_KEY; она работает с БД напрямую.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import secrets
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)
from sqlalchemy import MetaData, Table, create_engine, func, inspect, select as sa_select, text
from sqlalchemy import and_ as sa_and, or_ as sa_or, update as sa_update
from markupsafe import Markup
from sqlalchemy.engine import Engine

BASE_DIR = Path(__file__).resolve().parent

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
        "id", "user_id", "username", "global_name", "created_at_discord",
        "bot", "about", "premium_type", "last_updated",
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
}


def _sync_url(url: str) -> str:
    if url.startswith("sqlite+aiosqlite"):
        url = url.replace("sqlite+aiosqlite", "sqlite")
    return url


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

    app = Flask(__name__)
    app.secret_key = secret
    app.config["DATABASE_URL"] = database_url
    engine: Engine = create_engine(_sync_url(database_url), future=True)
    app.config["ENGINE"] = engine

    # --- аудит -------------------------------------------------------------
    def audit(action: str, target: str = "", details: str = "", result: str = "ok") -> None:
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO audit_log (actor, action, target, details, result) "
                        "VALUES ('admin', :a, :t, :d, :r)"
                    ),
                    {"a": action, "t": target[:256], "d": (details or "")[:4000], "r": result[:512]},
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
        return [c for c in chosen if c not in hidden]

    def editable_fields(sa_tbl: Table) -> list[dict]:
        """Редактируемые поля записи по схеме SQLAlchemy (тип/nullable/PK)."""
        from sqlalchemy.sql.visitors import traverse_single
        from sqlalchemy.engine import interfaces

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
                elif cls == "BigInteger":
                    kind = "bigint"      # текстовый ввод -> int (snowflake не влезает в JS-число)
                elif cls in ("Float", "Numeric", "DECIMAL"):
                    kind = "float"
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
        if kind == "datetime":
            from datetime import datetime as _dt
            s = raw.replace("T", " ").replace("Z", "")
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    return _dt.strptime(s, fmt)
                except ValueError:
                    continue
            raise ValueError(f"ожидается дата 'ГГГГ-ММ-ДД [ЧЧ:ММ[:СС]]', получено {raw!r}")
        return raw

    def render(page_src: str, **ctx):
        """Страница = LAYOUT с подставленным содержимым (без наследования Jinja)."""
        return render_template_string(LAYOUT, content=Markup(render_template_string(page_src, **ctx)))

    # --- шаблоны ------------------------------------------------------------
    LAYOUT = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
    <title>{% block t %}Профайлер — админка{% endblock %}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      body{font-family:system-ui,sans-serif;margin:0;background:#f4f5f7;color:#222}
      nav{background:#1e1f2b;padding:.6rem 1rem;display:flex;gap:1rem;flex-wrap:wrap}
      nav a{color:#cfd3ff;text-decoration:none;font-size:.9rem}
      nav a.on{color:#fff;font-weight:700}
      main{max-width:1200px;margin:1rem auto;padding:0 1rem}
      table{border-collapse:collapse;width:100%;background:#fff;font-size:.82rem}
      th,td{border:1px solid #ddd;padding:.3rem .5rem;vertical-align:top;
            max-width:380px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      td.json{white-space:pre-wrap;word-break:break-all}
      th{background:#eef}
      .card{background:#fff;border:1px solid #ddd;border-radius:8px;padding:1rem;margin:.6rem 0}
      .btn{background:#4550e5;color:#fff;border:0;border-radius:6px;padding:.45rem .9rem;cursor:pointer}
      .btn.warn{background:#c62828}.btn.gray{background:#666}
      input[type=password],input[type=text],textarea,select{padding:.45rem;border:1px solid #bbb;
            border-radius:6px;font:inherit;width:100%;box-sizing:border-box}
      textarea{min-height:110px;font-family:ui-monospace,monospace}
      .flash{background:#dff5e1;border:1px solid #9c9;padding:.5rem .8rem;border-radius:6px;margin:.5rem 0}
      .err{background:#ffd7d7;border-color:#c62828}
      .muted{color:#777;font-size:.8rem}
      .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:.6rem}
      pre.sql{background:#111;color:#8f8;padding:.6rem;border-radius:6px;overflow:auto}
      .pager a,.pager span{margin-right:.4rem}
    </style></head><body>
    {% if session.get('admin_ok') %}
    <nav>
      <a class="{{ 'on' if page=='dash' }}">📊 Дашборд</a>
      {% for tb in nav_tables %}<a href="{{ url_for('browse', table=tb) }}"
         class="{{ 'on' if page=='browse' and table==tb }}">{{ tb }}</a>{% endfor %}
      <a href="{{ url_for('sql_page') }}" class="{{ 'on' if page=='sql' }}">SQL</a>
      <a href="{{ url_for('audit_page') }}" class="{{ 'on' if page=='audit' }}">Аудит</a>
      <a href="{{ url_for('actions_page') }}" class="{{ 'on' if page=='actions' }}">⚙️ Действия</a>
      <a href="{{ url_for('logout') }}">Выйти</a>
    </nav>
    {% endif %}
    <main>
      {% with msgs = get_flashed_messages(with_categories=true) %}
        {% for cat, m in msgs %}<div class="flash {{ 'err' if cat=='error' }}">{{ m }}</div>{% endfor %}
      {% endwith %}
      {{ content }}
    </main></body></html>"""

    PAGE_LOGIN = """<div class='card' style='max-width:420px;margin:4rem auto'>"
        "<h3>Вход в админку</h3><form method='post'>"
        "<input type='password' name='pw' placeholder='ADMIN_PASSWORD' autofocus>"
        "<br><br><button class='btn'>Войти</button></form></div>"""

    PAGE_DASH = """<h2>Дашборд</h2>
    <p class='muted'>БД: {{ db }} · схема v{{ ver }} · всего строк: {{ total }}</p>
    <div class='grid'>{% for name, cnt in counts %}
      <div class='card'><b>{{ name }}</b><br><span style='font-size:1.4rem'>{{ cnt }}</span><br>
      <a href="{{ url_for('browse', table=name) }}">просмотр →</a></div>{% endfor %}</div>
    <div class='card'><h3>Последние действия</h3>
      {% if recent %}<table><tr><th>когда</th><th>действие</th><th>объект</th><th>результат</th></tr>
      {% for r in recent %}<tr><td>{{ r.created_at }}</td><td>{{ r.action }}</td>
      <td>{{ r.target }}</td><td>{{ r.result }}</td></tr>{% endfor %}</table>
      {% else %}<p class='muted'>пусто</p>{% endif %}</div>"""

    PAGE_BROWSE = """<h2>Таблица «{{ table }}» <span class='muted'>({{ total }} строк)</span></h2>
    <form method='get' class='card' style='display:flex;gap:.5rem;align-items:end'>
      <div style='flex:1'><label class='muted'>Поиск подстрокой</label>
      <input type='text' name='q' value='{{ q }}' placeholder='подстрока в любом столбце'></div>
      <div style='flex:1'><label class='muted'>Фильтр</label>
      <select name='col'><option value=''>—</option>
      {% for c in cols %}<option {{ 'selected' if col==c }}>{{ c }}</option>{% endfor %}</select></div>
      <div style='flex:1'><label class='muted'>= значение</label>
      <input type='text' name='val' value='{{ val }}'></div>
      <button class='btn'>Применить</button>
      <a class='btn gray' href='{{ url_for('browse', table=table) }}'>сброс</a>
    </form>
    <p class='muted'>В ленте только нужные столбцы{% if hidden_count %} · {{ hidden_count }}
    скрытых (raw/json) см. в карточке записи{% endif %}. Клик по id — открыть/править.</p>
    <div style='overflow:auto'><table><tr>
      {% for c in cols %}<th>{{ c }}</th>{% endfor %}<th></th></tr>
      {% for row in rows %}<tr>
        {% for cell in row.cells %}<td class='{{ 'json' if cell[1] }}' title='{{ cell[0] }}'>
          {% if loop.index0 == 0 %}<a href="{{ url_for('record', table=table, pk=row.pk) }}">{{ cell[0] }}</a>
          {% else %}{{ cell[0] }}{% endif %}</td>{% endfor %}
        <td style='white-space:nowrap'>
          <a class='btn' href="{{ url_for('record', table=table, pk=row.pk) }}">✎</a>
          <form method='post' action="{{ url_for('delete_row', table=table) }}" style='display:inline'
                onsubmit="return confirm('Удалить строку {{ table }}={{ row.pk }}?');">
            <input type='hidden' name='pk' value='{{ row.pk }}'><button class='btn warn'>✕</button></form></td>
      </tr>{% endfor %}</table></div>
    <p class='pager'>
      {% if cur_page > 1 %}<a class='btn gray' href='{{ href(cur_page-1) }}'>← назад</a>{% endif %}
      стр. {{ cur_page }} / {{ pages }}
      {% if cur_page < pages %}<a class='btn gray' href='{{ href(cur_page+1) }}'>вперёд →</a>{% endif %}
    </p>"""

    PAGE_RECORD = """<h2>{{ table }} · запись {{ pk }}</h2>
    <p class='muted'><a href="{{ url_for('browse', table=table) }}">← к таблице</a></p>
    <form method='post' class='card'>
      <table style='background:transparent'>{% for f in fields %}
      <tr>
        <td style='width:260px;border:0;padding:.3rem .6rem .3rem 0;white-space:normal'>
          <b>{{ f.name }}</b>
          <div class='muted'>{{ f.type }}{% if f.pk %} · PK{% endif %}{% if not f.nullable and not f.pk %} · обяз.%{% endif %}</div>
        </td>
        <td border='0' style='border:0;white-space:normal'>
          {% if f.pk %}
            <input type='text' value='{{ f.value }}' readonly style='background:#eee'>
          {% elif f.kind == 'bool' %}
            <select name='fld_{{ f.name }}' style='width:auto'>
              {% if f.nullable %}<option value='' {{ 'selected' if f.value is none }}>NULL</option>{% endif %}
              <option value='1' {{ 'selected' if f.value }}>true</option>
              <option value='0' {{ 'selected' if (not f.value and f.value is not none) or (f.value is none and not f.nullable) }}>false</option>
            </select>
          {% elif f.kind in ('textarea',) %}
            <textarea name='fld_{{ f.name }}'>{{ f.value if f.value is not none else '' }}</textarea>
          {% else %}
            <input type='text' name='fld_{{ f.name }}'
                   value='{{ f.value if f.value is not none else "" }}'
                   placeholder='{{ "пусто = NULL" if f.nullable else "обязательное поле" }}'>
          {% endif %}
        </td>
      </tr>{% endfor %}</table>
      <br><button class='btn'>💾 Сохранить изменения</button>
      <a class='btn gray' href='{{ url_for("browse", table=table) }}'>отмена</a>
    </form>"""

    PAGE_SQL = """<h2>SQL-редактор</h2>
    <div class='card muted'>SELECT выполняется сразу. UPDATE/DELETE требуют WHERE и подтверждения.
    DROP/ALTER/CREATE/VACUUM запрещены (используйте «Действия»).</div>
    {% if preview %}
      <div class='card'><pre class='sql'>{{ preview.sql }}</pre>
      <p>Затронет строк: <b>{{ preview.n }}</b>.
      {% if preview.n >= 0 %}
      <form method='post' style='display:inline'>
        <input type='hidden' name='confirm_token' value='{{ preview.token }}'>
        <input type='hidden' name='sql' value='{{ preview.sql }}'>
        <button class='btn warn'>Подтвердить выполнение</button></form>
      {% endif %}
      <a class='btn gray' href='{{ url_for('sql_page') }}'>отмена</a></p>
      {% if preview.error %}<div class='flash err'>{{ preview.error }}</div>{% endif %}</div>
    {% endif %}
    {% if result is not none %}
      <div class='card'><h4>Результат ({{ result|length }} строк)</h4>
      {% if result %}<table><tr>{% for k in result[0].keys() %}<th>{{ k }}</th>{% endfor %}</tr>
      {% for r in result %}<tr>{% for v in r.values() %}<td>{{ v }}</td>{% endfor %}</tr>{% endfor %}</table>
      {% else %}<p class='muted'>пусто</p>{% endif %}</div>
    {% endif %}
    <form method='post' class='card'>
      <textarea name='sql' placeholder="SELECT * FROM messages ORDER BY id DESC LIMIT 50">{{ raw or '' }}</textarea>
      <br><br><button class='btn'>Выполнить</button></form>"""

    PAGE_AUDIT = """<h2>Журнал аудита</h2>
    <table><tr><th>id</th><th>когда</th><th>action</th><th>target</th><th>details</th><th>result</th></tr>
    {% for r in rows %}<tr><td>{{ r.id }}</td><td>{{ r.created_at }}</td><td>{{ r.action }}</td>
    <td>{{ r.target }}</td><td class='json'>{{ r.details }}</td><td>{{ r.result }}</td></tr>{% endfor %}</table>
    <p class='pager'>{% if cur_page>1 %}<a class='btn gray' href='?page={{ cur_page-1 }}'>←</a>{% endif %}
    стр. {{ cur_page }} / {{ pages }}
    {% if cur_page<pages %}<a class='btn gray' href='?page={{ cur_page+1 }}'>→</a>{% endif %}</p>"""

    PAGE_ACT = """<h2>Админ-действия</h2>
    <div class='card'><h4>Перезапустить миграции схемы</h4>
    <p class='muted'>Применяет отсутствующие шаги app/models/migrations.py (идемпотентно).</p>
    <form method='post'><button class='btn'>Запустить миграции</button></form></div>
    <div class='card'><h4>VACUUM + ANALYZE (SQLite)</h4>
    <p class='muted'>Дефрагментация и пересчёт статистики планировщика.</p>
    <form method='post'><input type='hidden' name='op' value='vacuum'>
    <button class='btn warn'>Выполнить</button></form></div>
    {% if last %}<div class='card'><h4>Последний результат</h4><pre>{{ last }}</pre></div>{% endif %}"""

    # --- auth -----------------------------------------------------------------
    @app.before_request
    def require_login():
        if request.endpoint in ("login", "logout", "static"):
            return
        if not session.get("admin_ok"):
            return redirect(url_for("login"))

    @app.context_processor
    def inject_nav():
        return {"nav_tables": tables(), "page": ""}

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
        return render(PAGE_LOGIN, page="", error=error)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # --- дашборд ----------------------------------------------------------------
    @app.route("/")
    def dashboard():
        from app.models.migrations import get_current_version

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
            PAGE_DASH, page="dash", db=app.config["DATABASE_URL"],
            ver=get_current_version(engine), counts=counts, total=total, recent=recent,
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
        page = max(1, int(request.args.get("page") or 1))
        per = 50

        # Таблица через SQLAlchemy Core (autoload) — используется в выражениях ниже.
        sa_tbl = Table(table, MetaData(), autoload_with=engine)
        where_parts = []
        if q:
            like = "%" + q.replace("%", "\\%").replace("_", "\\_") + "%"
            conds = [sa_tbl.columns[c].like(like, escape="\\") for c in cols]
            where_parts.append(sa_or(*conds))
        if col and val:
            where_parts.append(sa_tbl.columns[col] == val)
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

        def href(p: int) -> str:
            base = f"/browse/{quote_plus(table)}?page={p}"
            if q:
                base += "&q=" + quote_plus(q)
            if col:
                base += "&col=" + quote_plus(col) + "&val=" + quote_plus(val)
            return base

        return render(
            PAGE_BROWSE, page="browse", table=table, cols=cols, rows=rows,
            total=total, q=q, col=col, val=val, cur_page=page,
            pages=max(1, -(-total // per)), href=href,
            hidden_count=len(all_cols) - len(cols),
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
                    # Сравнение по строковому представлению (SQLite хранит
                    # datetime как текст; bool/int приходят из формы строками).
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
             "value": values.get(f["name"]),
             "error": errors.get(f["name"])}
            for f in fields
        ]
        return render(PAGE_RECORD, page="browse", table=table, pk=pk,
                      fields=form_fields)

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
                with engine.begin() as conn:
                    conn.execute(text(sql))
                audit("sql_insert", details=sql, result="ok")
                flash("INSERT выполнен.", "ok")
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
            PAGE_SQL, page="sql", raw=raw, result=result, preview=preview,
        )

    def _select_for_count(kind: str, sql: str) -> str:
        """Извлекает табличное выражение для подсчёта затрагиваемых строк."""
        if kind == "DELETE":
            m = re.match(r"(?is)^DELETE\s+FROM\s+(\S+)(.*)$", sql)
            return f"* FROM {m.group(1)}{m.group(2)}"
        m = re.match(r"(?is)^UPDATE\s+(\S+)\s+SET\s+.+?(\s+WHERE\s+.*)?$", sql)
        where = m.group(2) or ""
        return f"* FROM {m.group(1)}{where}"

    # --- аудит-страница --------------------------------------------------------------
    @app.route("/audit")
    def audit_page():
        if "audit_log" not in tables():
            flash("Таблица audit_log отсутствует — выполните миграции («Действия»).", "error")
            return render(PAGE_AUDIT, page="audit", rows=[], cur_page=1, pages=1)
        page = max(1, int(request.args.get("page") or 1))
        per = 100
        with engine.connect() as conn:
            total = conn.execute(text("SELECT COUNT(*) FROM audit_log")).scalar() or 0
            rows = conn.execute(
                text("SELECT * FROM audit_log ORDER BY id DESC LIMIT :l OFFSET :o"),
                {"l": per, "o": (page - 1) * per},
            ).mappings().all()
        return render(
            PAGE_AUDIT, page="audit", rows=rows, cur_page=page, pages=max(1, -(-total // per)),
        )

    # --- действия ---------------------------------------------------------------------
    @app.route("/actions", methods=["GET", "POST"])
    def actions_page():
        last = session.get("last_action")
        if request.method == "POST":
            op = request.form.get("op", "migrate")
            if op == "vacuum":
                with engine.connect() as conn:
                    conn.execute(text("VACUUM"))
                    conn.execute(text("ANALYZE"))
                audit("vacuum_analyze", result="ok")
                last = "VACUUM/ANALYZE выполнены."
            else:
                from app.models.migrations import run_migrations

                n = run_migrations(engine)
                audit("run_migrations", result=f"applied={n}")
                last = f"Миграции: применено шагов — {n}."
            session["last_action"] = last
            flash(last, "ok")
            return redirect(url_for("actions_page"))
        return render(PAGE_ACT, page="actions", last=last)

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="Веб-админка поверх БД профайлера")
    ap.add_argument("--db", default=None, help="путь к sqlite или URL (ENV DATABASE_URL)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8777)
    args = ap.parse_args()

    load_dotenv(BASE_DIR / ".env")
    db = args.db or os.getenv("DATABASE_URL", "sqlite:///data/profiling.db")
    if not db.startswith(("sqlite:", "postgresql", "mysql")):
        db = "sqlite:///" + quote_plus(str(Path(db).resolve()))
    app = create_app(db)
    print(f"Admin UI: http://{args.host}:{args.port}  (db={db})")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
