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

from app.utils.helpers import to_json

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
        "system_user": bool(getattr(user, "system", False)),
        "avatar_url": _safe(lambda: str(user.display_avatar.url)),
        "banner_url": _safe(lambda: str(user.banner.url)) if getattr(user, "banner", None) else None,
        "accent_color": _safe(lambda: int(user.accent_color.value) if user.accent_color else None),
        "created_at_discord": _safe(lambda: user.created_at),
        "public_flags": _safe(lambda: int(user.public_flags.value)),
        "flags_raw": to_json(_safe(lambda: flags_to_list(user.public_flags), [])),
    }
    # UserProfile (fetch_user_profile) несёт bio/connections/badges/mutual
    fields["about"] = _safe(lambda: user.bio) or _safe(lambda: user.display_bio)
    badges = _safe(lambda: list(user.badges)) or []
    if badges:
        fields["badges_json"] = to_json(
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
        fields["connections_json"] = to_json(
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
        fields["raw_json"] = to_json(raw)
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
    roles_payload = [
        {
            "role_id": r.id,
            "guild_id": guild.id,
            "role_name": getattr(r, "name", "@everyone" if r.id == guild.id else ""),
            "position": getattr(r, "position", 0),
            "color": _safe(lambda: int(r.colour.value)) if getattr(r, "colour", None) else None,
            "is_mentionable": getattr(r, "mentionable", None),
            "permissions_raw": _safe(lambda: str(int(r.permissions.value))),
        }
        for r in roles
        if hasattr(r, "id")  # discord.Role в рантайме; duck-typed объекты в тестах
    ]
    return {k: v for k, v in gf.items() if v is not None}, roles_payload


# ---------------------------------------------------------------------------
# Сообщения
# ---------------------------------------------------------------------------
def message_data(msg: discord.Message, channel_guild_id: int | None) -> dict[str, Any]:
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
        "reactions_json": to_json(
            [{"emoji": str(r.emoji), "count": r.count, "me": r.me} for r in (msg.reactions or [])]
        )
        if msg.reactions
        else None,
        "mention_users_json": to_json([u.id for u in (msg.mentions or [])]) if msg.mentions else None,
        "mention_roles_json": to_json([r.id for r in (msg.role_mentions or [])]) if msg.role_mentions else None,
        "mention_channels_json": to_json(
            sorted({int(m) for m in CHANNEL_MENTION_RE.findall(msg.content or "")})
        )
        or None,
        "stickers_json": to_json(
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
    # сырой дамп — на случай полей, которые мы ещё не маппим
    raw = _safe(lambda: msg.to_dict())
    if raw:
        data["raw_json"] = to_json(raw)
    return data


def attachments_data(msg: discord.Message) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for a in msg.attachments or []:
        ct = (a.content_type or "").lower()
        ext = ("." + a.filename.rsplit(".", 1)[-1].lower()) if "." in (a.filename or "") else ""
        is_image = ct.startswith("image/") or ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
        out.append(
            {
                "id": a.id,
                "message_id": msg.id,
                "filename": a.filename or "",
                "url": a.url or "",
                "proxy_url": a.proxy_url,
                "content_type": a.content_type,
                "size": a.size or 0,
                "width": getattr(a, "width", None),
                "height": getattr(a, "height", None),
                "is_image": is_image,
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
