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

try:
    import psycopg
except ImportError:
    sys.exit("pip install psycopg[binary] (или запусти из venv проекта)")

MAXROWS = 40  # жёсткий потолок вывода на секцию
out_lines = []

def emit(s=""):
    out_lines.append(s)
    print(s)

def q(cur, sql, params=None):
    try:
        cur.execute(sql, params or ())
        return cur.fetchall()
    except Exception as e:
        return [("ERR", str(e)[:120])]

def table_exists(cur, name):
    return bool(q(cur, "SELECT 1 FROM information_schema.tables WHERE table_name=%s", (name,))
                ) and q(cur, "SELECT 1 FROM information_schema.tables WHERE table_name=%s", (name,))[0][0] == 1

def main():
    with psycopg.connect(db) as conn, conn.cursor() as cur:
        ver = q(cur, "SHOW server_version")[0][0]
        emit(f"# DIAG discord_prifiler {datetime.now(timezone.utc).isoformat(timespec='seconds')} pg={ver}")

        # --- 1. размеры таблиц ---
        emit("\n## TABLES rows")
        names = [r[0] for r in q(cur,
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY 1")]
        for t in names:
            n = q(cur, f'SELECT count(*) FROM "{t}"')[0][0]
            emit(f"{t}={n}")

        # --- 2. сообщения: источник и покрытие ---
        if table_exists(cur, "messages"):
            emit("\n## MESSAGES")
            for src, cnt, lo, hi in q(cur,
                "SELECT CASE WHEN guild_id IS NULL THEN 'dm' ELSE 'guild' END s,"
                " count(*), min(timestamp), max(timestamp) FROM messages GROUP BY 1"):
                emit(f"src={src} n={cnt} from={lo} to={hi}")
            dupes = q(cur,
                "SELECT count(*) FROM (SELECT message_id FROM messages GROUP BY 1 HAVING count(*)>1) x")[0][0]
            emit(f"duplicate_message_ids={dupes}")
            empty = q(cur, "SELECT count(*) FROM messages WHERE clean_content IS NULL OR length(trim(clean_content))=0")[0][0]
            emit(f"empty_content={empty}")
            edited = q(cur, "SELECT count(*) FROM messages WHERE edited_timestamp IS NOT NULL")[0][0]
            emit(f"edited_msgs={edited}")
            per_user = q(cur,
                "SELECT count(DISTINCT author_id) FROM messages")[0][0]
            emit(f"distinct_authors={per_user}")
            top = q(cur,
                "SELECT author_id, count(*) c FROM messages GROUP BY 1 ORDER BY c DESC LIMIT 5")
            emit("top_authors=" + ",".join(f"{a}:{c}" for a, c in top))
            gaps = q(cur,
                "SELECT count(*) FROM ("
                " SELECT author_id, ts_lag FROM ("
                "  SELECT author_id, timestamp - lag(timestamp) OVER (PARTITION BY author_id ORDER BY timestamp) ts_lag"
                "  FROM messages) x WHERE ts_lag > interval '30 days') y")[0][0]
            emit(f"gaps_gt_30d_per_author={gaps}")

        # --- 3. профили и качество ---
        if table_exists(cur, "user_profiles"):
            emit("\n## PROFILES")
            total = q(cur, "SELECT count(*) FROM user_profiles")[0][0]
            emit(f"total={total}")
            no_msgs = q(cur,
                "SELECT count(*) FROM user_profiles p WHERE NOT EXISTS"
                " (SELECT 1 FROM messages m WHERE m.author_id=p.user_id)")[0][0] if table_exists(cur, "messages") else "?"
            emit(f"profiles_without_messages={no_msgs}")
            bots = q(cur, "SELECT count(*) FROM user_profiles WHERE bot IS true")[0][0]
            emit(f"bots={bots}")
            pri = q(cur, "SELECT priority, count(*) FROM user_profiles GROUP BY 1 ORDER BY 1")[0:] if column_exists(cur, "user_profiles", "priority") else []
            emit(f"priorities=" + ",".join(f"{p}:{c}" for p, c in pri))

        # --- 4. ЛС-сбор ---
        if table_exists(cur, "user_profiles") and column_exists(cur, "user_profiles", "dm_fetch"):
            emit("\n## DM_COLLECTION")
            on = q(cur, "SELECT count(*) FROM user_profiles WHERE dm_fetch IS true")[0][0]
            emit(f"dm_fetch_on={on}")
            fixed_ch = q(cur, "SELECT count(*) FROM user_profiles WHERE dm_channel_id IS NOT NULL")[0][0]
            emit(f"channel_fixed={fixed_ch}")
            synced = q(cur, "SELECT count(*) FROM user_profiles WHERE dm_synced_at IS NOT NULL")[0][0]
            emit(f"ever_synced={synced}")
            dm_msgs = q(cur, "SELECT count(*) FROM messages WHERE guild_id IS NULL")[0][0] if table_exists(cur, "messages") else "?"
            emit(f"dm_messages_total={dm_msgs}")
            stuck = q(cur,
                "SELECT user_id, dm_status, dm_error_count, dm_last_error FROM user_profiles"
                " WHERE dm_error_count > 0 ORDER BY dm_error_count DESC LIMIT 5")
            for u, st, ec, er in stuck:
                emit(f"stuck user={u} status={st} errors={ec} last_err={(er or '')[:80]}")

        # --- 5. динамические сигналы v10 ---
        emit("\n## SIGNALS_V10")
        for t in ("message_revisions", "reaction_events", "presence_snapshots", "interaction_aggregates"):
            if table_exists(cur, t):
                emit(f"{t}={q(cur, f'SELECT count(*) FROM {t}')[0][0]}")
            else:
                emit(f"{t}=MISSING_TABLE")

        # --- 6. роли ---
        if table_exists(cur, "user_roles"):
            emit("\n## ROLES")
            emit(f"user_roles={q(cur, 'SELECT count(*) FROM user_roles')[0][0]}")
            ev = q(cur, "SELECT count(*) FROM user_roles WHERE role_id = guild_id")[0][0] if column_exists(cur, "user_roles", "guild_id") else "?"
            emit(f"everyone_left={ev}")
            dups = q(cur,
                "SELECT count(*) FROM (SELECT user_id, role_id FROM user_roles GROUP BY 1,2 HAVING count(*)>1) x")[0][0]
            emit(f"role_dups={dups}")
            distinct_roles = q(cur, "SELECT count(DISTINCT role_id) FROM user_roles")[0][0]
            emit(f"distinct_roles={distinct_roles}")

        # --- 7. психопрофили ---
        if table_exists(cur, "psych_profiles"):
            emit("\n## PSYCH")
            emit(f"total={q(cur, 'SELECT count(*) FROM psych_profiles')[0][0]}")
            cur_cnt = q(cur, "SELECT count(*) FROM psych_profiles WHERE is_current IS true")[0][0] if column_exists(cur, "psych_profiles", "is_current") else "?"
            emit(f"current={cur_cnt}")
            multi = q(cur,
                "SELECT count(*) FROM (SELECT user_id FROM psych_profiles WHERE is_current IS true"
                " GROUP BY 1 HAVING count(*)>1) x")[0][0] if column_exists(cur, "psych_profiles", "is_current") else "?"
            emit(f"users_with_multiple_current={multi}")

        # --- 8. схема/миграции ---
        emit("\n## SCHEMA")
        sm = "alembic_version" if table_exists(cur, "alembic_version") else None
        if sm:
            for v in q(cur, "SELECT version_num FROM alembic_version"):
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

    emit(f"\n# END lines={len(out_lines)}")

def column_exists(cur, table, col):
    return bool(q(cur,
        "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s", (table, col)))

if __name__ == "__main__":
    main()
