"""app.services.profile_export.stats — часть модуля экспорта «дел» для LLM.

Автономный экспорт из PostgreSQL (SQLAlchemy + stdlib), без Discord.
Сгенерирован разделением export_profiles.py; публичный CLI — export_profiles.py.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from .env import iso, load_json, parse_ts


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


def strip_mentions(text: str) -> str:
    return MENTION_RE.sub(lambda m: "@" if m.group(1) == "@!" else "#", text or "")


EMOTION_EMOJI: dict[str, set[str]] = {
    "support_agreement": {
        "👍", "👌", "🤝", "👏", "🙌", "✔️", "✅", "🫡", "💪", "👊", "🤞", "✊",
    },
    "affection_gratitude": {
        "❤️", "🧡", "💛", "💚", "💙", "💜", "🖤", "🤍", "🤎", "💕", "💞", "💓",
        "💗", "💖", "💘", "💝", "🥰", "😍", "😘", "🫶", "🤗", "🌹", "🙏",
    },
    "laughter_humor": {
        "😂", "🤣", "😆", "😅", "😹", "💀", "🫠", "😁", "😀", "lol", "lmao",
        "rofl", "kek", "hehe", "haha", "200", "joy",
    },
    "sadness_sympathy": {
        "😢", "😭", "😞", "😔", "😟", "🙁", "☹️", "😕", "💔", "😪", "🥱", "😧",
        "😦", "🌧", "⛈", "rain", "broken_heart", "sob", "cry", "frowning",
    },
    "anger_irritation": {
        "😠", "😡", "🤬", "😤", "👿", "💢", "🖕", "😾", "rage", "anger",
        "enraged", "upset", "mad",
    },
    "fear_anxiety": {
        "😨", "😰", "😱", "😳", "🥺", "😬", "fear", "scream",
        "anguish", "anxious", "nervous",
    },
    "shock_surprise": {
        "😲", "😮", "🤯", "❗", "❕", "⁉️", "shock", "astonished",
        "mind_blown", "whoa",
    },
    "disgust_contempt": {
        "🤢", "🤮", "🥴", "😒", "🙄", "😏", "🤡", "disgust", "nauseated",
        "unamused", "roll_eyes", "smirk", "clown",
    },
    "celebration_pride": {
        "🎉", "🎊", "🥳", "🏆", "🎖", "🥇", "🍾", "👑", "🚀", "tada", "party",
        "trophy", "medal", "champagne", "crown", "rocket",
    },
    "thinking_analysis": {
        "🤔", "🧐", "💭", "🤨", "think", "ponder", "monocle", "thought",
        "raised_eyebrow",
    },
    "sleep_tired": {
        "😴", "💤", "🌙", "😌", "zzz", "yawn", "sleepy", "moon",
    },
    "hostility_troll": {
        "👎", "🚫", "⚠️", "☠️", "middle_finger", "thumbsdown",
        "no_entry", "skull", "warning",
    },
    "positivity": {  # общий резерв: всё остальное тёплое/нейтрально-позитивное
        "🙂", "🙃", "😉", "😊", "😇", "🤩", "😗", "☺️", "✨", "🌟", "⭐",
        "🔥", "🌈", "☀️", "🌻", "🍀", "💯",
    },
}


_EMOTION_BY_TOKEN: dict[str, str] = {}


def reaction_display(reaction: dict) -> str:
    """Как показать реакцию в промпте: unicode-эмодзи как есть, кастомную — :name:.

    Поддерживает оба формата хранения reactions_json: плоский
    {"name":..,"count":..} и старый вложенный {"emoji":{"name":..},"count":..}.
    """
    if not isinstance(reaction, dict):
        return "?"
    emoji = reaction.get("emoji")
    name = ""
    if isinstance(emoji, dict):
        name = str(emoji.get("name") or "").strip()
    if not name:
        name = str(reaction.get("name") or "").strip()
    if not name:
        return "?"
    if name.isascii():  # кастомная реакция: хостинг discord-имени
        return f":{name.lower()}:"
    return name


def _lookup_emotion(token: str) -> str | None:
    """Кластер по токену (эмодзи или имя): точный поиск + варианты FE0F/skin/ZWJ."""
    def _find(s: str) -> str | None:
        s = s.strip().lower()
        if not s:
            return None
        for cand in (s, s.replace("\ufe0f", ""), s + "\ufe0f"):
            mapped = _EMOTION_BY_TOKEN.get(cand)
            if mapped:
                return mapped
        return None

    mapped = _find(token)
    if mapped:
        return mapped
    # последовательность из нескольких эмодзи (😂😂) или ZWJ-набор: первый угаданный
    for ch in EMOJI_RE.findall(token or ""):
        mapped = _find(ch)
        if mapped:
            return mapped
        for cp in ch:  # skin tone вариант 👍🏽 -> базовый 👍
            mapped = _find(cp)
            if mapped:
                return mapped
    return None


def classify_reaction(reaction: dict) -> str | None:
    """Эмоциональный кластер реакции; None — нейтральная/неизвестная."""
    disp = reaction_display(reaction)
    if disp == "?":
        return None
    return _lookup_emotion(disp.strip(":"))


def analyze_reactions(msgs: list[dict]) -> dict:
    """Агрегат по реакциям, которые ПОСТАВИЛИ пользователю (социальное влияние)."""
    total = 0
    by_emotion: Counter = Counter()
    top: Counter = Counter()
    neutral = 0
    reacted_msgs = 0
    for m in msgs:
        rl = load_json(m.get("reactions_json"))
        if not isinstance(rl, list) or not rl:
            continue
        msg_total = 0
        for r in rl:
            if not isinstance(r, dict):
                continue
            cnt = int(r.get("count") or 0)
            if cnt <= 0:
                continue
            msg_total += cnt
            disp = reaction_display(r)
            top[disp] += cnt
            emo = classify_reaction(r)
            if emo:
                by_emotion[emo] += cnt
            else:
                neutral += cnt
        if msg_total:
            reacted_msgs += 1
            total += msg_total
    out = {
        "total": total,
        "messages_with_reactions": reacted_msgs,
        "avg_per_message": round(total / len(msgs), 2) if msgs else 0.0,
        "by_emotion": dict(by_emotion.most_common()),
        "neutral_or_unclear": neutral,
        "top": [[k, v] for k, v in top.most_common(10)],
    }
    return out


def analyze_own_emojis(msgs: list[dict]) -> dict:
    """Эмодзи В ТЕКСТЕ пользователя — его собственная эмоциональная подача."""
    used = 0
    by_emotion: Counter = Counter()
    top: Counter = Counter()
    for m in msgs:
        text = (m["clean_content"] or m["content"] or "")
        found = EMOJI_RE.findall(text)
        if not found:
            continue
        used += 1
        for e in found:
            top[e] += 1
            emo = _lookup_emotion(e)
            if emo:
                by_emotion[emo] += 1
    return {
        "messages_with_emoji": used,
        "share": round(used / len(msgs), 3) if msgs else 0.0,
        "by_emotion": dict(by_emotion.most_common()),
        "top": [[k, v] for k, v in top.most_common(15)],
    }


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
        "reactions_emotions": {},
        "own_emoji_emotions": {},
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
    rx_agg = analyze_reactions(msgs)      # эмоции реакций, поставленных целевому
    own_agg = analyze_own_emojis(msgs)    # эмоции эмодзи в тексте целевого
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
        # эмоциональный разбор: реакции, поставленные целевому (социальное
        # влияние/какое впечатление производит), и эмодзи в его собственном тексте
        "reactions_emotions": rx_agg["by_emotion"],
        "reactions_top": [[k, v] for k, v in rx_agg["top"]],
        "reactions_messages_share": round(rx_agg["messages_with_reactions"] / n, 3),
        "own_emoji_emotions": own_agg["by_emotion"],
        "hesitation_word_hits": hesitation,
        "absolute_word_hits": absolute,
        "apology_hits": apologies,
        "gratitude_hits": gratitude,
        "self_mentions_of_others": sum(mentioned_counter.values()),
        "most_mentioned_users": mentioned_counter.most_common(10),
    })
    return stats
