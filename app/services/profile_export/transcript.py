"""app.services.profile_export.transcript — часть модуля экспорта «дел» для LLM.

Автономный экспорт из PostgreSQL (SQLAlchemy + stdlib), без Discord.
Сгенерирован разделением export_profiles.py; публичный CLI — export_profiles.py.
"""
from __future__ import annotations

import re
from .env import load_json, parse_ts
from .stats import classify_reaction, reaction_display, strip_mentions
from .textclean import redact, strip_urls_text


DEFAULT_PROMPT_MAX_KB = 300   # максимальный вес файла промпта (.md), КБ


def _utf8_len(s: str) -> int:
    """Вес строки в байтах UTF-8 (переносы считаются как \\n)."""
    return len((s or "").encode("utf-8"))


def reaction_line_tag(reaction: dict) -> str | None:
    """Короткая метка реакции для строки транскрипта.

    unicode-эмодзи пишется как есть, кастомная реакция — :name:, после чего
    добавляется эмоциональный кластер в скобках, если он известен:
        ❤️(support_affection)  :blobcat:(laughter_humor)
    """
    disp = reaction_display(reaction)
    if disp == "?":
        return None
    emo = classify_reaction(reaction)
    return f"{disp}({emo})" if emo else disp


def message_reactions_compact(m: dict, limit: int = 5) -> str:
    """'👍3x ❤️2x' — компактный блок реакций на сообщение (для транскрипта)."""
    rl = load_json(m.get("reactions_json"))
    if not isinstance(rl, list):
        return ""
    parts = []
    for r in rl[:limit]:
        if not isinstance(r, dict):
            continue
        cnt = int(r.get("count") or 0)
        tag = reaction_line_tag(r)
        if not tag:
            continue
        parts.append(f"{tag}{cnt}x" if cnt > 1 else tag)
    return " ".join(parts)


