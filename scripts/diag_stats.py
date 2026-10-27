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
            # колонки берём по факту схемы: id (PK, BIGSERIAL), discord_id — если есть;
            # edited_at в baseline-схеме (не edited_timestamp)
            msg_id_col = ("message_id" if column_exists(cur, "messages", "message_id")
                          else "discord_id" if column_exists(cur, "messages", "discord_id")
                          else "id")
            dupes = scalar(cur,
                f"SELECT count(*) FROM (SELECT {msg_id_col} FROM messages GROUP BY 1 HAVING count(*)>1) x")
            emit(f"duplicate_message_ids={dupes} (col={msg_id_col})")
            empty = scalar(cur, "SELECT count(*) FROM messages WHERE clean_content IS NULL OR length(trim(clean_content))=0")
            emit(f"empty_content={empty}")
            edited_col = ("edited_timestamp" if column_exists(cur, "messages", "edited_timestamp")
                          else "edited_at" if column_exists(cur, "messages", "edited_at")
                          else None)
            if edited_col:
                edited = scalar(cur, f"SELECT count(*) FROM messages WHERE {edited_col} IS NOT NULL")
                emit(f"edited_msgs={edited}")
            else:
                emit("edited_msgs=N/A (нет колонки edited_at/edited_timestamp)")
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
            # dm_error_count в реальной схеме отсутствует — есть dm_fail_count (миграция v12);
            # колонку выбираем по факту наличия, чтобы не ловить UndefinedColumn
            err_col = ("dm_error_count" if column_exists(cur, "user_profiles", "dm_error_count")
                       else "dm_fail_count" if column_exists(cur, "user_profiles", "dm_fail_count")
                       else None)
            if err_col and column_exists(cur, "user_profiles", "dm_last_error"):
                stuck = rows(cur,
                    f"SELECT user_id, dm_status, {err_col}, dm_last_error FROM user_profiles"
                    f" WHERE {err_col} > 0 ORDER BY {err_col} DESC LIMIT 5")
                for u, st, ec, er in stuck:
                    emit(f"stuck user={u} status={st} errors={ec} last_err={(er or '')[:80]}")
            else:
                emit("stuck_users=N/A (нет колонки dm_error_count/dm_fail_count)")

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

        # --- 7b. качество психо-профилей (результаты LLM) ---
        if table_exists(cur, "psych_profiles"):
            section("\n## PSYCH_QUALITY")
            need = ("big_five_openness", "mbti_type", "confidence_overall",
                    "methodology_snapshot", "messages_analyzed", "source_lines")
            have = {c for c in need if column_exists(cur, "psych_profiles", c)}
            no_mbti = scalar(cur, "SELECT count(*) FROM psych_profiles WHERE is_current IS true"
                                  " AND (mbti_type IS NULL OR mbti_type='')") if "mbti_type" in have else "?"
            emit(f"current_without_mbti={no_mbti}")
            no_b5 = scalar(cur, "SELECT count(*) FROM psych_profiles WHERE is_current IS true"
                                " AND big_five_openness IS NULL") if "big_five_openness" in have else "?"
            emit(f"current_without_bigfive={no_b5}")
            if "methodology_snapshot" in have:
                no_meth = scalar(cur, "SELECT count(*) FROM psych_profiles WHERE is_current IS true"
                                      " AND methodology_snapshot IS NULL")
                emit(f"current_without_methodology={no_meth}")  # >0 => профили до фикса P0 или битый путь записи
            # summary (итоговое резюме) лежит в result_json — считаем по JSONB-ключу
            no_sum = scalar(cur, "SELECT count(*) FROM psych_profiles WHERE is_current IS true"
                                 " AND (result_json->>'summary' IS NULL OR result_json->>'summary'='')")
            emit(f"current_without_summary={no_sum}")  # >0 => профили до добавления блока summary; пересчитать кнопкой
            if "messages_analyzed" in have:
                thin = scalar(cur, "SELECT count(*) FROM psych_profiles WHERE is_current IS true"
                                      " AND messages_analyzed < 50")
                emit(f"thin_profiles_lt50_msgs={thin}")  # низкая доказательная база
            sig_per_profile = scalar(cur,
                "SELECT coalesce(max(c),0) FROM (SELECT profile_id, count(*) c FROM psych_signals"
                " GROUP BY 1) x") if table_exists(cur, "psych_signals") else "?"
            emit(f"max_signals_per_profile={sig_per_profile}")

        # --- 7c. presence и игры (диагностика пустой user_games) ---
        if table_exists(cur, "user_games"):
            section("\n## PRESENCE_GAMES")
            ug = scalar(cur, "SELECT count(*) FROM user_games")
            emit(f"user_games_total={ug}")
            kind_null = scalar(cur, "SELECT count(*) FROM user_games WHERE kind IS NULL")
            emit(f"kind_null={kind_null}")  # >0 => legacy-строки не нормализованы (fix_legacy_game_kinds не отработал)
            # psycopg3: литеральные % в SQL без параметров нужно экранировать как %%
            kind_custom = scalar(cur, "SELECT count(*) FROM user_games WHERE lower(kind) LIKE '%%custom%%'")
            emit(f"kind_custom_leak={kind_custom}")  # >0 => кастомный статус снова пишется как игра
            distinct_players = scalar(cur, "SELECT count(DISTINCT user_id) FROM user_games")
            emit(f"users_with_games={distinct_players}")
            topg = rows(cur, "SELECT name, count(*) c FROM user_games GROUP BY 1 ORDER BY c DESC LIMIT 5")
            emit("top_games=" + ",".join(f"{n}:{c}" for n, c in topg) if topg else "top_games=-")
            # нормализация kind из presence_snapshots: legacy-строки содержат
            # 'ActivityType.playing' вместо 'playing'. lower + replace префикса.
            ps_kind_norm = ("lower(replace(activity_kind,'ActivityType.',''))")
            if table_exists(cur, "presence_snapshots"):
                ps = scalar(cur, "SELECT count(*) FROM presence_snapshots")
                emit(f"presence_snapshots={ps}")
                pu = scalar(cur, "SELECT count(DISTINCT user_id) FROM presence_snapshots")
                emit(f"presence_distinct_users={pu}")
                # сколько юзеров с games вообще не имеют ни одной игры — норма при офлайн-статусах
                if ug is not None and str(ug) != "ERR":
                    psu = scalar(cur, "SELECT count(DISTINCT user_id) FROM presence_snapshots"
                                      " WHERE user_id NOT IN (SELECT user_id FROM user_games)")
                    emit(f"presence_users_without_games={psu}")
                # индикатор «реальной» проблемы: у юзера в presence стоит игра
                # (kind='playing'), но в user_games пусто => fix_legacy_game_kinds
                # не отработал или presence завис. Считаем только после нормализации kind.
                pgw = scalar(cur, f"SELECT count(DISTINCT user_id) FROM presence_snapshots"
                                  f" WHERE {ps_kind_norm}='playing'"
                                  " AND user_id NOT IN (SELECT user_id FROM user_games)")
                emit(f"presence_playing_no_game={pgw}")  # >0 => лечим через fix_legacy_game_kinds
            # когда был последний снимок (в реальной схеме колонка observed_at)
            ps_time_col = next((c for c in ("observed_at", "captured_at", "snapshot_at", "created_at")
                                if table_exists(cur, "presence_snapshots")
                                and column_exists(cur, "presence_snapshots", c)), None)
            if ps_time_col:
                last_ps = scalar(cur, f"SELECT max({ps_time_col}) FROM presence_snapshots")
                emit(f"presence_last_capture={last_ps} (col={ps_time_col})")
                # свежесть сбора: если последний снимок старше ~2 ч — коллектор presence не крутится
                age_min = scalar(cur,
                    f"SELECT extract(epoch from (now() - max({ps_time_col})))/60"
                    " FROM presence_snapshots")
                try:
                    if age_min != "ERR" and float(age_min) > 120:
                        emit(f"WARN presence_stale_min={int(float(age_min))} (>120: presence-сбор завис)")
                except (TypeError, ValueError):
                    pass

        # --- 7d. DM-курсоры: зависшие задачи pending_dm ---
        if table_exists(cur, "user_profiles") and column_exists(cur, "user_profiles", "pending_dm"):
            section("\n## DM_PENDING")
            pend = scalar(cur, "SELECT count(*) FROM user_profiles WHERE pending_dm IS true")
            emit(f"pending_dm_true={pend}")  # >0 надолго => dm-loop не крутится (коллектор упал/не выбивает флаг)
            if pend not in ("ERR", 0) and column_exists(cur, "user_profiles", "dm_synced_at"):
                stuckp = rows(cur,
                    "SELECT user_id, dm_status, dm_fail_count, left(coalesce(dm_last_error,''),60)"
                    " FROM user_profiles WHERE pending_dm IS true LIMIT 10")
                for u, st, fc, er in stuckp:
                    emit(f"pending user={u} status={st} fails={fc} err={er}")

        # --- 7e. Серверы/чаты/анкеты (миграция a00000000006) ---
        if table_exists(cur, "chats"):
            section("\n## CHATS_QA")
            emit(f"guilds={scalar(cur, 'SELECT count(*) FROM guilds') if table_exists(cur, 'guilds') else 'MISSING guilds'}")
            emit(f"chats={scalar(cur, 'SELECT count(*) FROM chats')}")
            emit(f"chats_collects_qa={scalar(cur, 'SELECT count(*) FROM chats WHERE collects_qa IS true')}")
            emit(f"chat_members={scalar(cur, 'SELECT count(*) FROM chat_members') if table_exists(cur, 'chat_members') else 'MISSING chat_members'}")
            if table_exists(cur, "chat_members"):
                q_filled = scalar(cur,
                    "SELECT count(*) FROM chat_members "
                    "WHERE btrim(coalesce(questionnaire,'')) <> ''")
                emit(f"questionnaires_filled={q_filled}")
                orphan = scalar(cur, "SELECT count(*) FROM chat_members cm LEFT JOIN chats c ON c.channel_id = cm.channel_id WHERE c.channel_id IS NULL")
                emit(f"members_orphan_no_chat={orphan}")  # >0 => перенос не покрывает авторов ЛС/левых каналов
                if orphan not in ("ERR", 0):
                    emit("WARN members_orphan_no_chat>0: анкеты этих участников не видны в админке (нет строки chats)")
                qa_off = scalar(cur,
                    "SELECT count(*) FROM chat_members cm JOIN chats c ON c.channel_id=cm.channel_id "
                    "WHERE c.collects_qa IS NOT true AND btrim(coalesce(cm.questionnaire,'')) <> ''")
                emit(f"qa_filled_but_chat_disabled={qa_off}")  # заполненные анкеты, которые НЕ попадут в промпт
                if qa_off not in ("ERR", 0):
                    emit(f"WARN qa_filled_but_chat_disabled={qa_off}: поставьте галку «анкеты» на чатах или это ожидаемо")

        # --- 8. схема/миграции ---
        section("\n## SCHEMA")
        sm = "alembic_version" if table_exists(cur, "alembic_version") else None
        if sm:
            for v in rows(cur, "SELECT version_num FROM alembic_version"):
                emit(f"alembic_rev={v[0]}")
        # физические проверки против моделей
        checks = []
        if table_exists(cur, "user_profiles"):
            for c in ("dm_fetch", "dm_channel_id", "dm_status",
                      "dm_fail_count" if not column_exists(cur, "user_profiles", "dm_error_count")
                      else "dm_error_count"):
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
