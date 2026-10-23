"""app.services.profile_export.dialog — часть модуля экспорта «дел» для LLM.

Автономный экспорт из PostgreSQL (SQLAlchemy + stdlib), без Discord.
Сгенерирован разделением export_profiles.py; публичный CLI — export_profiles.py.
"""
from __future__ import annotations

from .env import load_json, parse_ts


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
