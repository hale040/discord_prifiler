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


def fetch_questionnaires(conn, channel_ids=None) -> list[dict]:
    """Заполненные анкеты чатов с галкой «анкеты»: [{author_id, text, ...}].

    channel_ids — ограничить выборку конкретными чатами (лента строится по
    одному каналу — анкеты берём из того же канала); None = все чаты с
    collects_qa=TRUE. author_id — владелец анкеты; subject — автор
    сообщения-заякорения (NULL, если якорного сообщения уже нет в базе).

    Мягкая деградация: если таблиц chats/chat_members ещё нет (старая БД) —
    пустой список, сборка промпта продолжится без анкет.
    """
    if not (conn.has_table("chats") and conn.has_table("chat_members")):
        return []
    where_ch = ""
    if channel_ids:
        ids = ", ".join(str(int(x)) for x in channel_ids)
        where_ch = f" AND cm.channel_id IN ({ids})"
    sql = (
        "SELECT cm.author_id, cm.questionnaire AS text, m.author_id AS subject, "
        "       cm.channel_id, cm.message_id, "
        "       up.username, up.global_name "
        "FROM chat_members cm "
        "JOIN chats c ON c.channel_id = cm.channel_id AND c.collects_qa "
        "LEFT JOIN messages m ON m.id = cm.message_id "
        "LEFT JOIN user_profiles up ON up.user_id = cm.author_id "
        "WHERE cm.questionnaire IS NOT NULL "
        "  AND btrim(cm.questionnaire) <> ''"
        f"{where_ch} "
        "ORDER BY cm.channel_id, cm.author_id"
    )
    try:
        rows = conn.execute(sql)
    except Exception:  # noqa: BLE001 — старые схемы без части колонок
        return []
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["author_name"] = d.get("global_name") or d.get("username") or str(d["author_id"])
        out.append(d)
    return out


def _first_level_posts(conn) -> str:
    """SQL-условие «пост первого уровня» в имеющейся схеме.

    Схема хранит только reference_id (reply-to; thread_root_id не маппится),
    поэтому первый уровень дерева = сообщение ни на что не отвечает.
    """
    cols = set()
    try:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'messages'")
        cols = {r["column_name"] for r in rows}
    except Exception:  # noqa: BLE001
        pass
    conds = []
    if "thread_root_id" in cols:
        conds.append("m.thread_root_id IS NULL")
    if "reference_id" in cols:
        conds.append("m.reference_id IS NULL")
    return ("AND (" + " AND ".join(conds) + ")" if conds else "")


def qa_posts_autofill(conn, guild_id=None) -> int:
    """Привязать посты-анкеты к участникам чатов с галкой «анкеты».

    Анкета участника = POST первого уровня дерева чата (сообщение автора,
    ни на что не отвечающее), автор сообщения == автор анкеты
    (chat_members.author_id). Если у записи ещё нет message_id — проставляем
    самый свежий такой пост. Это источник и для анкеты (текст поста +
    questionnaire оператора идут в промпт), и для комментариев (ответы
    треда/реплаи помечаются как ответы на анкету этого пользователя).
    Возвращает число привязок. Идемпотентно."""
    if not (conn.has_table("chats") and conn.has_table("chat_members")
            and conn.has_table("messages")):
        return 0
    where_g = ""
    if guild_id is not None:
        where_g = f" AND c.guild_id = {int(guild_id)}"
    lvl = _first_level_posts(conn)
    sql = (
        "UPDATE chat_members cm SET message_id = sub.pid, "
        "questionnaire = CASE WHEN btrim(COALESCE(cm.questionnaire, '')) = ''"
        "                        THEN COALESCE(sub.pcontent, '') "
        "                        ELSE cm.questionnaire END, "
        "updated_at = now() "
        "  FROM ("
        "    SELECT DISTINCT ON (cm2.channel_id, cm2.author_id)"
        "           cm2.channel_id AS ch, cm2.author_id AS au, m.id AS pid,"
        "           COALESCE(m.content, '') AS pcontent"
        "      FROM chat_members cm2"
        "      JOIN chats c ON c.channel_id = cm2.channel_id AND c.collects_qa"
        "      JOIN messages m ON m.channel_id = cm2.channel_id"
        "                   AND m.author_id = cm2.author_id"
        "     WHERE (cm2.questionnaire IS NULL"
        "            OR btrim(cm2.questionnaire) = '')"
        "       AND cm2.message_id IS NULL"
        f"       {lvl}"
        f"       {where_g}"
        "     ORDER BY cm2.channel_id, cm2.author_id, m.timestamp ASC"
        "  ) sub "
        "WHERE cm.channel_id = sub.ch AND cm.author_id = sub.au"
    )
    try:
        res = conn.execute(sql)
        return len(res) if isinstance(res, list) else getattr(res, "rowcount", 0)
    except Exception:  # noqa: BLE001 — старые схемы
        return 0


