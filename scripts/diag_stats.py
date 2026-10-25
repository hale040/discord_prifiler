#!/usr/bin/env python3
"""
diag_stats.py — компактная диагностика БД для передачи аналитику.

Запуск (из корня проекта, тем же venv):
    venv/bin/python scripts/diag_stats.py            # вывод в консоль
    venv/bin/python scripts/diag_stats.py > diag.txt # в файл (пришли мне этот файл)

Формат: ~120 строк максимум, ключ=value, чтобы гарантированно влезть
в контекст. Собирает: размер таблиц, дубли, курсоры сбора, состояние ЛС,
качество данных, роли, presence/реакции/ревизии, ошибки миграций.
"""
import os, sys, json
from datetime import datetime, timezone

# DATABASE_URL из env или .env рядом с main.py
db = os.environ.get("DATABASE_URL", "")
if not db:
    for p in (".env", "../.env"):
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                if line.strip().startswith("DATABASE_URL"):
                    db = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        if db:
            break
if not db:
    sys.exit("DATABASE_URL не найден (env или .env)")

# Нормализация URL: в .env лежит SQLAlchemy-строка postgresql+psycopg://...,
# а psycopg требует чистый драйверный postgresql://... (иначе ProgrammingError
# "missing = after ..."). Режем всё между '://' и первым ':'.
if "://" in db:
    scheme, rest = db.split("://", 1)
    if "+" in scheme:          # postgresql+psycopg -> postgresql
        db = scheme.split("+", 1)[0] + "://" + rest

try:
    import psycopg
except ImportError:
    sys.exit("pip install psycopg[binary] (или запусти из venv проекта)")

MAXROWS = 40  # жёсткий потолок вывода на секцию
out_lines = []
# Собираем ВСЕ ошибки выполнения скрипта, чтобы показать их в конце и
# на stderr (при >0 — ненулевой exit code). Раньше они молча превращались
# в строки ERR внутри результатов и терялись из виду.
ERRORS = []

def emit(s=""):
    out_lines.append(s)
    print(s)

def _record_error(section, exc, sql="", params=None):
    """Логирует ошибку: тип + текст + контекст (секция/SQL/параметры) + traceback."""
    import traceback
    etype = type(exc).__name__
    msg = str(exc).splitlines()[0][:300] if str(exc) else "(пустое сообщение)"
    sql_s = " ".join((sql or "").split())[:200]
    ERRORS.append({"section": section, "type": etype, "msg": msg,
                   "sql": sql_s, "params": repr(params)[:120]})
    emit(f"ERROR [{section}] {etype}: {msg}")
    if sql_s:
        emit(f"  SQL: {sql_s}")
    if params:
        emit(f"  PARAMS: {repr(params)[:120]}")
    tb = traceback.format_exc().strip().splitlines()
    for line in tb[-4:]:  # последние кадры стека — где именно упало
        emit(f"  TRACE | {line.strip()}")

CURRENT_SECTION = ["main"]  # mutable «текущая секция» — обновляется через section()

def section(title):
    """Эмитит заголовок секции и запоминает её имя для метки ошибок."""
    CURRENT_SECTION[0] = title.strip("# ").strip()
    emit(title)

def q(cur, sql, params=None):
    """Выполняет запрос; при ошибке откатывает подтранзакцию (SAVEPOINT),
    регистрирует ошибку в ERRORS и возвращает None вместо «тихого» результата."""
    try:
        # НЕ используем `cur.transaction()` — это API только psycopg3-курсора.
        # На psycopg2/SQLAlchemy-обёртке каждый запрос падал с
        # AttributeError 'Cursor' object has no attribute 'transaction'.
        cur.execute(sql, params if params is not None else ())
        return cur.fetchall()
    except Exception as e:
        _record_error(CURRENT_SECTION[0], e, sql, params)
        # Сбрасываем aborted-транзакцию (есть у обоих драйверов), иначе все
        # последующие запросы упадут лавиной «current transaction is aborted».
        try:
            cn = getattr(cur, "connection", None)
            if cn is not None and hasattr(cn, "rollback"):
                cn.rollback()
        except Exception:
            pass
        return None

def rows(cur, sql, params=None, default=None):
    """q() + защита от None: при ошибке возвращаем default, чтобы распаковка
    строк не падала TypeError и скрипт доходил до конца."""
    r = q(cur, sql, params)
    return r if r is not None else (default if default is not None else [])

def scalar(cur, sql, params=None, default="ERR"):
    """Первое значение первой строки; при ошибке/пустоте — default."""
    r = q(cur, sql, params)
    if r and r[0] and r[0][0] is not None:
        return r[0][0]
    return default