def _make_transcript_plain(msgs: list[dict], max_lines: "int | None",
                           redact_pii: bool,
                           max_bytes: int | None = None) -> tuple[str, bool]:
    """Старый одиночный формат: [2026-10-07 14:03] текст.

    Лимит — вес ленты в байтах (max_bytes); берутся самые свежие целые
    строки (срез от нового к старому). Если max_bytes не задан — устаревший
    лимит по количеству строк (max_lines). <=0 / None нигде — полная лента.
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
        rx = message_reactions_compact(m)  # реакции с эмоциональной разметкой
        if rx:
            meta.append(f"reactions: {rx}")
        tag = f" [{','.join(meta)}]" if meta else ""
        line = f"[{ts.strftime('%Y-%m-%d %H:%M')}] {text}{tag}"
        if redact_pii:
            line = redact(line)
        lines.append(line)
        if limit_on and len(lines) >= max_lines:
            break
    truncated = limit_on and len(msgs) > len(lines)
    if max_bytes and max_bytes > 0:
        kept: list[str] = []
        used = 0
        for l in lines:                    # lines уже от новых к старым
            w = _utf8_len(l) + (1 if kept else 0)   # перенос между строками
            if kept and used + w > max_bytes:
                break
            kept.append(l)                 # первая строка берётся всегда —
            used += w                      # лента не бывает пустой
        truncated = len(kept) < len(lines)
        lines = kept
    return "\n".join(reversed(lines)), truncated


def make_transcript(
    msgs: list[dict],
    max_lines: int | None,
    redact_pii: bool,
    target_uid: int | None = None,
    names: dict[int, str] | None = None,
    max_bytes: int | None = None,
    window_days: int | None = None,
) -> tuple[str, bool]:
    """Компактная диалоговая лента (экономия токенов — приоритет).

    Без контекста (target_uid=None) — старый формат «[дата время] текст».
    С контекстом — строки вида:
        T 14:03 >5 текст            — реплика целевого пользователя
        O2 14:04 >2 текст           — собеседник O2 (имя — один раз в шапке)
        *                             — начало новой темы/ветки
    Флаги: >N — ответ на реплику N, e/d — edit/delete, [img]/[emb] — медиа.
    Лимит ленты — ВЕС в байтах (max_bytes): остаются самые СВЕЖИЕ целые
    строки (срез от нового к старому), старые отбрасываются; строки текста
    не обрезаются посимвольно. max_bytes <= 0 / None — лимит по количеству
    строк (max_lines; устаревший режим), 0/-1 и там и там = полная лента.
    window_days — скользящее временное окно: старше N дней от последнего
    сообщения не попадает в ленту (сегментация «сейчас vs история»).
    """
    if window_days and window_days > 0 and msgs:
        try:
            from datetime import timedelta
            last_ts = parse_ts(msgs[-1].get("timestamp"))
            if last_ts is not None:
                cutoff = last_ts - timedelta(days=window_days)
                kept = [m for m in msgs
                        if (parse_ts(m.get("timestamp")) or cutoff) >= cutoff]
                if kept:                      # окно не должно обнулять ленту
                    msgs = kept
        except Exception:                     # любые сюрпризы формата времени
            pass                              # — не роняем экспорт, полное окно
    if target_uid is None or names is None:
        return _make_transcript_plain(msgs, max_lines, redact_pii,
                                      max_bytes=max_bytes)

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
            "@Uk — адресат ответа, *=новая тема, «...» — реакции на реплику")

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
        rx = message_reactions_compact(m)  # реакции с эмоциональной разметкой
        body = " ".join(x for x in (f"{label} {ts.strftime('%H:%M')}", flags, text, atts,
                                    f"«{rx}»" if rx else "") if x)
        lines.append(f"{parts[0]} {body}".strip() if parts else body)
        prev_ts_dt = ts

    # срез ленты: основной режим — по ВЕСУ в байтах (max_bytes), остаются
    # самые СВЕЖИЕ целые строки; fallback — устаревший лимит по количеству
    # строк (max_lines). Гарантии обоих режимов: 1) строки целевого
    # пользователя (T) вырезаются в последнюю очередь; 2) транскрипт не
    # бывает пустым при наличии сообщений (минимум одна самая свежая строка);
    # 3) шапка-легенда всегда сохраняется.
    tgt_idx = [i for i, m in enumerate(header_rows) if m["author_id"] == target_uid]
    # строки-разделители дней («# 2026-10-05») не считаем контентом: при
    # жёстком лимите они не должны вытеснять реплики целевого пользователя
    day_row = {i for i, l in enumerate(lines) if l.startswith("# ")}

    if max_bytes and max_bytes > 0:
        # бюджет считается на финальный текст (после redact), поэтому
        # пересчитываем сумму каждый раз — строки короткие, это дёшево
        def _fits(keep: set[int]) -> bool:
            sel = sorted(keep)
            txt = "\n".join([head] + [(redact(lines[i]) if redact_pii else lines[i])
                                      for i in sel])
            return _utf8_len(txt) <= max_bytes

        def _pick_by_weight() -> tuple[set[int], bool]:
            cand = list(range(len(lines) - 1, -1, -1))   # свежие раньше
            keep: set[int] = set()
            for i in cand:
                if i in day_row:
                    continue
                if _fits(keep | {i}):
                    keep.add(i)
            if not keep:
                keep = {len(lines) - 1}                  # лента не может быть пустой
            hdrs = [i for i in sorted(day_row) if i <= max(keep)]
            freshest_day = hdrs[-1] if hdrs else None
            if freshest_day is not None and _fits(keep | {freshest_day}):
                keep.add(freshest_day)
            n_content = sum(1 for l in lines if not l.startswith("# "))
            truncated = len(keep) < n_content or \
                (freshest_day is not None and freshest_day not in keep)
            return keep, truncated

        keep, truncated = _pick_by_weight()
        body_lines = [lines[i] for i in sorted(keep)]
    elif max_lines and max_lines > 0 and len(lines) > max_lines:
        content_budget = max(max_lines - 1, 1)  # 1 слот резервируем под дату
        keep = set(tgt_idx[-content_budget:])    # свежие реплики целевого — приоритет
        extra = [i for i in range(len(lines))
                 if i not in keep and i not in day_row][-max(content_budget - len(keep), 0):]
        keep.update(extra)
        if not keep:
            keep = {len(lines) - 1}
        # самая свежая дата в рамках лимита (иначе «Период» потеряет день);
        # при нехватке места уступают чужие строки, реплики T — в последнюю очередь
        hdrs = [i for i in sorted(day_row) if i <= (max(keep) if keep else -1)]
        freshest_day = hdrs[-1] if hdrs else None
        if freshest_day is not None and len(keep) + 1 <= max_lines:
            keep.add(freshest_day)
        elif freshest_day is not None and keep:
            droppable = [i for i in keep if i not in tgt_idx and i != freshest_day]
            if droppable:
                keep.discard(min(droppable))
                keep.add(freshest_day)
        body_lines = [lines[i] for i in sorted(keep)]
        truncated = len(keep) < len(lines)
    else:
        body_lines, truncated = lines, False   # лимит не задан: полная лента
    out_lines = [head] + body_lines
    if redact_pii:
        out_lines = [redact(l) for l in out_lines]
    return "\n".join(out_lines), truncated


def ensure_nonempty_transcript(transcript: str, msgs: list[dict],
                               redact_pii: bool) -> str:
    """Гарантия: если у пользователя есть сообщения, транскрипт не пустой.
    Вставка самого свежего сообщения в однопользовательской ленте."""
    if transcript.strip():
        return transcript
    fresh = next((m for m in reversed(msgs)
                  if (m.get("clean_content") or m.get("content") or "").strip()
                  or m.get("has_attachment")), None)
    if fresh is None:
        return transcript
    ts = parse_ts(fresh["timestamp"])
    text = strip_urls_text((fresh["clean_content"] or fresh["content"] or "").strip())
    line = f"[{ts.strftime('%Y-%m-%d %H:%M')}] {text or '[msg]'}"
    if redact_pii:
        line = redact(line)
    return line


_RX_LINE_RE = re.compile(r"\s*«[^»]*»\s*$")


def _strip_reactions_from_transcript(t: str) -> str:
    """Убирает построчные блоки реакций «...» из конца строк транскрипта.

    Эмоциональный разбор реакций уже есть в агрегатах stats
    (reactions_emotions / reactions_top / own_emoji_emotions), а сами символы
    эмодзи — в тексте сообщений; дублировать их в каждой строке — лишние байты."""
    out = []
    for ln in t.splitlines():
        m = _RX_LINE_RE.search(ln)
        if m and ("x" in m.group(0) or any(ord(c) > 0x2500 for c in m.group(0))):
            ln = ln[:m.start()].rstrip()
        out.append(ln)
    return "\n".join(out)