def fetch_qa_replies(conn, channel_ids=None) -> list[dict]:
    """Комментарии к постам-анкетам (ответы в треде/реплаи на пост автора).

    Каждый комментарий помечается анкетой, к которой он относится
    (qa_author_id/qa_author_name) — в промпте это выглядит как «комментарий
    из анкеты другого пользователя», а не обычная реплика чата.

    Возвращает [{id, timestamp, content, author_id, author_name,
    qa_author_id, qa_author_name, channel_id}]; мягкая деградация — [].
    """
    if not (conn.has_table("chats") and conn.has_table("chat_members")
            and conn.has_table("messages")):
        return []
    where_ch = ""
    if channel_ids:
        ids = ", ".join(str(int(x)) for x in channel_ids)
        where_ch = f" AND q.channel_id IN ({ids})"
    # ветки UNION собираются по фактическим колонкам messages: thread_root_id
    # появляется только с новой ревизией маппера, reference_id есть всегда
    try:
        cols = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'messages'")}
    except Exception:  # noqa: BLE001
        cols = {"reference_id"}
    branches: list[str] = []
    head = (
        "  SELECT m.id, m.timestamp, m.channel_id, m.author_id,"
        "         COALESCE(m.clean_content, m.content) AS content,"
        "         COALESCE(ump.global_name, ump.username,"
        "                  CAST(m.author_id AS TEXT)) AS author_name,"
        "         q.qa_author_id, q.qa_author_name"
        "  FROM messages m")
    if "thread_root_id" in cols:
        branches.append(head +
                        "  JOIN qa_posts q ON q.post_id = m.thread_root_id"
                        "  LEFT JOIN user_profiles ump ON ump.user_id = m.author_id")
    if "reference_id" in cols:
        branches.append(head +
                        "  JOIN qa_posts q ON q.post_id = m.reference_id"
                        "  LEFT JOIN user_profiles ump ON ump.user_id = m.author_id")
    if not branches:
        return []
    sql = (
        "WITH qa_posts AS ("
        "  SELECT cm.message_id AS post_id, cm.author_id AS qa_author_id,"
        "         COALESCE(upq.global_name, upq.username,"
        "                  CAST(cm.author_id AS TEXT)) AS qa_author_name,"
        "         cm.channel_id"
        "  FROM chat_members cm"
        "  JOIN chats c ON c.channel_id = cm.channel_id AND c.collects_qa"
        "  LEFT JOIN user_profiles upq ON upq.user_id = cm.author_id"
        "  WHERE cm.questionnaire IS NOT NULL"
        "    AND btrim(cm.questionnaire) <> ''"
        "    AND cm.message_id IS NOT NULL"
        "), replies AS ("
        + "  UNION".join(branches) +
        ") "
        "SELECT * FROM replies"
        f"{where_ch}"
        " ORDER BY timestamp"
    )
    try:
        return [dict(r) for r in conn.execute(sql)]
    except Exception:  # noqa: BLE001 — старые схемы без части колонок
        return []


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


def render_replies_block(replies: list[dict], target_uid: int) -> str:
    """Markdown-блок «Комментарии из анкет» для системной части промпта.

    Реплаи на посты-анкеты других участников — это мнения об анкетируемых,
    но они описывают и автора реплиа (как он относится к людям). Для
    целевого пользователя важны реплии OTHER на ЕГО анкету (отзывы о нём) —
    вывод по ним помечается ссылкой [коммент из анкеты]."""
    if not replies:
        return ""
    out: list[str] = ["", "## Комментарии из анкет (ответы в тредах постов-анкет)",
                      "Это НЕ обычные сообщения чата: каждый комментарий — "
                      "ответ на анкету указанного пользователя. Выводы на их "
                      "основе помечай ссылкой «[коммент из анкеты]»."]
    about_target = [r for r in replies if r.get("qa_author_id") == target_uid]
    by_target = [r for r in replies if r.get("author_id") == target_uid]
    if about_target:
        out.append("\n### Отзывы на анкету целевого пользователя")
        for r in about_target[:20]:
            who = r.get("author_name") or r.get("author_id")
            txt = re.sub(r"\s+", " ", str(r.get("content") or "").strip())[:300]
            out.append(f"- @{who}: {txt}")
    if by_target:
        out.append("\n### Комментарии целевого пользователя к анкетам других")
        for r in by_target[:20]:
            qa_who = r.get("qa_author_name") or r.get("qa_author_id")
            txt = re.sub(r"\s+", " ", str(r.get("content") or "").strip())[:300]
            out.append(f"- (к анкете @{qa_who}): {txt}")
    others = [r for r in replies
              if r.get("qa_author_id") != target_uid
              and r.get("author_id") != target_uid]
    if others:
        out.append("\n### Прочие комментарии из анкет (фон)")
        for r in others[:15]:
            who = r.get("author_name") or r.get("author_id")
            qa_who = r.get("qa_author_name") or r.get("qa_author_id")
            txt = re.sub(r"\s+", " ", str(r.get("content") or "").strip())[:200]
            out.append(f"- @{who} → анкета @{qa_who}: {txt}")
    return "\n".join(out)
