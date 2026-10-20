#!/usr/bin/env python3
"""Экспорт собранных данных в формат, подготовленный для нейросетевого профайлинга.

Скрипт работает автономно (только SQLite + stdlib), к Discord НЕ подключается.
Для каждого пользователя формирует «дело» — структурированный JSON с:
  - профилем (bio, бейджи, connections, flags, аккаунт-метаданные);
  - серверными атрибутами (ник, роли/теги, дата вступления, таймауты);
  - статистикой коммуникации (частоты, длины, реакции, ответы, эмодзи,
    активность по часам/дням недели — сигналы для определения психотипа);
  - текстовой лентой сообщений (chronological transcript; лимит — КОЛИЧЕСТВО
    СТРОК чата, берутся самые свежие: EXPORT_MAX_LINES в .env или --max-lines N;
    0 = полная лента без среза);
  - медиа-сигналами (картинки: локальные пути/URL).

Выход:
  out/profiles/<user_id>_<username>.json   — полное дело одного пользователя
  out/prompts/<user_id>_<username>.md      — готовый промпт для LLM-профайлера
  out/dataset.jsonl                         — строка-на-пользователя (fine-tune/batch)
  out/index.json                            — сводка по всем пользователям

Запуск:
  python export_profiles.py [--db data/profiling.db] [--out out]
        [пользователь] [--users id1,id2] [--min-messages 5] [--max-lines N]
        [--per-user-json] [--no-prompts]

Настройки выгрузки по умолчанию читаются из .env (файл подгружается сам):
  EXPORT_MAX_LINES=300          — максимум СТРОК чата в транскрипте; берутся
                                  самые СВЕЖИЕ (срез от нового к старому),
                                  строки текста не обрезаются. 0 = полная лента.
  EXPORT_CONTEXT_WINDOW=120     — окно временной связности диалога, сек.
Флаг командной строки (--max-lines / --context-window) переопределяет .env.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def load_env_file(path: str = ".env") -> None:
    """Минималистичный загрузчик .env (stdlib, без зависимостей).

    Значения из самого файла НЕ перекрывают переменные окружения процесса.
    Используется для EXPORT_* настроек выгрузки (например EXPORT_MAX_LINES).
    """
    try:
        p = Path(path)
        if not p.is_file():
            return
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.split(" #", 1)[0].strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass


load_env_file()

# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF"
    "\U0001F900-\U0001F9FF"
    "]+"
)
MENTION_RE = re.compile(r"<(@[!#]?)(\d{5,25})>")
CODEBLOCK_RE = re.compile(r"```.*?(```|$)", re.S)
URL_RE = re.compile(r"https?://\S+")
ALL_CAP_WORD_RE = re.compile(r"[A-ZА-ЯЁ]{4,}")
QUESTION_RE = re.compile(r"\?+")
EXCLAM_RE = re.compile(r"!{2,}")
HESITATION_WORDS = {
    "наверное", "возможно", "может", "probably", "maybe", "как бы",
    "вроде", "не знаю", "i dunno", "похоже", "нечто", "немного",
}
ABSOLUTE_WORDS = {
    "всегда", "никогда", "точно", "конечно", "абсолютно", "always",
    "never", "definitely", "конечно", "безусловно", "100%",
}
APOLOGY_WORDS = {"извини", "простите", "sorry", "виноват", "пардон", "oops"}
GRATITUDE_WORDS = {"спасибо", "thanks", "thx", "благодарю", "thank you"}


def load_json(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


def iso(dt):
    """Нормализует datetime/строку в ISO. SQLite может вернуть строку без tz."""
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.isoformat()


def parse_ts(value) -> datetime:
    """datetime (SQLAlchemy) или ISO-строка (raw sqlite3) -> aware datetime (UTC)."""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Загрузка из БД
# ---------------------------------------------------------------------------

def open_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        sys.exit(f"База не найдена: {db_path}. Сначала запустите сбор (python main.py).")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_users(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM user_profiles ORDER BY user_id").fetchall()
    return [dict(r) for r in rows]


def fetch_guild_profiles(conn) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for r in conn.execute("SELECT * FROM guild_member_profiles ORDER BY user_id"):
        out.setdefault(r["user_id"], []).append(dict(r))
    return out


def fetch_roles(conn) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for r in conn.execute("SELECT * FROM user_roles ORDER BY user_id, position DESC"):
        out.setdefault(r["user_id"], []).append(dict(r))
    return out


def fetch_messages(conn) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    q = """SELECT id, channel_id, guild_id, author_id, content, clean_content,
                  type, reference_id, reply_to_author_id, timestamp, edited_at,
                  deleted_at, pinned,
                  word_count, char_count, has_attachment, embed_count,
                  mention_count, hashtag_count, reaction_total, reactions_json,
                  mention_users_json, stickers_json
           FROM messages WHERE deleted_at IS NULL ORDER BY timestamp ASC"""
    for r in conn.execute(q):
        d = dict(r)
        # старые БД без колонки reply_to_author_id -> None (достроим позже)
        d.setdefault("reply_to_author_id", None)
        out.setdefault(d["author_id"], []).append(d)
    return out


def fetch_all_messages_flat(conn) -> list[dict]:
    """Все сообщения канала хронологически (для сборки диалогов с собеседниками)."""
    q = """SELECT id, channel_id, guild_id, author_id, content, clean_content,
                  type, reference_id, reply_to_author_id, timestamp, edited_at,
                  deleted_at, pinned,
                  word_count, char_count, has_attachment, embed_count,
                  mention_count, hashtag_count, reaction_total, reactions_json,
                  mention_users_json, stickers_json
           FROM messages WHERE deleted_at IS NULL ORDER BY timestamp ASC, id ASC"""
    rows = [dict(r) for r in conn.execute(q)]
    for d in rows:
        d.setdefault("reply_to_author_id", None)
    return rows


def build_names(conn, users: list[dict]) -> dict[int, str]:
    """user_id -> отображаемое имя (global_name/username, серверный ник в приоритете)."""
    names = {u["user_id"]: (u["global_name"] or u["username"] or f"id{u['user_id']}")
             for u in users}
    try:
        for g in conn.execute("SELECT user_id, nick FROM guild_member_profiles"):
            if g["nick"]:
                names[g["user_id"]] = g["nick"]
    except sqlite3.Error:
        pass
    return names


# ---------------------------------------------------------------------------
# Связи диалога: reply-цепочки + временной контекст собеседников
# ---------------------------------------------------------------------------

CONTEXT_WINDOW_SEC = 120  # окно «соседних» реплик для восстановления ветки


def rebuild_links(messages_all: dict[int, list[dict]]) -> None:
    """Достроить ``reply_to_author_id`` из raw-полей БД (идемпотентно).

    Используется, если коллектор ещё не выполнил link_reply_references():
    1) родитель по reference_id есть в базе -> автор родителя;
    2) только для сообщений БЕЗ reference_id: первое resolved-упоминание
       (mention_users_json), отличные от автора. Для настоящих reply-сообщений
       упоминания не используются — там <@id> это ping третьих лиц, а не адресат.
    """
    by_id = {m["id"]: m for msgs in messages_all.values() for m in msgs}
    for m in by_id.values():
        if m.get("reply_to_author_id"):
            continue
        ref = m.get("reference_id")
        if ref and ref != m["id"]:
            parent = by_id.get(ref)
            if parent and parent["author_id"] != m["author_id"]:
                m["reply_to_author_id"] = parent["author_id"]
        elif not ref:
            for uid in load_json(m.get("mention_users_json")) or []:
                try:
                    uid = int(uid)
                except (TypeError, ValueError):
                    continue
                if uid != m["author_id"]:
                    m["reply_to_author_id"] = uid
                    break


def build_dialog(
    all_msgs: list[dict],
    target_uid: int,
    window_sec: int = CONTEXT_WINDOW_SEC,
) -> tuple[list[dict], str]:
    """Собрать связанные беседы вокруг сообщений целевого пользователя.

    Правила связности (сообщения уже отсортированы по времени):
      * корень ветки — реплика целевого;
      * reply-связь: сообщение с reference_id == id другой реплики любой
        стороны, или ответ адресован целевому (reply_to_author_id);
      * временная близость +-window_sec внутри того же канала.
    Возвращает (уникальные сообщения хронологически, режим связи).
    """
    if not all_msgs:
        return [], "none"
    tgt_ts = [parse_ts(m["timestamp"]) for m in all_msgs]
    tgt_pos = {m["id"]: i for i, m in enumerate(all_msgs)}
    selected: dict[int, dict] = {}
    linked_any = False

    def add(msg: dict) -> None:
        selected[msg["id"]] = msg

    def neighbor_ids(center_idx: int, want_target: bool | None) -> list[int]:
        """Индексы соседей в окне; want_target=None — все, True/False — только
        реплики целевого / собеседников."""
        ts = tgt_ts[center_idx]
        ids = []
        j = center_idx - 1
        while j >= 0 and (ts - tgt_ts[j]).total_seconds() <= window_sec:
            m = all_msgs[j]
            if want_target is None or (m["author_id"] == target_uid) == want_target:
                ids.append(m["id"])
            j -= 1
        j = center_idx + 1
        end = len(all_msgs)
        while j < end and (tgt_ts[j] - ts).total_seconds() <= window_sec:
            m = all_msgs[j]
            if want_target is None or (m["author_id"] == target_uid) == want_target:
                ids.append(m["id"])
            j += 1
        return ids

    for i, m in enumerate(all_msgs):
        if m["author_id"] != target_uid:
            continue
        add(m)
        # 1) прямые reply-связи в обе стороны
        ref = m.get("reference_id")
        if ref and ref in tgt_pos and ref != m["id"]:
            add(all_msgs[tgt_pos[ref]])
            linked_any = True
        # соседи, отвечающие на текущее (дочерние реплики)
        for other in all_msgs[max(0, i - 40): i + 41]:
            if other["id"] == m["id"]:
                continue
            if other.get("reference_id") == m["id"] or (
                other.get("reply_to_author_id") == target_uid
                and other.get("reference_id") in (None, m["id"])
            ):
                add(other)
                linked_any = True
        # 2) временной контекст: реплики собеседников рядом с целевым
        for nid in neighbor_ids(i, want_target=False):
            add(all_msgs[tgt_pos[nid]])
        # и собственные соседние реплики (контекст монолога/темы)
        for nid in neighbor_ids(i, want_target=True):
            add(all_msgs[tgt_pos[nid]])

    mode = "reply+time" if linked_any else "time-only"
    result = sorted(selected.values(), key=lambda x: (x["timestamp"], x["id"]))
    return result, mode


def row_get(row: dict, key: str, default=None):
    """Безопасное чтение колонки: старые/урезанные БД могут не иметь части полей."""
    return row.get(key, default) if isinstance(row, dict) else default


def fetch_attachments(conn) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for r in conn.execute(
        "SELECT message_id, filename, url, proxy_url, content_type, width, height,"
        " is_image, is_spoiler, downloaded, stored_path FROM attachments"
    ):
        out.setdefault(r["message_id"], []).append(dict(r))
    return out


# ---------------------------------------------------------------------------
# Статистика коммуникации (сигналы для психотипирования)
# ---------------------------------------------------------------------------

def strip_mentions(text: str) -> str:
    return MENTION_RE.sub(lambda m: "@" if m.group(1) == "@!" else "#", text or "")


def analyze_messages(msgs: list[dict], all_user_ids: set[int]) -> dict:
    n = len(msgs)
    stats = {
        "messages_total": n,
        "first_message_at": iso(msgs[0]["timestamp"]) if msgs else None,
        "last_message_at": iso(msgs[-1]["timestamp"]) if msgs else None,
        "span_days": None,
        "avg_words_per_message": None,
        "median_words": None,
        "longest_message_words": 0,
        "edited_count": 0,
        "reply_share": 0.0,
        "mention_share": 0.0,
        "question_rate": 0.0,
        "exclamation_rate": 0.0,
        "caps_heavy_share": 0.0,
        "code_block_share": 0.0,
        "link_share": 0.0,
        "emoji_usage_share": 0.0,
        "avg_reactions_received": 0.0,
        "pinned_count": 0,
        "attachment_share": 0.0,
        "active_hours_utc": {},          # 0..23 -> кол-во сообщений
        "active_weekdays": {},           # Mon..Sun -> кол-во
        "burst_sessions": 0,             # серий из >=10 сообщений подряд с интервалом <60с
        "top_emojis": [],
        "hesitation_word_hits": 0,
        "absolute_word_hits": 0,
        "apology_hits": 0,
        "gratitude_hits": 0,
        "self_mentions_of_others": 0,    # как часто автор упоминает других
        "most_mentioned_users": [],
    }
    if not n:
        return stats

    word_counts, hours, weekdays = [], Counter(), Counter()
    questions = exclamations = caps_heavy = codeblocks = links = emojis_used = 0
    replies = mentions = edited = pinned = with_att = 0
    reactions_sum = 0
    emoji_counter: Counter = Counter()
    mentioned_counter: Counter = Counter()
    hesitation = absolute = apologies = gratitude = 0
    burst = 0
    prev_ts: datetime | None = None
    run_len = 0

    for m in msgs:
        text = (m["clean_content"] or m["content"] or "").strip()
        wc = m["word_count"] or len(text.split())
        word_counts.append(wc)
        ts = parse_ts(m["timestamp"])
        hours[ts.hour] += 1
        weekdays[ts.weekday()] += 1

        low = text.lower()
        plain = strip_mentions(text)
        words = re.findall(r"[\w']+", plain.lower())
        hesitation += sum(1 for w in HESITATION_WORDS if w in low)
        absolute += sum(1 for w in ABSOLUTE_WORDS if w in low)
        apologies += sum(1 for w in APOLOGY_WORDS if w in low)
        gratitude += sum(1 for w in GRATITUDE_WORDS if w in low)

        if QUESTION_RE.search(plain):
            questions += 1
        if EXCLAM_RE.search(plain):
            exclamations += 1
        if text and len(ALL_CAP_WORD_RE.findall(text)) >= 2:
            caps_heavy += 1
        if CODEBLOCK_RE.search(text):
            codeblocks += 1
        if URL_RE.search(text):
            links += 1
        found = EMOJI_RE.findall(text)
        if found:
            emojis_used += 1
            for e in found:
                emoji_counter[e] += 1
        for mg in MENTION_RE.finditer(text):
            uid = int(mg.group(2))
            if uid in all_user_ids and uid != m["author_id"]:
                mentioned_counter[uid] += 1
        if m["reference_id"]:
            replies += 1
        if (m["mention_count"] or 0) > 0:
            mentions += 1
        if m["edited_at"]:
            edited += 1
        if m["pinned"]:
            pinned += 1
        if m["has_attachment"]:
            with_att += 1
        reactions_sum += m["reaction_total"] or 0

        if prev_ts is not None and (ts - prev_ts).total_seconds() < 60:
            run_len += 1
            if run_len == 10:
                burst += 1
        else:
            run_len = 0
        prev_ts = ts

    ws = sorted(word_counts)
    first_ts = parse_ts(msgs[0]["timestamp"])
    last_ts = parse_ts(msgs[-1]["timestamp"])

    stats.update({
        "span_days": round((last_ts - first_ts).total_seconds() / 86400, 2),
        "avg_words_per_message": round(sum(word_counts) / n, 2),
        "median_words": ws[n // 2] if n % 2 else round((ws[n // 2 - 1] + ws[n // 2]) / 2, 1),
        "longest_message_words": max(word_counts),
        "edited_count": edited,
        "reply_share": round(replies / n, 3),
        "mention_share": round(mentions / n, 3),
        "question_rate": round(questions / n, 3),
        "exclamation_rate": round(exclamations / n, 3),
        "caps_heavy_share": round(caps_heavy / n, 3),
        "code_block_share": round(codeblocks / n, 3),
        "link_share": round(links / n, 3),
        "emoji_usage_share": round(emojis_used / n, 3),
        "avg_reactions_received": round(reactions_sum / n, 2),
        "pinned_count": pinned,
        "attachment_share": round(with_att / n, 3),
        "active_hours_utc": {str(h): hours.get(h, 0) for h in range(24)},
        "active_weekdays": {
            ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d]: weekdays.get(d, 0)
            for d in range(7)
        },
        "burst_sessions": burst,
        "top_emojis": emoji_counter.most_common(15),
        "hesitation_word_hits": hesitation,
        "absolute_word_hits": absolute,
        "apology_hits": apologies,
        "gratitude_hits": gratitude,
        "self_mentions_of_others": sum(mentioned_counter.values()),
        "most_mentioned_users": mentioned_counter.most_common(10),
    })
    return stats


# ---------------------------------------------------------------------------
# Транскрипт и очистка от PII
# ---------------------------------------------------------------------------

def make_transcript(
    msgs: list[dict],
    max_lines: int | None,
    redact_pii: bool,
    target_uid: int | None = None,
    names: dict[int, str] | None = None,
) -> tuple[str, bool]:
    """Компактная диалоговая лента (экономия токенов — приоритет).

    Без контекста (target_uid=None) — старый формат «[дата время] текст».
    С контекстом — строки вида:
        T 14:03 >5 текст            — реплика целевого пользователя
        O2 14:04 >2 текст           — собеседник O2 (имя — один раз в шапке)
        *                             — начало новой темы/ветки
    Флаги: >N — ответ на реплику N, e/d — edit/delete, [img]/[emb] — медиа.
    Лимит — КОЛИЧЕСТВО СТРОК ЧАТА (не символов): берутся самые СВЕЖИЕ
    max_lines строк (срез от нового к старому), старые отбрасываются.
    max_lines <= 0 / None — полная лента без среза.
    """
    if target_uid is None or names is None:
        return _make_transcript_plain(msgs, max_lines, redact_pii)

    header_rows = list(msgs)
    labels: dict[int, str] = {}
    order: list[tuple[str, int]] = [("T", target_uid)]
    for m in header_rows:
        aid = m["author_id"]
        if aid == target_uid or aid in labels:
            continue
        lab = f"O{len(order)}"
        labels[aid] = lab
        order.append((lab, aid))
    for m in header_rows:
        rid = m.get("reply_to_author_id")
        if rid and rid != target_uid and rid not in labels:
            lab = f"O{len(order)}"
            labels[rid] = lab
            order.append((lab, rid))

    legend = ", ".join(f"{lab}={names.get(uid, uid)}" for lab, uid in order)
    head = (f"# целевой=T; {legend}; "
            "формат: 'автор ЧЧ:ММ [>N|@Uk][ed][img] текст', >N — ответ на реплику N, "
            "@Uk — адресат ответа, *=новая тема")

    lines: list[str] = []
    id_to_num: dict[int, int] = {}
    prev_ts_dt = None
    num = 0
    for m in header_rows:
        num += 1
        id_to_num[m["id"]] = num
        ts = parse_ts(m["timestamp"])
        day = ts.strftime("%Y-%m-%d")
        new_day = prev_ts_dt is None or ts.date() != prev_ts_dt.date()
        gap_min = (ts - prev_ts_dt).total_seconds() / 60 if prev_ts_dt else 999
        label = "T" if m["author_id"] == target_uid else labels.get(
            m["author_id"], f"?{m['author_id']}")
        text = strip_mentions((m["clean_content"] or m["content"] or "").strip())
        text = re.sub(r"\s+", " ", text)
        if not text:
            text = "[msg]"
        parts = []
        if new_day:
            parts.append(f"# {day}")
        elif gap_min > 15:
            parts.append("*")
        flags = ""
        ref = m.get("reference_id")
        if ref and ref in id_to_num and ref != m["id"]:
            flags += f">{id_to_num[ref]}"
        elif m.get("reply_to_author_id"):
            rid = m["reply_to_author_id"]
            flags += "@" + ("T" if rid == target_uid else labels.get(rid, "?"))
        if m.get("edited_at"):
            flags += "e"
        if m.get("deleted_at"):
            flags += "d"
        atts = ""
        if m.get("has_attachment"):
            atts += "[img]"
        if m.get("embed_count"):
            atts += "[emb]"
        body = " ".join(x for x in (f"{label} {ts.strftime('%H:%M')}", flags, text, atts) if x)
        lines.append(f"{parts[0]} {body}".strip() if parts else body)
        prev_ts_dt = ts

    # срез по КОЛИЧЕСТВУ СТРОК чата: берём самые СВЕЖИЕ (конец ленты),
    # старые строки отбрасываются; шапка-легенда всегда сохраняется.
    if max_lines and max_lines > 0 and len(lines) > max_lines:
        body_lines = lines[-max_lines:]
        truncated = True
    else:
        body_lines, truncated = lines, False   # лимит не задан: полная лента
    out_lines = [head] + body_lines
    if redact_pii:
        out_lines = [redact(l) for l in out_lines]
    return "\n".join(out_lines), truncated


def _make_transcript_plain(msgs: list[dict], max_lines: "int | None", redact_pii: bool) -> tuple[str, bool]:
    """Старый одиночный формат: [2026-10-07 14:03] текст.

    Лимит — количество строк чата; берутся самые свежие (срез от нового
    к старому). max_lines <= 0 / None — полная лента без среза.
    """
    lines = []
    limit_on = bool(max_lines) and max_lines > 0
    for m in reversed(msgs):  # идём от свежих к старым
        ts = parse_ts(m["timestamp"])
        text = (m["clean_content"] or m["content"] or "").strip()
        if not text and not m["has_attachment"]:
            continue
        meta = []
        if m["reference_id"]:
            meta.append("reply")
        if m["has_attachment"]:
            meta.append("attachment")
        if m["embed_count"]:
            meta.append(f"embed x{m['embed_count']}")
        tag = f" [{','.join(meta)}]" if meta else ""
        line = f"[{ts.strftime('%Y-%m-%d %H:%M')}] {text}{tag}"
        if redact_pii:
            line = redact(line)
        lines.append(line)
        if limit_on and len(lines) >= max_lines:
            break
    truncated = limit_on and len(msgs) > len(lines)
    return "\n".join(reversed(lines)), truncated


PII_PATTERNS = [
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[email]"),
    # телефон ДО IP; leading-digit вариант требует пробела до и после, чтобы
    # не съесть ISO-дату в [2026-10-19 13:01] (там скобка сразу после цифр)
    (re.compile(r"\+\d[\d \-.()]{6,}\d|(?<= )\d[\d \-.()]{8,}\d(?= |$)"),
     lambda m: "[phone]" if sum(c.isdigit() for c in m.group()) >= 7 else m.group()),
    (re.compile(r"(?<![\d.])\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![\d.])"), "[ip]"),
    (re.compile(r"(?:^|\s)(?:[A-Z]{2}\d{10,14}|[a-z]{2}\d{9,12})(?:\s|$)"), "[iban]"),
]


def redact(text: str) -> str:
    for pat, repl in PII_PATTERNS:
        text = pat.sub(repl, text)
    return text


def sanitize(value, redact_pii: bool):
    """Рекурсивная чистка строковых полей профиля."""
    if isinstance(value, str):
        return redact(value) if redact_pii else value
    if isinstance(value, list):
        return [sanitize(v, redact_pii) for v in value]
    if isinstance(value, dict):
        return {k: sanitize(v, redact_pii) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# Сборка «дела» пользователя
# ---------------------------------------------------------------------------

PROFILE_SYSTEM_PROMPT = """Ты — аналитик по профайлингу личности. Тебе передано \
«дело» пользователя Discord: профиль, метаданные аккаунта, статистика коммуникации \
и хронологический транскрипт его сообщений. Задача: оценить психотип и поведение.

