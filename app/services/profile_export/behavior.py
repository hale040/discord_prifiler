"""app.services.profile_export.behavior — производные поведенческие метрики.

Обогащение сигналов для промпта: вместо «сырого текста» LLM получает
готовые поведенческие паттерны, которые иначе модель обязана была бы
высчитывать сама по транскрипту (и делала бы это ненадёжно):

* сессионизация потока сообщений (разрыв > SESSION_GAP_SEC = новая сессия);
* инициативность — кто начинает диалог в сессии;
* ответная задержка (медиана/перцентили) и её асимметрия между людьми;
* взаимность переписки A->B / B->A;
* сегментация ЛС vs публичные каналы («социальные маски»);
* тональные маркеры (вопросы/восклицания/caps/эмодзи) отдельно по сегментам;
* мета-данные правок (edit_rate) и реакций (received/given).

Чистая stdlib-логика над словарями сообщений той же формы, что отдаёт
db.fetch_messages (ключи: id, channel_id, guild_id, author_id, timestamp,
clean_content/content, edited_at, reply_user_id, reactions_json, word_count).
Никаких обращений к БД и Discord — модуль легко тестировать.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone

# разрыв тишины, после которого поток считается новой сессией общения
SESSION_GAP_SEC = 30 * 60


def _ts(value) -> datetime | None:
    """Универсальный парсер времени сообщения (aware dt / str / naive)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _text(m: dict) -> str:
    return (m.get("clean_content") or m.get("content") or "").strip()


def _pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    sv = sorted(vals)
    idx = min(len(sv) - 1, max(0, int(round(q * (len(sv) - 1)))))
    return round(float(sv[idx]), 1)


def is_dm(m: dict) -> bool:
    """Сообщение из личных сообщений: сервер отсутствует."""
    return not m.get("guild_id")


def split_segments(msgs: list[dict]) -> tuple[list[dict], list[dict]]:
    """dm, guild — сегментация ленты по источнику (ЛС vs публичные каналы)."""
    dm = [m for m in msgs if is_dm(m)]
    guild = [m for m in msgs if not is_dm(m)]
    return dm, guild


def _sessions(messages: list[dict], gap_sec: int):
    """Разбить отсортированный список на сессии; вернуть [(start, end, items)]."""
    out = []
    cur: list[dict] = []
    prev_ts = None
    for m in messages:
        ts = _ts(m.get("timestamp"))
        if ts is None:
            continue
        if prev_ts is not None and (ts - prev_ts).total_seconds() > gap_sec:
            if cur:
                out.append(cur)
            cur = []
        cur.append(m)
        prev_ts = ts
    if cur:
        out.append(cur)
    return [(s[0], s[-1], s) for s in out]


def sessionize(msgs: list[dict], target_uid: int, *,
               gap_sec: int = SESSION_GAP_SEC) -> dict:
    """Сессии целевого пользователя: сколько он «зашёл в чат сам» и т.д."""
    mine = sorted((m for m in msgs if m.get("author_id") == target_uid),
                  key=lambda m: _ts(m.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc))
    sessions = _sessions(mine, gap_sec)
    n = len(sessions)
    lens = [len(s[2]) for s in sessions]
    gaps = []
    for i in range(1, n):
        a = _ts(sessions[i - 1][1].get("timestamp"))
        b = _ts(sessions[i][0].get("timestamp"))
        if a and b:
            gaps.append((b - a).total_seconds())
    return {
        "gap_min": round(gap_sec / 60),
        "sessions_total": n,
        "avg_len": round(statistics.mean(lens), 1) if lens else 0,
        "max_len": max(lens) if lens else 0,
        "median_gap_hours": (_pct([g / 3600 for g in gaps], 0.5)),
    }


def initiative_and_latency(msgs: list[dict], target_uid: int, *,
                           gap_sec: int = SESSION_GAP_SEC) -> dict:
    """Инициативность + ответные задержки + взаимность по собеседникам."""
    ordered = sorted(
        (m for m in msgs if _ts(m.get("timestamp"))),
        key=lambda m: _ts(m.get("timestamp")))
    sessions = _sessions(ordered, gap_sec)

    init_by = Counter()          # uid -> сколько сессий начат им
    part_by = Counter()          # uid -> участие в сессиях (для доли)
    resp_lat: dict[tuple[int, int], list[float]] = defaultdict(list)  # (asker, answerer) -> сек
    pairs: dict[tuple[int, int], int] = Counter()   # directed пары сообщений

    for _, _, items in sessions:
        authors = {m["author_id"] for m in items}
        first = items[0]["author_id"]
        init_by[first] += 1
        for a in authors:
            part_by[a] += 1
        # задержка ответа: первое сообщение B после сообщения A в этой сессии
        last_by = {}
        for m in items:
            uid, ts = m["author_id"], _ts(m.get("timestamp"))
            for other, ots in list(last_by.items()):
                if other != uid:
                    resp_lat[(other, uid)].append((ts - ots).total_seconds())
            last_by[uid] = ts
        # directed-взаимодействия соседних реплик разных авторов
        for a, b in zip(items, items[1:]):
            if a["author_id"] != b["author_id"]:
                pairs[(a["author_id"], b["author_id"])] += 1

    def lat_stats(a: int, b: int) -> dict | None:
        vals = resp_lat.get((a, b))
        if not vals:
            return None
        return {"n": len(vals),
                "median_sec": round(statistics.median(vals), 1),
                "p90_sec": _pct(vals, 0.9)}

    top_partners = [u for u, _ in part_by.most_common(8) if u != target_uid]
    partners = []
    for p in top_partners:
        share = round(init_by[p] / part_by[p], 2) if part_by[p] else None
        t_share = round(init_by[target_uid] / part_by[target_uid], 2) \
            if part_by[target_uid] else None
        la, lb = lat_stats(target_uid, p), lat_stats(p, target_uid)
        ab = pairs.get((target_uid, p), 0)
        ba = pairs.get((p, target_uid), 0)
        ratio = round(ab / ba, 2) if ba else None
        entry = {"user_id": str(p),
                 "sessions_with_target": part_by[p],
                 "initiative_share": share,
                 "latency_you_to_them_sec": (la or {}).get("median_sec"),
                 "latency_them_to_you_sec": (lb or {}).get("median_sec"),
                 "reciprocity_ratio": ratio}
        if la and lb and la["n"] >= 3 and lb["n"] >= 3:
            asym = round(la["median_sec"] / lb["median_sec"], 2)
            entry["asymmetry"] = asym     # >1 — человек отвечает тебе медленнее, чем ты ему
        partners.append(entry)

    return {
        "target_initiative_share": (round(init_by[target_uid] / part_by[target_uid], 2)
                                    if part_by[target_uid] else None),
        "target_first_starts": init_by[target_uid],
        "partners": partners,
    }


