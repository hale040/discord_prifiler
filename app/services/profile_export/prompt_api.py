"""app.services.profile_export.api — часть модуля экспорта «дел» для LLM.

Автономный экспорт из PostgreSQL (SQLAlchemy + stdlib), без Discord.
Сгенерирован разделением export_profiles.py; публичный CLI — export_profiles.py.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from .env import _env_int
from .db import build_names, fetch_all_messages_flat, fetch_attachments, fetch_games, fetch_presence_snapshots, fetch_reaction_events, fetch_messages, fetch_profile_history, fetch_psych_profiles, fetch_role_defs, fetch_users, open_db
from .dialog import CONTEXT_WINDOW_SEC, build_dialog, rebuild_links
from .transcript import DEFAULT_PROMPT_MAX_KB
from .dossier import _date_only, build_dossier, render_prompt_md
from .behavior import build_behavior_block


def build_prompt_for_user(db_url: str, user_id: int, *,
                          prompt_max_kb: int | None = None,
                          context_window: int | None = None,
                          window_days: int | None = None) -> dict:
    """Собрать «дело» и готовый промпт одного пользователя из PostgreSQL.

    prompt_max_kb — максимальный вес итогового промпта (по умолчанию из
    .env EXPORT_PROMPT_MAX_KB); транскрипт усекается по ВЕСУ.
    window_days — скользящее окно ленты (по умолчанию из .env
    EXPORT_WINDOW_DAYS; 0/пусто = вся история).
    Возвращает dict: username, display_name, messages_total, period_from,
    period_to, prompt (markdown-промпт с системной инструкцией и схемой JSON).
    Исключения — ValueError (нет данных/пользователя), RuntimeError (БД).
    """
    conn, _label = open_db(db_url)
    try:
        users = fetch_users(conn)
        uid_set = {u["user_id"] for u in users}
        if user_id not in uid_set:
            raise ValueError(f"Пользователь {user_id} не найден в базе.")
        msg_map = fetch_messages(conn)
        msgs_all = msg_map.get(user_id, [])
        if not msgs_all:
            raise ValueError(
                f"У пользователя {user_id} нет сообщений в базе — "
                f"сначала запустите сбор (python main.py).")

        names = build_names(conn, users)
        att_map = fetch_attachments(conn)
        games_map = fetch_games(conn)
        history_map = fetch_profile_history(conn)
        psych_map = fetch_psych_profiles(conn)
        events_map = fetch_reaction_events(conn)
        presence_map = fetch_presence_snapshots(conn)
        role_names = fetch_role_defs(conn)
        all_user_ids = set(uid_set)

        # диалог по самому активному каналу (как в main())
        ch = Counter(m["channel_id"] for m in msgs_all).most_common(1)[0][0]
        flat_all = [m for m in fetch_all_messages_flat(conn) if m["channel_id"] == ch]
        rebuild_links({user_id: msgs_all})
        dialog_msgs, dialog_mode = build_dialog(
            flat_all, user_id,
            window_sec=(context_window if context_window is not None
                        else _env_int("EXPORT_CONTEXT_WINDOW", CONTEXT_WINDOW_SEC)))

        ns = argparse.Namespace(
            max_lines=None,
            prompt_max_kb=(prompt_max_kb if prompt_max_kb is not None
                           else _env_int("EXPORT_PROMPT_MAX_KB",
                                         DEFAULT_PROMPT_MAX_KB)),
            redact_pii=False, no_context=False,
            window_days=(window_days if window_days is not None
                         else _env_int("EXPORT_WINDOW_DAYS", 0) or None),
            context_window=(context_window if context_window is not None
                            else _env_int("EXPORT_CONTEXT_WINDOW", CONTEXT_WINDOW_SEC)),
        )
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        dossier = build_dossier(
            next(u for u in users if u["user_id"] == user_id),
            [], [], msgs_all, att_map, all_user_ids, ns, generated_at,
            dialog_msgs=dialog_msgs, dialog_mode=dialog_mode, names=names,
            games=games_map.get(user_id), history=history_map.get(user_id),
            psych=psych_map.get(user_id), role_names=role_names,
            behavior=build_behavior_block(
                msgs_all, user_id,
                given_events=events_map.get(user_id),
                presence_snaps=presence_map.get(user_id)),
        )
        st = dossier["statistics"]
        bh = dossier.get("behavior") or {}
        eff_window = ns.window_days  # None = вся история (окно не резало ленту)
        return {
            "user_id": user_id,
            "username": dossier["identity"].get("username") or str(user_id),
            "display_name": names.get(user_id) or str(user_id),
            "messages_total": st["messages_total"],
            # Data Quality Gate (см. psych_gate): окно наблюдения и объём ЛС
            "span_days": st.get("span_days"),
            "dm_messages_total": sum(1 for m in msgs_all if not m.get("guild_id")),
            "period_from": _date_only(st.get("first_message_at")),
            "period_to": _date_only(st.get("last_message_at")),
            "prompt": render_prompt_md(dossier),
            # версионирование методики: что реально попало в промпт —
            # был ли блок «Поведение» и каким окном резалась лента
            "behavior_present": bool(bh.get("sessions")
                                     or bh.get("initiative_latency")
                                     or bh.get("segments")),
            "window_days": eff_window,
        }
    except SQLAlchemyError as e:
        raise RuntimeError(f"Ошибка чтения БД: {e}") from e
    finally:
        conn.close()