def table_exists(cur, name):
    r = q(cur, "SELECT 1 FROM information_schema.tables WHERE table_name=%s", (name,))
    return bool(r) and len(r) == 1 and r[0][0] == 1

def main():
    try:
        conn_ctx = psycopg.connect(db)
    except Exception as e:
        _record_error("connect", e)
        sys.exit(1)
    # НЕ оборачиваем коннект в `with`: контекст-протокол Connection есть
    # только у psycopg3; на других драйверах — TypeError. Закрываем явно.
    conn = conn_ctx
    try:
        cur = conn.cursor()
        ver = scalar(cur, "SHOW server_version", default="?")
        emit(f"# DIAG discord_prifiler {datetime.now(timezone.utc).isoformat(timespec='seconds')} pg={ver}")

        # --- 1. размеры таблиц ---
        section("\n## TABLES rows")
        names = [r[0] for r in rows(cur,
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY 1")]
        for t in names:
            n = scalar(cur, f'SELECT count(*) FROM "{t}"')
            emit(f"{t}={n}")

        # --- 2. сообщения: источник и покрытие ---
        if table_exists(cur, "messages"):
            section("\n## MESSAGES")
            for src, cnt, lo, hi in rows(cur,
                "SELECT CASE WHEN guild_id IS NULL THEN 'dm' ELSE 'guild' END s,"
                " count(*), min(timestamp), max(timestamp) FROM messages GROUP BY 1"):
                emit(f"src={src} n={cnt} from={lo} to={hi}")
            dupes = scalar(cur,
                "SELECT count(*) FROM (SELECT message_id FROM messages GROUP BY 1 HAVING count(*)>1) x")
            emit(f"duplicate_message_ids={dupes}")
            empty = scalar(cur, "SELECT count(*) FROM messages WHERE clean_content IS NULL OR length(trim(clean_content))=0")
            emit(f"empty_content={empty}")
            edited = scalar(cur, "SELECT count(*) FROM messages WHERE edited_timestamp IS NOT NULL")
            emit(f"edited_msgs={edited}")
            per_user = scalar(cur,
                "SELECT count(DISTINCT author_id) FROM messages")
            emit(f"distinct_authors={per_user}")
            top = rows(cur,
                "SELECT author_id, count(*) c FROM messages GROUP BY 1 ORDER BY c DESC LIMIT 5")
            emit("top_authors=" + ",".join(f"{a}:{c}" for a, c in top))
            gaps = scalar(cur,
                "SELECT count(*) FROM ("
                " SELECT author_id, ts_lag FROM ("
                "  SELECT author_id, \"timestamp\" - lag(\"timestamp\") OVER (PARTITION BY author_id ORDER BY \"timestamp\") ts_lag"
                "  FROM messages) x WHERE ts_lag > interval '30 days') y")
            emit(f"gaps_gt_30d_per_author={gaps}")

        # --- 3. профили и качество ---
        if table_exists(cur, "user_profiles"):
            section("\n## PROFILES")
            total = scalar(cur, "SELECT count(*) FROM user_profiles")
            emit(f"total={total}")
            no_msgs = scalar(cur,
                "SELECT count(*) FROM user_profiles p WHERE NOT EXISTS"
                " (SELECT 1 FROM messages m WHERE m.author_id=p.user_id)") if table_exists(cur, "messages") else "?"
            emit(f"profiles_without_messages={no_msgs}")
            bots = scalar(cur, "SELECT count(*) FROM user_profiles WHERE bot IS true")
            emit(f"bots={bots}")
            pri = rows(cur, "SELECT priority, count(*) FROM user_profiles GROUP BY 1 ORDER BY 1") if column_exists(cur, "user_profiles", "priority") else []
            emit(f"priorities=" + ",".join(f"{p}:{c}" for p, c in pri))

        # --- 4. ЛС-сбор ---
        if table_exists(cur, "user_profiles") and column_exists(cur, "user_profiles", "dm_fetch"):
            section("\n## DM_COLLECTION")
            on = scalar(cur, "SELECT count(*) FROM user_profiles WHERE dm_fetch IS true")
            emit(f"dm_fetch_on={on}")
            fixed_ch = scalar(cur, "SELECT count(*) FROM user_profiles WHERE dm_channel_id IS NOT NULL")
            emit(f"channel_fixed={fixed_ch}")
            synced = scalar(cur, "SELECT count(*) FROM user_profiles WHERE dm_synced_at IS NOT NULL")
            emit(f"ever_synced={synced}")
            dm_msgs = scalar(cur, "SELECT count(*) FROM messages WHERE guild_id IS NULL") if table_exists(cur, "messages") else "?"
            emit(f"dm_messages_total={dm_msgs}")
            stuck = rows(cur,
                "SELECT user_id, dm_status, dm_error_count, dm_last_error FROM user_profiles"
                " WHERE dm_error_count > 0 ORDER BY dm_error_count DESC LIMIT 5")
            for u, st, ec, er in stuck:
                emit(f"stuck user={u} status={st} errors={ec} last_err={(er or '')[:80]}")

        # --- 5. динамические сигналы v10 ---
        section("\n## SIGNALS_V10")
        for t in ("message_revisions", "reaction_events", "presence_snapshots", "interaction_aggregates"):
            if table_exists(cur, t):
                emit(f"{t}={scalar(cur, f'SELECT count(*) FROM {t}')}")
            else:
                emit(f"{t}=MISSING_TABLE")

        # --- 6. роли ---
        if table_exists(cur, "user_roles"):
            section("\n## ROLES")
            emit(f"user_roles={scalar(cur, 'SELECT count(*) FROM user_roles')}")
            ev = scalar(cur, "SELECT count(*) FROM user_roles WHERE role_id = guild_id") if column_exists(cur, "user_roles", "guild_id") else "?"
            emit(f"everyone_left={ev}")
            dups = scalar(cur,
                "SELECT count(*) FROM (SELECT user_id, role_id FROM user_roles GROUP BY 1,2 HAVING count(*)>1) x")
            emit(f"role_dups={dups}")
            distinct_roles = scalar(cur, "SELECT count(DISTINCT role_id) FROM user_roles")
            emit(f"distinct_roles={distinct_roles}")

        # --- 7. психопрофили ---
        if table_exists(cur, "psych_profiles"):
            section("\n## PSYCH")
            emit(f"total={scalar(cur, 'SELECT count(*) FROM psych_profiles')}")
            cur_cnt = scalar(cur, "SELECT count(*) FROM psych_profiles WHERE is_current IS true") if column_exists(cur, "psych_profiles", "is_current") else "?"
            emit(f"current={cur_cnt}")
            multi = scalar(cur,
                "SELECT count(*) FROM (SELECT user_id FROM psych_profiles WHERE is_current IS true"
                " GROUP BY 1 HAVING count(*)>1) x") if column_exists(cur, "psych_profiles", "is_current") else "?"
            emit(f"users_with_multiple_current={multi}")

        # --- 8. схема/миграции ---
        section("\n## SCHEMA")
        sm = "alembic_version" if table_exists(cur, "alembic_version") else None
        if sm:
            for v in rows(cur, "SELECT version_num FROM alembic_version"):
                emit(f"alembic_rev={v[0]}")
        # физические проверки против моделей
        checks = []
        if table_exists(cur, "user_profiles"):
            for c in ("dm_fetch", "dm_channel_id", "dm_status", "dm_error_count"):
                if not column_exists(cur, "user_profiles", c):
                    checks.append(f"MISSING user_profiles.{c}")
        for ch in checks:
            emit(ch)
        if not checks:
            emit("physical_columns=OK")

    # --- 9. ошибки выполнения (главное нововведение) ---
        if ERRORS:
            emit(f"\n## ERRORS count={len(ERRORS)}")
            for i, e in enumerate(ERRORS, 1):
                emit(f"[{i}] section={e['section']} {e['type']}: {e['msg']}")
                if e["sql"]:
                    emit(f"     sql={e['sql']}")
                if e["params"] and e["params"] != "None":
                    emit(f"     params={e['params']}")
        else:
            emit("\n## ERRORS count=0")

        emit(f"\n# END lines={len(out_lines)} errors={len(ERRORS)}")

        # Ошибки — также на stderr, и ненулевой exit code, чтобы `diag.sh && ...`
        # или CI не приняли частично проваленный прогон за успешный.
        if ERRORS:
            print(f"\ndiag_stats.py: во время выполнения возникло ошибок: {len(ERRORS)}", file=sys.stderr)
            for i, e in enumerate(ERRORS, 1):
                print(f"  [{i}] {e['section']}: {e['type']}: {e['msg']}", file=sys.stderr)
            sys.exit(1)
    finally:
        try:
            conn.close()
        except Exception:
            pass

def column_exists(cur, table, col):
    r = q(cur,
        "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s", (table, col))
    return bool(r)

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        # Любая непредвиденная ошибка вне запросов (питон-баг, сеть при connect и т.п.)
        # тоже должна быть видна, а не уходить в молчаливый traceback без контекста секции.
        import traceback
        traceback.print_exc()
        print(f"\ndiag_stats.py: ФАТАЛЬНАЯ ОШИБКА: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