def tone_markers(msgs: list[dict]) -> dict:
    """Тональная агрегация: вопросы/восклицания/caps/эмодзи/средняя длина."""
    n = len(msgs)
    if not n:
        return {"messages": 0}
    q = e = caps = emo = wsum = ed = 0
    for m in msgs:
        t = _text(m)
        if not t:
            continue
        if "?" in t:
            q += 1
        if "!" in t:
            e += 1
        letters = [c for c in t if c.isalpha()]
        if len(letters) >= 8 and sum(c.isupper() for c in letters) / len(letters) > 0.6:
            caps += 1
        if any(ord(c) > 0x2100 for c in t):
            emo += 1
        wsum += m.get("word_count") or len(t.split())
        if m.get("edited_at"):
            ed += 1
    return {
        "messages": n,
        "question_rate": round(q / n, 3),
        "exclamation_rate": round(e / n, 3),
        "caps_heavy_share": round(caps / n, 3),
        "emoji_usage_share": round(emo / n, 3),
        "avg_words": round(wsum / n, 1),
        "edit_rate": round(ed / n, 3),
    }


def _count_reactions(raw) -> int:
    if not raw:
        return 0
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return 0
    if isinstance(data, list):
        return sum(int(r.get("count", 1) or 1) for r in data if isinstance(r, dict))
    return 0


def reaction_meta(msgs: list[dict], target_uid: int,
                  given_events: list[dict] | None = None) -> dict:
    """Мета-данные реакций: сколько получили (топливо/статусность) и дали сами."""
    received = sum(_count_reactions(m.get("reactions_json"))
                   for m in msgs if m.get("author_id") == target_uid)
    per_msg = round(received / max(1, sum(1 for m in msgs
                                          if m.get("author_id") == target_uid)), 2)
    given = 0
    given_emojis: Counter = Counter()
    for ev in given_events or []:
        if int(ev.get("user_id") or 0) == target_uid and ev.get("action") == "add":
            given += 1
            given_emojis[str(ev.get("emoji") or "?")] += 1
    return {
        "received_total": received,
        "received_per_message": per_msg,
        "given_total": given,
        "top_given_emojis": [{"emoji": k, "count": v}
                             for k, v in given_emojis.most_common(5)],
    }


def presence_digest(snapshots: list[dict]) -> dict:
    """Циркадный ритм и паттерны присутствия по presence_snapshots (v10)."""
    hours: Counter = Counter()
    online = idle = dnd = offline = 0
    customs: Counter = Counter()
    games: Counter = Counter()
    days: set = set()
    for s in snapshots or []:
        ts = _ts(s.get("observed_at") or s.get("captured_at"))
        if ts:
            hours[ts.hour] += 1
            days.add(ts.date())
        st = str(s.get("status") or "").lower()
        if st == "online":
            online += 1
        elif st == "idle":
            idle += 1
        elif st == "dnd":
            dnd += 1
        else:
            offline += 1
        cs = s.get("status_text") or s.get("custom_status")
        if cs:
            customs[str(cs)] += 1
        act = s.get("activity_name") or s.get("activity")
        if act:
            games[str(act)] += 1
    total = max(1, len(snapshots or []))
    night = sum(v for h, v in hours.items() if h < 6)
    return {
        "observations": len(snapshots or []),
        "days_covered": len(days),
        "status_mix": {"online": round(online / total, 2),
                       "idle": round(idle / total, 2),
                       "dnd": round(dnd / total, 2),
                       "offline": round(offline / total, 2)},
        "night_presence_share": round(night / total, 2),   # 00:00–06:00 UTC
        "peak_hours_utc": [h for h, _ in sorted(hours.items(),
                                                 key=lambda kv: -kv[1])[:4]],
        "custom_status_top": [{"status": k, "count": v}
                              for k, v in customs.most_common(8)],
        "activities_top": [{"activity": k, "count": v}
                           for k, v in games.most_common(8)],
    }


def build_behavior_block(msgs: list[dict], target_uid: int, *,
                         given_events: list[dict] | None = None,
                         presence_snaps: list[dict] | None = None) -> dict:
    """Единый блок «Поведение» для досье/промпта."""
    dm, guild = split_segments(msgs)
    return {
        "segments": {
            "dm": {"messages": len(dm), **tone_markers(dm)},
            "guild": {"messages": len(guild), **tone_markers(guild)},
        },
        "sessions": sessionize(msgs, target_uid),
        "initiative_latency": initiative_and_latency(msgs, target_uid),
        "tone_overall": tone_markers(msgs),
        "reactions": reaction_meta(msgs, target_uid, given_events),
        "presence": presence_digest(presence_snaps),
    }
