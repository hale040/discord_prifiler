"""app.services.profile_export.questionnaire — анкета пользователя в промпте.

Анкета (chat_members.questionnaire) — текстовые сведения о человеке, которые
оператор добавляет к профилю вручную через админку («Серверы → чат с галкой
«анкеты» → участник → Анкета»). Текст попадает в запрос к LLM как ПЕРВИЧНЫЙ
источник данных и помечается так, чтобы модель отличала его от наблюдаемых
сообщений: вывод на основе анкеты должен ссылаться на неё, а не выдаваться
за цитату из транскрипта.

Как анкета попадает в ленту (режим контекста диалога):
  * анкета целевого пользователя вставляется строкой-«репликой» T рядом с
    сообщением-заякорением (message_id);
  * анкеты других участников — строками Qk (новая метка легенды, «дугого
    пользователя»); строки реальные, поэтому байтовый лимит промпта
    (fit_prompt_size) считает их честно и не срезает метку с половины.

Отдельный модуль — чтобы dossier/transcript оставались чистыми функциями;
здесь вся схема чатов/анкет и мягкая деградация на старых БД.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

# якорь даты/времени стандартной строки ленты: [YYYY-MM-DD HH:MM] ...
_ANCHOR_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})\]")


def _snowflake_dt(mid: int) -> datetime | None:
    """Дата создания сообщения по snowflake id (epoch 2015-01-01)."""
    try:
        return datetime.fromtimestamp(
            ((int(mid) >> 22) + 1_420_070_400_000) / 1000, tz=timezone.utc)
    except Exception:
        return None


def fetch_questionnaires(conn) -> list[dict]:
    """Все заполненные анкеты: [{author_id, text, subject, channel_id}].

    author_id — владелец анкеты; subject — автор сообщения-заякорения
    (NULL, если якорного сообщения уже нет в базе).

    Мягкая деградация: если таблиц chats/chat_members ещё нет (старая БД) —
    пустой список, сборка промпта продолжится без анкет.
    """
    if not (conn.has_table("chats") and conn.has_table("chat_members")):
        return []
    sql = (
        "SELECT cm.author_id, cm.questionnaire AS text, m.author_id AS subject, "
        "       cm.channel_id, cm.message_id, "
        "       up.username, up.global_name "
        "FROM chat_members cm "
        "JOIN chats c ON c.channel_id = cm.channel_id AND c.collects_qa "
        "LEFT JOIN messages m ON m.id = cm.message_id "
        "LEFT JOIN user_profiles up ON up.user_id = cm.author_id "
        "WHERE cm.questionnaire IS NOT NULL "
        "  AND btrim(cm.questionnaire) <> '' "
        "ORDER BY cm.channel_id, cm.author_id"
    )
    try:
        rows = conn.execute(sql)
    except Exception:  # noqa: BLE0001 — старые схемы без части колонок
        return []
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["author_name"] = d.get("global_name") or d.get("username") or str(d["author_id"])
        out.append(d)
    return out


def render_block(items: list[dict], target_uid: int) -> str:
    """Markdown-блок «Анкета» для системной части промпта."""
    own = [it for it in items if it.get("subject") == target_uid]
    out: list[str] = ["", "## Анкета (сведения от оператора; НЕ сообщения)",
                      "Каждый вывод, основанный на анкете, помечай ссылкой "
                      "«[анкета]»; не выдавай её за цитату из транскрипта."]
    for it in own:
        t = (it.get("text") or "").strip()
        if t:
            out.append(f"\n### Анкета целевого пользователя\n{t}")
    others = [it for it in items if it.get("subject") != target_uid]
    if others:
        out.append("\n### Анкеты других участников (контекст окружения)")
        for it in others[:10]:
            who = str(it.get("author_name") or it["author_id"])
            out.append(f"- @{who}: {(it['text'] or '').strip()[:600]}")
    return "\n".join(out)


def inject_into_dialog(dialog_msgs: list[dict], names: dict[int, str],
                       items: list[dict], target_uid: int) -> tuple[list[dict], int]:
    """Вставляет анкеты в исходник диалога ДО построения транскрипта.

    Каждая анкета становится служебным «сообщением» с id, derived из
    snowflake-якоря (или id владельца при отсутствии якоря): date_key и
    время совпадают с якорной строкой, поэтому лента ставит анкету сразу
    после неё, а fit_prompt_size усечёт её вместе со старыми строками.
    Возвращает (новый dialog_msgs, число вставок).
    """
    if not items or dialog_msgs is None:
        return dialog_msgs, 0
    anchor_ids = {m["id"] for m in dialog_msgs}
    by_author: dict[int, list[dict]] = {}
    for m in dialog_msgs:
        by_author.setdefault(m["author_id"], []).append(m)
    inserted: list[dict] = []
    used_fids: set[int] = set()
    n = 0
    for it in items:
        aid = it.get("author_id")
        txt = re.sub(r"\s+", " ", (it.get("text") or "").strip())[:800]
        if aid is None or not txt:
            continue
        base = None
        mid = it.get("message_id")
        if mid and mid in anchor_ids:
            base = next((m for m in dialog_msgs if m["id"] == mid), None)
        if base is None:
            lst = by_author.get(aid) or []
            base = lst[-1] if lst else None
        ts_src = base or {"id": int(aid)}
        dt = _snowflake_dt(ts_src["id"])
        if dt is None:
            continue
        ms = int(dt.timestamp() * 1000) - 1_420_070_400_000
        # служебный snowflake: та же дата/время, что у якоря; младшие биты —
        # детерминированный микс id владельца анкеты (уникальность вставок)
        fid = (ms << 22) + (int(aid) % (1 << 20)) + 1
        while fid in used_fids or fid in anchor_ids:
            fid += 1
        used_fids.add(fid)
        row = {
            "id": fid,
            "channel_id": (base or {}).get("channel_id", 0),
            "guild_id": (base or {}).get("guild_id"),
            "author_id": aid,
            "content": f"[анкета] {txt}",
            "clean_content": f"[анкета] {txt}",
            "type": 0, "reference_id": None, "reply_to_author_id": None,
            "timestamp": dt.isoformat(),
            "edited_at": None, "deleted_at": None, "pinned": False,
            "word_count": 0, "char_count": len(txt), "has_attachment": False,
            "embed_count": 0, "mention_count": 0, "hashtag_count": 0,
            "reaction_total": 0, "reactions_json": None,
            "mention_users_json": None, "stickers_json": None,
        }
        inserted.append(row)
        n += 1
    if not inserted:
        return dialog_msgs, 0
    merged = sorted(dialog_msgs + inserted,
                    key=lambda m: (str(m.get("timestamp")), int(m["id"])))
    return merged, n