Верни СТРОГО JSON со схемой:
{
  "big_five": {"openness": 0-100, "conscientiousness": 0-100, "extraversion": 0-100,
               "agreeableness": 0-100, "neuroticism": 0-100},
  "mbti_guess": {"type": "INTP", "confidence": 0.0-1.0, "reasoning": "..."},
  "enneagram_guess": {"type": "5w6", "confidence": 0.0-1.0, "reasoning": "..."},
  "communication_style": "...",   // лаконичный портманто стиля общения
  "emotional_tone": "...",        // доминирующий тон, триггеры
  "interests": ["..."],
  "social_roles": ["..."],        // лидер/генератор идей/модератор/наблюдатель...
  "risk_flags": ["..."],          // агрессия, манипуляции, нестабильность — если есть
  "evidence": [{"signal": "...", "example": "..."}],  // 5-10 цитат-обоснований
  "confidence_overall": 0.0-1.0,
  "disclaimer": "..."             // ограничения вывода
}
Опирайся только на переданные данные; при недостатке данных честно снижай confidence."""


def build_dossier(user, guild_profiles, roles, msgs, att_by_msg,
                  all_user_ids, args, generated_at,
                  dialog_msgs: list[dict] | None = None,
                  dialog_mode: str | None = None,
                  names: dict[int, str] | None = None) -> dict:
    uname = user["global_name"] or user["username"] or str(user["user_id"])
    stats = analyze_messages(msgs, all_user_ids)
    if dialog_msgs is not None and not args.no_context:
        transcript, truncated = make_transcript(
            dialog_msgs, args.max_lines, args.redact_pii,
            target_uid=user["user_id"], names=names or {},
        )
    else:
        transcript, truncated = make_transcript(msgs, args.max_lines, args.redact_pii)

    attachments_summary = []
    images = []
    for m in msgs:
        for a in att_by_msg.get(m["id"], []):
            entry = {
                "message_id": m["id"],
                "filename": a["filename"],
                "content_type": a["content_type"],
                "is_image": bool(a["is_image"]),
                "spoiler": bool(a["is_spoiler"]),
                "local_path": a["stored_path"] if a["downloaded"] else None,
                "url": a["proxy_url"] or a["url"],
            }
            attachments_summary.append(entry)
            if a["is_image"]:
                images.append(entry)

    dossier = {
        "meta": {
            "generated_at": generated_at,
            "source": "discord_profiler collection DB",
            "purpose": "LLM profiling input (psychotype, communication style)",
            "transcript_truncated": truncated,
            "transcript_lines": max(0, transcript.count("\n")),
            "line_limit": int(args.max_lines) if args.max_lines else None,
            "pii_redacted": bool(args.redact_pii),
        },
        "identity": {
            "user_id": str(user["user_id"]),
            "username": user["username"],
            "discriminator": row_get(user, "discriminator"),
            "global_name": user["global_name"],
            "account_created_at": iso(row_get(user, "created_at_discord")),
            "locale": row_get(user, "locale"),
            "timezone": row_get(user, "timezone_name"),
            "bot": bool(row_get(user, "bot", 0)),
        },
        "profile": sanitize({
            "about": row_get(user, "about"),
            "avatar_url": row_get(user, "avatar_url"),
            "banner_url": row_get(user, "banner_url"),
            "accent_color": row_get(user, "accent_color"),
            "badges": load_json(row_get(user, "badges_json")),
            "flags": load_json(row_get(user, "flags_raw")),
            "public_flags": row_get(user, "public_flags"),
            "connections": load_json(row_get(user, "connections_json")),
            "premium_type": row_get(user, "premium_type"),
            "mutual": row_get(user, "mutual"),
        }, args.redact_pii),
        "guild_presence": sanitize([
            {
                "guild_id": str(g["guild_id"]),
                "nick": row_get(g, "nick"),
                "joined_at": iso(row_get(g, "joined_at")),
                "boosting_since": iso(row_get(g, "boosting_since")),
                "pending": row_get(g, "pending"),
                "mobile": row_get(g, "mobile"),
                "timeout_until": iso(row_get(g, "timeout_until")),
                "color": row_get(g, "color"),
                "top_role": row_get(g, "top_role"),
            } for g in guild_profiles
        ], args.redact_pii),
        "roles_tags": [
            {"role_id": str(r["role_id"]), "name": r["role_name"],
             "position": r["position"], "guild_id": str(r["guild_id"])}
            for r in roles
        ],
        "statistics": stats,
        "dialog_context": {
            "mode": dialog_mode or ("off" if args.no_context else "none"),
            "linked_messages": len(dialog_msgs) if dialog_msgs is not None else 0,
            "note": ("транскрипт содержит связанные реплики собеседников: "
                     "reply-цепочки (>N/@Uk) и временной контекст +-120 c; "
                     "T — целевой пользователь, O2..On — остальные участники"),
        },
        "media": {
            "attachments_total": len(attachments_summary),
            "images_total": len(images),
            "images": images[:200],
        },
        "transcript": transcript,
    }
    return dossier


def render_prompt_md(dossier: dict) -> str:
    d = dossier
    ident = d["identity"]
    st = d["statistics"]
    prof = d["profile"]
    lines = [
        f"# Профайлинг: {ident['global_name'] or ident['username']} "
        f"(id {ident['user_id']})",
        "",
        "## Системная инструкция",
        PROFILE_SYSTEM_PROMPT,
        "",
        "## Данные дела",
        "```json",
        json.dumps(
            {k: v for k, v in d.items() if k != "transcript"},
            ensure_ascii=False, indent=1, default=str,
        ),
        "```",
        "",
        "## Транскрипт сообщений (хронологический)",
        "```",
        d["transcript"] or "(нет сообщений)",
        "```",
        "",
        f"Всего сообщений в деле: {st['messages_total']}. "
        + (f"Транскрипт ПОЛНЫЙ, строк чата: {d['meta']['transcript_lines']}. "
           if not d["meta"]["transcript_truncated"]
           else f"ВНИМАНИЕ: лента срезана по лимиту строк "
                f"({d['meta']['line_limit']}), показаны только самые свежие "
                f"{d['meta']['transcript_lines']} строк чата; старые дни отброшены. ")
        + f"Период данных: {st.get('first_message_at')} .. "
        f"{st.get('last_message_at')}, {st.get('span_days')} дн.",
        "Ответь только валидным JSON по схеме из системной инструкции.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def slug(name: str) -> str:
    name = unicodedata.normalize("NFKC", name or "unknown")
    name = re.sub(r"[^\w\-]+", "_", name).strip("_")
    return name[:40] or "unknown"


def _env_int(name: str, default: int) -> int:
    """Чтение целого из ENV (.env уже подгружен); некорректное значение -> default."""
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except ValueError:
        return default


def parse_args():
    # настройки по умолчанию берутся из .env (EXPORT_MAX_LINES / EXPORT_CONTEXT_WINDOW),
    # их может переопределить флаг командной строки
    env_max_lines = _env_int("EXPORT_MAX_LINES", 0)
    env_ctx_window = _env_int("EXPORT_CONTEXT_WINDOW", CONTEXT_WINDOW_SEC)
    p = argparse.ArgumentParser(
        description="Экспорт данных для LLM-профайлинга.\n"
                    "Без аргументов — выгрузка ВСЕХ участников канала.\n"
                    "Позиционный аргумент — фильтр одного пользователя по username,\n"
                    "display-имени или user_id: python export_profiles.py fluff\n"
                    "Лимиты ленты читаются из .env: EXPORT_MAX_LINES, EXPORT_CONTEXT_WINDOW")
    p.add_argument("target", nargs="?", default="",
                   help="username / имя / user_id одного пользователя (пусто = все)")
    p.add_argument("--db", default="data/profiling.db", help="путь к SQLite базе")
    p.add_argument("--out", default="out", help="каталог результата")
    p.add_argument("--users", default="", help="список user_id через запятую (по умолчанию все)")
    p.add_argument("--min-messages", type=int, default=1,
                   help="минимум сообщений, чтобы экспортить пользователя")
    p.add_argument("--max-lines", type=int, default=env_max_lines, metavar="N",
                   dest="max_lines",
                   help=f"лимит КОЛИЧЕСТВА СТРОК чата на транскрипт; берутся самые "
                        f"СВЕЖИЕ N строк (срез от нового к старому), строки текста "
                        f"не обрезаются. 0 = без лимита (полная лента). "
                        f"По умолчанию из .env EXPORT_MAX_LINES={env_max_lines}")
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
    p.add_argument("--since", default="", metavar="YYYY-MM-DD[THH:MM]",
                   help="включать в транскрипт только сообщения с этой даты/времени (UTC)")
    p.add_argument("--until", default="", metavar="YYYY-MM-DD[THH:MM]",
                   help="включать в транскрипт только сообщения до этой даты/времени (UTC)")
    args = p.parse_args()
    if args.legacy_max_msg_chars is not None:
        print("[WARN] --max-msg-chars устарел и игнорируется: лимит теперь "
              "по КОЛИЧЕСТВУ СТРОК чата (--max-lines N или EXPORT_MAX_LINES в .env).")
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
    db_path = Path(args.db)
    out_dir = Path(args.out)
    conn = open_db(db_path)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    users = fetch_users(conn)
    guild_map = fetch_guild_profiles(conn)
    role_map = fetch_roles(conn)
    msg_map = fetch_messages(conn)
    att_map = fetch_attachments(conn)
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
            )
            fname = f"{uid}_{slug(u['username'])}"
            jl.write(json.dumps(dossier, ensure_ascii=False, default=str) + "\n")
            if args.per_user_json:
                (profiles_dir / f"{fname}.json").write_text(
                    json.dumps(dossier, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
            if not args.no_prompts:
                (prompts_dir / f"{fname}.md").write_text(
                    render_prompt_md(dossier), encoding="utf-8")
            st = dossier["statistics"]
            index.append({
                "user_id": str(uid),
                "display_name": u["global_name"] or u["username"],
                "messages": st["messages_total"],
                "span_days": st["span_days"],
                "transcript_truncated": dossier["meta"]["transcript_truncated"],
                "file": f"profiles/{fname}.json" if args.per_user_json else "dataset.jsonl",
                "prompt": None if args.no_prompts else f"prompts/{fname}.md",
            })
            exported += 1
            print(f"  ✔ {u['username']} ({uid}): {st['messages_total']} сообщ.")

    summary = {
        "generated_at": generated_at,
        "db": str(db_path),
        "users_total": len(users),
        "exported": exported,
        "skipped_low_data": skipped,
        "min_messages": args.min_messages,
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


if __name__ == "__main__":
    main()
