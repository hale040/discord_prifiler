"""app.services.profile_export.dialog — часть модуля экспорта «дел» для LLM.

Автономный экспорт из PostgreSQL (SQLAlchemy + stdlib), без Discord.
Сгенерирован разделением export_profiles.py; публичный CLI — export_profiles.py.
"""
from __future__ import annotations

import re

from .env import load_json, parse_ts


CONTEXT_WINDOW_SEC = 120  # окно «соседних» реплик для восстановления ветки

# ">" опционален: не терять пинги при обрезке/редации; [0-9] вместо \d —
# совместимость с окружениями, где \d ведёт себя иначе; ID в capture-группе 1.
USER_MENTION_RE = re.compile(r"<@!?([0-9]{5,25})>?")

# Строгий вариант: ОБЯЗАТЕЛЬНЫЙ закрывающий ">" и только user-формат
# (<@id>/<@!id>). Роли <@&id> не матчатся. Используется для правил
# связности (пинг = обращение): мягкий regex выше матчит и обрезанные
# токены вида "<@123" внутри текста/кода — из-за этого в ленту целевого
# попадали чужие «несвязанные» сообщения.
_STRICT_MENTION_RE = re.compile(r"<@!?([0-9]{5,25})>")


def _pings(msg) -> list[int]:
    """ID, упомянутые в тексте сообщения (<@id>/<@!id>). Принимает dict или str."""
    if isinstance(msg, dict):
        raw = msg.get("content") or msg.get("clean_content") or ""
    else:
        raw = msg or ""
    out = []
    for x in _STRICT_MENTION_RE.finditer(str(raw)):
        uid = int(x.group(1))
        if uid not in out:
            out.append(uid)
    return out


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
        # ВАЖНО: упоминание (<@id>) БОЛЬШЕ не трактуется как ответ
        # (reply_to_author_id). Пинг без reply — это обращение к человеку,
        # оно учитывается отдельным флагом pUk и связью в build_dialog,
        # но не превращается в «ответ», иначе лента наполнялась чужими
        # сообщениями, никак не связанными с целевым пользователем.


def build_dialog(
    all_msgs: list[dict],
    target_uid: int,
    window_sec: int | None = None,
    time_window_sec: int | None = None,
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
    # единый источник окон: env EXPORT_CONTEXT_WINDOW (window_sec) и
    # EXPORT_TIME_WINDOW (узкое окно собеседников); явные аргументы — приоритет.
    import os as _os
    if window_sec is None:
        try:
            window_sec = int(_os.getenv("EXPORT_CONTEXT_WINDOW") or CONTEXT_WINDOW_SEC)
        except ValueError:
            window_sec = CONTEXT_WINDOW_SEC
    if time_window_sec is None:
        try:
            time_window_sec = int(_os.getenv("EXPORT_TIME_WINDOW") or 30)
        except ValueError:
            time_window_sec = 30
    tgt_ts = [parse_ts(m["timestamp"]) for m in all_msgs]
    tgt_pos = {m["id"]: i for i, m in enumerate(all_msgs)}
    selected: dict[int, dict] = {}
    linked_any = False

    def add(msg: dict) -> None:
        selected[msg["id"]] = msg

    def neighbor_ids(center_idx: int, want_target: bool | None,
                     wsec: int | None = None) -> list[int]:
        """Индексы соседей в окне; want_target=None — все, True/False — только
        реплики целевого / собеседников. wsec — размер окна (по умолчанию общий)."""
        wsec = window_sec if wsec is None else wsec
        ts = tgt_ts[center_idx]
        ids = []
        j = center_idx - 1
        while j >= 0 and (ts - tgt_ts[j]).total_seconds() <= wsec:
            m = all_msgs[j]
            if want_target is None or (m["author_id"] == target_uid) == want_target:
                ids.append(m["id"])
            j -= 1
        j = center_idx + 1
        end = len(all_msgs)
        while j < end and (tgt_ts[j] - ts).total_seconds() <= wsec:
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
        # 1b) ПИНГИ: целевой упомянут в чужом сообщении — берём только само
        # сообщение-пинг (обращение к целевому), без лавины соседних реплик.
        # Окно ограничено window_sec вокруг реплики целевого: раньше скан
        # шёл по ВСЕМУ каналу и тащил пинги недельной давности — «несвязанные»
        # сообщения в ленте.
        _mts = parse_ts(m["timestamp"])
        for other in all_msgs[max(0, i - 400): i + 401]:
            if other["author_id"] == target_uid or other["id"] in selected:
                continue
            if abs((parse_ts(other["timestamp"]) - _mts).total_seconds()) > window_sec:
                continue
            if target_uid in _pings(other):
                add(other)
                linked_any = True
        # 1c) исходящие пинги целевого: подтягиваем ОДНУ ближайшую предыдущую
        # реплику адресата (контекст обращения), а не всё окно канала.
        # Адресаты — из raw content ИЛИ mention_users_json (если текст вычищен).
        pinged = set(_pings(m))
        for uid in load_json(m.get("mention_users_json")) or []:
            try:
                pinged.add(int(uid))
            except (TypeError, ValueError):
                pass
        pinged.discard(target_uid)
        if pinged:
            lo = max(0, i - 400)
            while lo < i and (parse_ts(m["timestamp"]) - parse_ts(all_msgs[lo]["timestamp"])).total_seconds() > window_sec:
                lo += 1
            for other in reversed(all_msgs[lo:i]):
                if other["author_id"] == target_uid:
                    continue
                if other["author_id"] in pinged and \
                        (parse_ts(m["timestamp"]) - parse_ts(other["timestamp"])).total_seconds() <= window_sec:
                    add(other)
                    linked_any = True
                    break
        # 2) временной контекст: реплики собеседников рядом с целевым.
        # Узкое окно (time_window_sec): раньше ±120сек тянули весь офтопик
        # шумных каналов — в промпт попадали сообщения, не связанные с
        # пользователем. По умолчанию 30 сек.
        for nid in neighbor_ids(i, want_target=False, wsec=time_window_sec):
            add(all_msgs[tgt_pos[nid]])
        # и собственные соседние реплики (контекст монолога/темы)
        for nid in neighbor_ids(i, want_target=True):
            add(all_msgs[tgt_pos[nid]])

    mode = "reply+time" if linked_any else "time-only"
    result = sorted(selected.values(), key=lambda x: (x["timestamp"], x["id"]))
    return result, mode
