"""Маппинг объектов discord.py-self -> словари для Repository (Model-слой).

Собираем ВСЁ доступное: аккаунт, профиль (bio/pronouns/badges/connections),
серверные данные участника (nick/joined/roles/timeouts), каждое сообщение со
статистикой текста и вложениями.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import discord

import json

from app.utils.helpers import DiscordJSONEncoder, to_json


def _to_json_or_none(value: Any):
    """to_json + «пустые» ([] / {} / '[]') трактуем как NULL (экономия места)."""
    v = to_json(value)
    if isinstance(v, str):  # fallback после сбоя сериализации — не пишем в JSONB
        return None
    if v in ([], {}, ""):
        return None
    return v

log = logging.getLogger("mappers")

USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")


def _safe(fn, default=None):
    try:
        v = fn()
        return v if v is not None else default
    except Exception:  # noqa: BLE001
        return default


def to_int(value, default: int = 0) -> int:
    """Безопасное приведение к int.

    discord.py-self (2.x) возвращает для enum-полей объекты вида
    `_EnumValue_MessageType` / `_EnumValue_PremiumType`, у которых НЕТ
    `__int__` — прямой `int(msg.type)` падает с TypeError. Число лежит в
    атрибуте `.value`. Также поддерживаем строковые числа и None.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    inner = getattr(value, "value", None)
    if inner is not None and inner is not value:
        try:
            return int(inner)
        except (TypeError, ValueError):
            pass
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def flags_to_list(flags) -> list[str]:
    """Только ВЗВЕДЁННЫЕ имена флагов.

    discord.py-self: итерация по объекту Flags отдаёт пары (name, bool)
    для всех известных флагов — без фильтра `if val` список содержит
    вообще все возможные флаги, что ломает профайлинг.
    Поддержка и dict-флагов ({"staff": true}), и enum-списков.
    """
    if flags is None:
        return []
    try:
        if isinstance(flags, dict):
            return [k for k, v in flags.items() if v]
        items = list(flags)
        if items and isinstance(items[0], tuple):
            return [name for name, val in items if val]
        return [str(f) for f in items]  # iterable of enabled enum values
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Профиль пользователя (глобальный) + серверный + роли
# ---------------------------------------------------------------------------
def user_fields(user: discord.abc.User) -> dict[str, Any]:
    """Базовые поля из объекта User/Member/UserProfile."""
    fields: dict[str, Any] = {
        "username": getattr(user, "username", None) or getattr(user, "name", "") or "",
        "discriminator": str(getattr(user, "discriminator", "0") or "0"),
        "global_name": _safe(lambda: user.global_name),
        "mention": _safe(lambda: user.mention),
        "bot": bool(getattr(user, "bot", False)),
        "is_system": bool(getattr(user, "system", False)),  # колонка переименована: system_user — синтаксическая ошибка в PG (см. entities.py)
        "avatar_url": _safe(lambda: str(user.display_avatar.url)),
        "banner_url": _safe(lambda: str(user.banner.url)) if getattr(user, "banner", None) else None,
        "accent_color": _safe(lambda: int(user.accent_color.value) if user.accent_color else None),
        "created_at_discord": _safe(lambda: user.created_at),
        "public_flags": _safe(lambda: int(user.public_flags.value)),
        "flags_raw": _to_json_or_none(_safe(lambda: flags_to_list(user.public_flags), [])),
    }
    # UserProfile (fetch_user_profile) несёт bio/connections/badges/mutual
    fields["about"] = _safe(lambda: user.bio) or _safe(lambda: user.display_bio)
    badges = _safe(lambda: list(user.badges)) or []
    if badges:
        fields["badges_json"] = _to_json_or_none(
            [
                {
                    "key": getattr(b, "key", None),
                    "name": getattr(b, "name", None),
                    "description": getattr(b, "description", None),
                }
                for b in badges
            ]
        )
    conns = _safe(lambda: list(user.connections)) or []
    if conns:
        fields["connections_json"] = _to_json_or_none(
            [
                {
                    "platform": getattr(c, "platform", None),
                    "name": getattr(c, "name", None),
                    "type": str(getattr(c, "type", "")),
                    "verified": getattr(c, "verified", None),
                    "friend_sync": getattr(c, "friend_sync", None),
                    "revoked": getattr(c, "revoked", None),
                    "metadata_visibility": _safe(lambda: c.metadata_visibility),
                }
                for c in conns
            ]
        )
    fields["premium_type"] = _safe(lambda: to_int(user.premium_type, -1))
    fields["premium_since"] = _safe(lambda: user.premium_since)
    mg = _safe(lambda: list(user.mutual_guilds))
    mf = _safe(lambda: list(user.mutual_friends))
    mfc = _safe(lambda: user.mutual_friends_count)
    raw: dict[str, Any] = {}
    if mg:
        # discord.py-self 2.1.0: MutualGuild имеет только .id и .nick —
        # имени гильдии у объекта нет (AttributeError при прямом обращении).
        raw["mutual_guilds"] = [
            {"id": _safe(lambda g=g: int(g.id)), "nick": _safe(lambda g=g: g.nick)}
            for g in mg
        ]
    if mf:
        raw["mutual_friends"] = [f.id for f in mf]
    if mfc is not None:
        raw["mutual_friends_count"] = mfc
    if raw:
        fields["raw_json"] = _to_json_or_none(raw)
    return {k: v for k, v in fields.items() if v is not None}


