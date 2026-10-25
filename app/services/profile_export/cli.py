"""app.services.profile_export.cli — часть модуля экспорта «дел» для LLM.

Автономный экспорт из PostgreSQL (SQLAlchemy + stdlib), без Discord.
Сгенерирован разделением export_profiles.py; публичный CLI — export_profiles.py.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from .env import _env_int, parse_ts
from .db import build_names, fetch_all_messages_flat, fetch_attachments, fetch_games, fetch_guild_profiles, fetch_messages, fetch_presence_snapshots, fetch_profile_history, fetch_psych_profiles, fetch_reaction_events, fetch_role_defs, fetch_roles, fetch_users, open_db
from .dialog import CONTEXT_WINDOW_SEC, build_dialog, rebuild_links
from .transcript import DEFAULT_PROMPT_MAX_KB
from .dossier import build_dossier, render_prompt_md
from .behavior import build_behavior_block


def slug(name: str) -> str:
    name = unicodedata.normalize("NFKC", name or "unknown")
    name = re.sub(r"[^\w\-]+", "_", name).strip("_")
    return name[:40] or "unknown"


def _dossier_for_json(dossier: dict) -> dict:
    """Копия дела без служебных ключов _tr_* (для dataset.jsonl / *.json)."""
    return {k: v for k, v in dossier.items() if not k.startswith("_tr_")}


def parse_args():
    # настройки по умолчанию берутся из .env (EXPORT_PROMPT_MAX_KB / EXPORT_CONTEXT_WINDOW),
    # их может переопределить флаг командной строки
    env_max_kb = _env_int("EXPORT_PROMPT_MAX_KB", DEFAULT_PROMPT_MAX_KB)
    env_ctx_window = _env_int("EXPORT_CONTEXT_WINDOW", CONTEXT_WINDOW_SEC)
    p = argparse.ArgumentParser(
        description="Экспорт данных для LLM-профайлинга.\n"
                    "Без аргументов — выгрузка ВСЕХ участников канала.\n"
                    "Позиционный аргумент — фильтр одного пользователя по username,\n"
                    "display-имени или user_id: python export_profiles.py fluff\n"
                    "Лимиты ленты читаются из .env: EXPORT_PROMPT_MAX_KB, EXPORT_CONTEXT_WINDOW")
    p.add_argument("target", nargs="?", default="",
                   help="username / имя / user_id одного пользователя (пусто = все)")
    p.add_argument("--db", default=os.getenv("DATABASE_URL", ""),
                   help="PostgreSQL URL (по умолчанию из DATABASE_URL в .env/окружении)")
    p.add_argument("--out", default="out", help="каталог результата")
    p.add_argument("--users", default="", help="список user_id через запятую (по умолчанию все)")
    p.add_argument("--priority-only", action="store_true",
                   help="экспортировать только приоритетных пользователей "
                        "(флаг priority в админке; колонка появится после миграции v3)")
    p.add_argument("--min-messages", type=int, default=1,
                   help="минимум сообщений, чтобы экспортить пользователя")
    p.add_argument("--prompt-max-kb", type=int, default=env_max_kb, metavar="N",
                   dest="prompt_max_kb",
                   help=f"максимальный вес ИТОГОВОГО файла промпта (.md) в КБ; "
                        f"транскрипт усекается по ВЕСУ (остаются самые свежие "
                        f"целые строки). 0 = без лимита (полная лента). "
                        f"По умолчанию из .env EXPORT_PROMPT_MAX_KB={env_max_kb}")
    p.add_argument("--max-lines", type=int, default=None, metavar="N",
                   dest="legacy_max_lines", help=argparse.SUPPRESS)  # deprecated
    p.add_argument("--max-msg-chars", type=int, default=None, metavar="N",
                   dest="legacy_max_msg_chars", help=argparse.SUPPRESS)  # deprecated
    p.add_argument("--per-user-json", action="store_true",
                   help="класть дела в out/profiles/*.json, а не только dataset.jsonl")
    p.add_argument("--no-prompts", action="store_true", help="не генерировать .md промпты")
    p.add_argument("--redact-pii", action="store_true",
                   help="вырезать телефоны/email/IP/IBAN из текста перед экспортом")
    p.add_argument("--no-context", action="store_true",
                   help="не включать реплики собеседников (старый формат: только монолог целевого)")
    p.add_argument("--context-window", type=int, default=env_ctx_window,
                   metavar="SEC", dest="context_window",
                   help=f"окно временной связности диалога в секундах "
                        f"(по умолчанию из .env EXPORT_CONTEXT_WINDOW={env_ctx_window})")
    env_window_days = _env_int("EXPORT_WINDOW_DAYS", 0)
    p.add_argument("--window-days", type=int, default=env_window_days,
                   metavar="N", dest="window_days",
                   help="скользящее окно транскрипта: только последние N дней от "
                        "самого свежего сообщения (0/пусто = вся история). "
                        "По умолчанию из .env EXPORT_WINDOW_DAYS")
    p.add_argument("--since", default="", metavar="YYYY-MM-DD[THH:MM]",
                   help="включать в транскрипт только сообщения с этой даты/времени (UTC)")
    p.add_argument("--until", default="", metavar="YYYY-MM-DD[THH:MM]",
                   help="включать в транскрипт только сообщения до этой даты/времени (UTC)")
    args = p.parse_args()
    if getattr(args, "legacy_max_lines", None) is not None:
        print("[WARN] --max-lines устарел и игнорируется: лимит теперь по ВЕСУ "
              "промпта (--prompt-max-kb N или EXPORT_PROMPT_MAX_KB в .env).")
    if args.legacy_max_msg_chars is not None:
        print("[WARN] --max-msg-chars устарел и игнорируется: лимит теперь "
              "по ВЕСУ промпта (--prompt-max-kb N или EXPORT_PROMPT_MAX_KB в .env).")
    args.max_lines = None   # байтовый лимит основной; строковый режим отключён
    return args


def parse_bound(value: str, end_of_day: bool = False) -> datetime | None:
    """Граница периода: '2026-09-25' или '2026-09-25T18:00' -> aware UTC datetime."""
    if not value:
        return None
    v = value.strip().replace(" ", "T")
    dt = None
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
            try:
                dt = datetime.strptime(v, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        sys.exit(f"[FATAL] Не удалось разобрать дату '{value}' (ждёт YYYY-MM-DD[THH:MM]).")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if end_of_day and len(value.strip()) <= 10:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt


def main():
    args = parse_args()
    since_dt = parse_bound(args.since)
    until_dt = parse_bound(args.until, end_of_day=True)
    if since_dt and until_dt and since_dt > until_dt:
        sys.exit(f"[FATAL] --since ({args.since}) позже --until ({args.until}).")
    out_dir = Path(args.out)
    conn, db_label = open_db(args.db)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    users = fetch_users(conn)
    guild_map = fetch_guild_profiles(conn)
    role_map = fetch_roles(conn)
    role_names = fetch_role_defs(conn)
    msg_map = fetch_messages(conn)
    att_map = fetch_attachments(conn)
    games_map = fetch_games(conn)
    history_map = fetch_profile_history(conn)
    psych_map = fetch_psych_profiles(conn)
    events_map = fetch_reaction_events(conn)
    presence_map = fetch_presence_snapshots(conn)
    all_user_ids = set(u["user_id"] for u in users)

    # связи ответов + полная лента канала для сборки диалогов с собеседниками
    rebuild_links(msg_map)
    names = build_names(conn, users)
    usernames = {u["user_id"]: (u["username"] or "").lower() for u in users}

    if args.no_context:
        flat_all: list[dict] | None = None
    else:
        flat_all = fetch_all_messages_flat(conn)
        by_author: dict[int, list[dict]] = {}
        for m in flat_all:
            by_author.setdefault(m["author_id"], []).append(m)
        rebuild_links({uid: ms for uid, ms in by_author.items()})

    wanted = {int(x) for x in args.users.split(",") if x.strip()} if args.users else None

    # --priority-only: экспорт только приоритетных (флаг ставится в админке).
    if getattr(args, "priority_only", False):
        cols = conn.columns("user_profiles")
        if "priority" not in cols:
            sys.exit("[FATAL] В базе нет колонки user_profiles.priority. "
                     "Пересоздайте схему (init_db создаёт таблицы из моделей) "
                     "или добавьте колонку вручную: "
                     "ALTER TABLE user_profiles ADD COLUMN priority boolean DEFAULT FALSE;")
        prio_ids = {r["user_id"] for r in conn.execute(
            "SELECT user_id FROM user_profiles WHERE priority IS TRUE")}
        print(f"[FILTER] приоритетных пользователей: {len(prio_ids)}")
        if not prio_ids:
            sys.exit("[FATAL] Приоритетных пользователей нет — отметьте хотя бы одного "
                     "★ в админке (таблица user_profiles).")
        wanted = prio_ids if wanted is None else (wanted & prio_ids)

    # позиционный аргумент: один пользователь по username / имени / id (пусто = все)
    target_arg = (args.target or "").strip().lower()
    if target_arg:
        ids: set[int] = set()
        if target_arg.isdigit():
            ids.add(int(target_arg))
        for uid in all_user_ids:
            if usernames.get(uid) == target_arg:
                ids.add(uid)
        if not ids:  # нестрогое совпадение по username или отображаемому имени
            for uid in all_user_ids:
                disp = (names.get(uid) or "").lower()
                uname = usernames.get(uid) or ""
                if target_arg in uname or target_arg in disp:
                    ids.add(uid)
        if not ids:
            conn.close()
            sys.exit(f"[FATAL] Пользователь '{args.target}' не найден в базе "
                     f"(всего участников: {len(all_user_ids)}). "
                     f"Укажите username, часть имени или user_id.")
        print(f"[FILTER] '{args.target}' -> user_id: {', '.join(map(str, sorted(ids)))}")
        wanted = ids if wanted is None else (wanted & ids)

    profiles_dir = out_dir / "profiles"
    prompts_dir = out_dir / "prompts"
    if args.per_user_json:
        profiles_dir.mkdir(parents=True, exist_ok=True)
    if not args.no_prompts:
        prompts_dir.mkdir(parents=True, exist_ok=True)

    index = []
    exported = skipped = 0
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "dataset.jsonl", "w", encoding="utf-8") as jl:
        for u in users:
            uid = u["user_id"]
            if wanted is not None and uid not in wanted:
                continue
            msgs = msg_map.get(uid, [])
            if len(msgs) < args.min_messages:
                skipped += 1
                continue
            dialog_msgs, dialog_mode = None, None
            if flat_all is not None:
                # диалог строим по каналу активности целевого пользователя
                ch = Counter(m["channel_id"] for m in msgs).most_common(1)[0][0]
                channel_msgs = [m for m in flat_all if m["channel_id"] == ch]
                dialog_msgs, dialog_mode = build_dialog(
                    channel_msgs, uid, window_sec=args.context_window)
            # фильтр периода (--since/--until): режем и монолог, и диалог —
            # статистика и транскрипт считаются по одному и тому же окну
            if since_dt or until_dt:
                def _in_period(m: dict) -> bool:
                    ts = parse_ts(m["timestamp"])
                    return ((not since_dt or ts >= since_dt)
                            and (not until_dt or ts <= until_dt))
                msgs = [m for m in msgs if _in_period(m)]
                if len(msgs) < args.min_messages:
                    skipped += 1
                    continue
                if dialog_msgs is not None:
                    dialog_msgs = [m for m in dialog_msgs if _in_period(m)]
                    if not any(m["author_id"] == uid for m in dialog_msgs):
                        dialog_msgs = None
                        dialog_mode = None
            dossier = build_dossier(
                u, guild_map.get(uid, []), role_map.get(uid, []),
                msgs, att_map, all_user_ids, args, generated_at,
                dialog_msgs=dialog_msgs, dialog_mode=dialog_mode, names=names,
                games=games_map.get(uid), history=history_map.get(uid),
                psych=psych_map.get(uid), role_names=role_names,
                behavior=build_behavior_block(
                    msgs, uid,
                    given_events=events_map.get(uid),
                    presence_snaps=presence_map.get(uid)),
            )
            fname = f"{uid}_{slug(u['username'])}"
            jl.write(json.dumps(_dossier_for_json(dossier),
                                ensure_ascii=False, default=str) + "\n")
            if args.per_user_json:
                (profiles_dir / f"{fname}.json").write_text(
                    json.dumps(_dossier_for_json(dossier), ensure_ascii=False,
                               indent=2, default=str),
                    encoding="utf-8")
            prompt_kb = getattr(args, "prompt_max_kb", None)
            if not args.no_prompts:
                prompt_text = render_prompt_md(dossier)
                (prompts_dir / f"{fname}.md").write_text(
                    prompt_text, encoding="utf-8")
                w_bytes = len(prompt_text.encode("utf-8"))
                if prompt_kb and prompt_kb > 0 and w_bytes > prompt_kb * 1024:
                    print(f"  [WARN] {u['username']}: вес промпта "
                          f"{w_bytes / 1024:.1f} КБ превышает лимит "
                          f"{prompt_kb} КБ — лента ужать дальше некуда "
                          f"(неизменяемая часть файла больше лимита)")
            st = dossier["statistics"]
            if not (dossier["transcript"] or "").strip():
                print(f"  [WARN] {u['username']}: транскрипт пуст при "
                      f"{st['messages_total']} сообщ. — проверьте EXPORT_PROMPT_MAX_KB/--prompt-max-kb")
            index.append({
                "user_id": str(uid),
                "display_name": u["global_name"] or u["username"],
                "priority": bool(u.get("priority")),
                "messages": st["messages_total"],
                "span_days": st["span_days"],
                "prompt_weight_kb": round(len(prompt_text.encode("utf-8")) / 1024, 1)
                                    if not args.no_prompts else None,
                "transcript_truncated": dossier["meta"]["transcript_truncated"],
                "file": f"profiles/{fname}.json" if args.per_user_json else "dataset.jsonl",
                "prompt": None if args.no_prompts else f"prompts/{fname}.md",
            })
            exported += 1
            print(f"  ✔ {u['username']} ({uid}): {st['messages_total']} сообщ.")

    summary = {
        "generated_at": generated_at,
        "db": db_label,
        "users_total": len(users),
        "exported": exported,
        "skipped_low_data": skipped,
        "min_messages": args.min_messages,
        "prompt_max_kb": (int(args.prompt_max_kb)
                          if getattr(args, "prompt_max_kb", None) else None),
        "pii_redacted": bool(args.redact_pii),
        "users": index,
    }
    (out_dir / "index.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    conn.close()
    print(f"\nГотово: {exported} пользовател(ей|ей), пропущено малоактивных: {skipped}")
    print(f"Результат в {out_dir}/: dataset.jsonl, index.json"
          + (", profiles/" if args.per_user_json else "")
          + ("" if args.no_prompts else ", prompts/"))