def guild_and_roles_from_member(member: discord.Member) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Серверный профиль + теги-роли из объекта Member/MemberProfile."""
    guild = getattr(member, "guild", None)
    if guild is None:
        return {}, []
    gf: dict[str, Any] = {
        "guild_id": guild.id,
        "nick": _safe(lambda: member.nick),
        "joined_at": _safe(lambda: member.joined_at),
        "boosting_since": _safe(lambda: getattr(member, "premium_since", None)),
        "pending": _safe(lambda: member.pending),
        "mobile": _safe(lambda: getattr(member, "mobile", None) or member.is_on_mobile()),
        "deaf": _safe(lambda: member.deaf),
        "mute": _safe(lambda: member.mute),
        "timeout_until": _safe(lambda: member.timed_out_until),
        "avatar_url": _safe(lambda: str(member.guild_avatar.url)) if getattr(member, "guild_avatar", None) else None,
        "color": _safe(lambda: int(member.colour.value) if getattr(member, "colour", None) else None),
        "top_role": _safe(lambda: member.top_role.name),
        "permissions_raw": _safe(lambda: str(int(member.guild_permissions.value))),
    }
    roles = _safe(lambda: list(member.roles)) or []
    # @everyone (id == guild.id) — не настоящая роль: она есть у всех и всегда,
    # в профилировании только раздувает user_roles. Не сохраняем.
    roles_payload = [
        {
            "role_id": r.id,
            "guild_id": guild.id,
            "position": getattr(r, "position", 0),
            "color": _safe(lambda: int(r.colour.value)) if getattr(r, "colour", None) else None,
            "is_mentionable": getattr(r, "mentionable", None),
            "permissions_raw": _safe(lambda: str(int(r.permissions.value))),
        }
        for r in roles
        if hasattr(r, "id") and r.id != guild.id  # discord.Role в рантайме; duck-typed объекты в тестах; @everyone пропускаем
    ]
    return {k: v for k, v in gf.items() if v is not None}, roles_payload


def role_def_from_role(r, guild_id: int) -> dict:
    """Запись справочника roles из discord.Role (или duck-typed в тестах)."""
    return {
        "guild_id": guild_id,
        "role_id": r.id,
        "name": getattr(r, "name", ""),
        "position": getattr(r, "position", 0),
        "color": _safe(lambda: int(r.colour.value)) if getattr(r, "colour", None) else None,
        "is_mentionable": getattr(r, "mentionable", None),
        "permissions_raw": _safe(lambda: str(int(r.permissions.value))),
    }


# ---------------------------------------------------------------------------
# Сообщения
# ---------------------------------------------------------------------------
def message_data(
    msg: discord.Message, channel_guild_id: int | None, *, store_raw: bool = True
) -> dict[str, Any]:
    author = msg.author
    clean = msg.clean_content if hasattr(msg, "clean_content") else msg.content
    data: dict[str, Any] = {
        "id": msg.id,
        "channel_id": msg.channel.id,
        "guild_id": getattr(getattr(msg, "guild", None), "id", None) or channel_guild_id,
        "author_id": author.id,
        "content": msg.content or "",
        "clean_content": clean or "",
        "tts": bool(getattr(msg, "tts", False)),
        "type": to_int(getattr(msg, "type", 0)),
        "flags": to_int(getattr(msg.flags, "value", 0)) if getattr(msg, "flags", None) else 0,
        "timestamp": msg.created_at,
        "edited_at": msg.edited_at,
        "pinned": bool(getattr(msg, "pinned", False)),
        "webhook_id": getattr(msg, "webhook_id", None),
        # метрики для будущего психотипирования
        "word_count": len((clean or "").split()),
        "char_count": len(clean or ""),
        "has_attachment": bool(msg.attachments),
        "embed_count": len(msg.embeds or []),
        "mention_count": len(msg.mentions or []) + len(msg.role_mentions or []),
        "reaction_total": sum(r.count for r in (msg.reactions or [])),
        "reactions_json": _to_json_or_none(
            [{"emoji": str(r.emoji), "count": r.count, "me": r.me} for r in (msg.reactions or [])]
        )
        if msg.reactions
        else None,
        "mention_users_json": _to_json_or_none([u.id for u in (msg.mentions or [])]) if msg.mentions else None,
        "mention_roles_json": _to_json_or_none([r.id for r in (msg.role_mentions or [])]) if msg.role_mentions else None,
        "mention_channels_json": _to_json_or_none(
            sorted({int(m) for m in CHANNEL_MENTION_RE.findall(msg.content or "")})
        )
        or None,
        "stickers_json": _to_json_or_none(
            [
                {
                    "id": s.id,
                    "name": s.name,
                    "tags": getattr(s, "tags", None),
                    "format": str(getattr(s, "format", "")),
                    "type": str(getattr(s, "type", "")),
                }
                for s in (msg.stickers or [])
            ]
        )
        if msg.stickers
        else None,
    }
    ref = getattr(msg, "reference", None)
    if ref is not None:
        data["reference_id"] = _safe(lambda: int(ref.message_id))
        # если либа отдала resolved-родителя — автора ответа знаем сразу,
        # без ожидания достройки графа из БД
        rid = _safe(lambda: int(ref.resolved.author.id))
        if rid is not None and rid != msg.author.id:
            data["reply_to_author_id"] = rid
    # сырой дамп — на случай полей, которые мы ещё не маппим.
    # По умолчанию НЕ сохраняется (STORE_RAW_JSON=0): полный to_dict() каждого
    # сообщения раздувал SQLite в разы; все нужные поля уже смаплены выше.
    if store_raw:
        raw = _safe(lambda: msg.to_dict())
        if raw:
            data["raw_json"] = _to_json_or_none(raw)
    return data


_DOC_EXTS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".txt", ".rtf", ".csv")
_ARCHIVE_EXTS = (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2")
_AUDIO_EXTS = (".mp3", ".ogg", ".wav", ".flac", ".m4a", ".opus")
_VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv", ".avi", ".gifv")
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def classify_attachment(filename: str | None, content_type: str | None,
                        width: int | None, height: int | None) -> str:
    """Эвристическая классификация вложения по имени/MIME/размерам.

    kind: image|meme|screenshot|document|archive|audio|video|other.
    Мемы и скриншоты — подвиды картинок (по имени файла); для нейросети
    это сигнал «юмор-контент» vs «доказательный артефакт».
    """
    name = (filename or "").lower()
    ct = (content_type or "").lower()
    ext = ("." + name.rsplit(".", 1)[-1]) if "." in name else ""
    stem = name.rsplit(".", 1)[0]
    if any(k in stem for k in ("spoiler-", "image", "photo")) and ext in _IMAGE_EXTS:
        # типичные автоимена Discord-картинок — просто картинка
        pass
    if ct.startswith("image/") or ext in _IMAGE_EXTS:
        if any(k in stem for k in ("meme", "mem_", "_mem", "jaja", "lol", "lmao", "прикол", "мем", "смех")):
            return "meme"
        if any(k in stem for k in ("screenshot", "screen", "scrnshot", "scr", "snip", "скрин", "скриншот")) \
                or name.startswith(("obs_", "sharex", "greenshot", "snagit", "screen-")):
            return "screenshot"
        return "image"
    if ct.startswith("video/") or ext in _VIDEO_EXTS:
        return "video"
    if ct.startswith("audio/") or ext in _AUDIO_EXTS:
        return "audio"
    if ct.startswith("text/") or ext in _DOC_EXTS or ct in (
        "application/pdf", "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ):
        return "document"
    if ext in _ARCHIVE_EXTS or ct in ("application/zip", "application/x-rar-compressed",
                                      "application/x-7z-compressed", "application/gzip"):
        return "archive"
    return "other"


def attachments_data(msg: discord.Message) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for a in msg.attachments or []:
        ct = (a.content_type or "").lower()
        ext = ("." + a.filename.rsplit(".", 1)[-1].lower()) if "." in (a.filename or "") else ""
        is_image = ct.startswith("image/") or ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
        w = getattr(a, "width", None)
        h = getattr(a, "height", None)
        out.append(
            {
                "id": a.id,
                "message_id": msg.id,
                "filename": a.filename or "",
                "url": a.url or "",
                "proxy_url": a.proxy_url,
                "content_type": a.content_type,
                "size": a.size or 0,
                "width": w,
                "height": h,
                "is_image": is_image,
                "kind": classify_attachment(a.filename, a.content_type, w, h),
                "is_spoiler": bool(getattr(a, "is_spoiler", lambda: False)())
                if callable(getattr(a, "is_spoiler", None))
                else bool(getattr(a, "is_spoiler", False)),
            }
        )
    return out


def extract_user_ids(msg: discord.Message) -> set[int]:
    """Все id пользователей, упомянутые в сообщении (в т.ч. в тексте)."""
    ids: set[int] = {msg.author.id}
    for u in msg.mentions or []:
        ids.add(u.id)
    ids.update(int(m) for m in USER_MENTION_RE.findall(msg.content or ""))
    ref = getattr(msg, "reference", None)
    rid = _safe(lambda: int(ref.resolved.author.id)) if ref is not None else None
    if rid:
        ids.add(rid)
    return ids


# ---------------------------------------------------------------------------
# Игры / активности пользователя (таблица user_games)
# ---------------------------------------------------------------------------
_ACTIVITY_KINDS = {
    0: "playing", 1: "streaming", 2: "listening", 3: "watching", 4: "custom",
    5: "dnd", 6: "competing",
}


def activity_kind_str(a: Any) -> str | None:
    """Нормализует тип активности в человекочитаемую строку.

    discord.py-self отдаёт enum ActivityType, у которого str() возвращает
    'ActivityType.custom_status', а int() — числовой код protocol'а.
    Неизвестные/не распознанные типы (в т.ч. type=6 competing без enum,
    отсутствующий type) возвращаем как 'playing' — раньше отсюда уходил
    None, и purge удалял такие строки вместе с реальными играми.
    """
    t = getattr(a, "type", None)
    num = _safe(lambda: int(t))
    if num is not None and num in _ACTIVITY_KINDS:
        return _ACTIVITY_KINDS[num]
    raw = str(_safe(lambda: str(t)) or "").lower()
    if "custom" in raw:
        return "custom"
    cleaned = raw.replace("activitytype.", "")[:32]
    if cleaned and cleaned != "none":
        return cleaned
    name_hint = str(_safe(lambda: str(getattr(a, "name", "")) or "").lower())
    for hint in ("streaming", "listening", "watching", "competing"):
        if hint in name_hint:
            return hint
    return "playing"


def games_from_activities(obj: Any) -> list[dict[str, Any]]:
    """Извлекает список игр/активностей из User/Member/UserProfile объекта.

    discord.py-self: activities живёт только на Member (presence) и иногда на
    UserProfile; для обычного User может отсутствовать — тогда пустой список.
    Steam-логин берётся из connections (platform == steam) — сам профиль Steam
    НЕ тянется, сохраняется только логин (он уже есть в connections_json).

    Кастомный статус (ActivityType.custom_status) — это НЕ игра: он попадает
    в profile_snapshots.status_text и сюда не пишется.
    """
    out: list[dict[str, Any]] = []
    acts = _safe(lambda: list(getattr(obj, "activities", None) or [])) or []
    for a in acts:
        kind = activity_kind_str(a)
        if kind == "custom":
            continue  # кастомный статус — не игра, храним его в снапшоте профиля
        name = _safe(lambda a=a: str(a.name)) or ""
        if not name:
            continue
        url = _safe(lambda a=a: str(a.url)) if getattr(a, "url", None) else None
        details = _safe(lambda a=a: str(a.details)) if getattr(a, "details", None) else None
        state_txt = _safe(lambda a=a: str(a.state)) if getattr(a, "state", None) else None
        app_id = _safe(lambda a=a: int(a.application_id)) if getattr(a, "application_id", None) else None
        out.append(
            {
                "name": name[:256],
                "kind": kind,
                "details": details,
                "state": state_txt,
                "url": url,
                "application_id": app_id,
            }
        )
    return out


def steam_login_from(obj: Any) -> str | None:
    """Логин Steam из connections профиля (без обращения к самому Steam)."""
    conns = _safe(lambda: list(getattr(obj, "connections", None) or [])) or []
    for c in conns:
        platform = str(_safe(lambda c=c: c.platform) or "").lower()
        if platform in ("steam",):
            name = _safe(lambda c=c: str(c.name))
            if name:
                return name
    return None

