"""Repository-слой (Model): персист данных через SQLAlchemy поверх SQLite.

Вся работа с БД сосредоточена здесь — контроллеры и сервисы не пишут
SQL/ORM-запросы напрямую.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models.entities import (
    Attachment,
    ChannelState,
    GuildMemberProfile,
    Message,
    UserRoleTag,
    UserProfile,
)

log = logging.getLogger("repository")

HASHTAG_RE = re.compile(r"(?<!\w)(?:#[a-zA-Z0-9_]{2,})|(?<!\w)<#\d+>")


class Repository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._sf = session_factory

    # ------------------------------------------------------------------ users
    def upsert_user_profile(
        self,
        user_id: int,
        fields: dict[str, Any],
        guild_fields: dict[str, Any] | None = None,
        roles: Iterable[dict[str, Any]] | None = None,
    ) -> None:
        """Создать/обновить глобальный профиль + серверный профиль + теги-роли."""
        with self._sf() as s:
            user = s.scalars(select(UserProfile).where(UserProfile.user_id == user_id)).first()
            if user is None:
                user = UserProfile(user_id=user_id)
                s.add(user)
            for k, v in fields.items():
                if hasattr(user, k) and v is not None:
                    setattr(user, k, v)

            if guild_fields:
                gid = guild_fields["guild_id"]
                gp = s.scalars(
                    select(GuildMemberProfile).where(
                        GuildMemberProfile.user_id == user_id,
                        GuildMemberProfile.guild_id == gid,
                    )
                ).first()
                if gp is None:
                    gp = GuildMemberProfile(user_id=user_id, guild_id=gid)
                    s.add(gp)
                for k, v in guild_fields.items():
                    if k == "guild_id":
                        continue
                    if hasattr(gp, k) and v is not None:
                        setattr(gp, k, v)

            if roles:
                existing = {
                    r.role_id
                    for r in s.scalars(select(UserRoleTag).where(UserRoleTag.user_id == user_id))
                }
                for r in roles:
                    if r["role_id"] in existing:
                        continue
                    s.add(UserRoleTag(user_id=user_id, **r))
                    existing.add(r["role_id"])

            s.commit()

    def get_user(self, user_id: int) -> UserProfile | None:
        with self._sf() as s:
            return s.scalars(select(UserProfile).where(UserProfile.user_id == user_id)).first()

    # -------------------------------------------------------------- messages
    def message_exists(self, message_id: int) -> bool:
        with self._sf() as s:
            return s.get(Message, message_id) is not None

    def insert_message(self, data: dict[str, Any], attachments: list[dict[str, Any]]) -> bool:
        """Сохранить сообщение и его вложения. False — если уже есть."""
        with self._sf() as s:
            if s.get(Message, data["id"]) is not None:
                return False
            content = data.get("clean_content") or data.get("content") or ""
            data.setdefault("word_count", len(content.split()))
            data.setdefault("char_count", len(content))
            data.setdefault("hashtag_count", len(HASHTAG_RE.findall(content)))
            msg = Message(**data)
            s.add(msg)
            for a in attachments:
                s.add(Attachment(**a))
            s.commit()
            return True

    def update_message_edited(self, message_id: int, edited_at: datetime, content: str) -> None:
        with self._sf() as s:
            msg = s.get(Message, message_id)
            if msg is not None:
                msg.edited_at = edited_at
                msg.content = content
                msg.clean_content = content
                msg.word_count = len(content.split())
                msg.char_count = len(content)
                s.commit()

    # ------------------------------------------------------------ attachments
    def mark_attachment_downloaded(
        self, attachment_id: int, path: str, sha256: str
    ) -> None:
        with self._sf() as s:
            att = s.get(Attachment, attachment_id)
            if att is not None:
                att.downloaded = True
                att.stored_path = path
                att.sha256 = sha256
                s.commit()

    def pending_image_attachments(self, limit: int = 50) -> list[Attachment]:
        """Не скачанные картинки для фоновой загрузки."""
        with self._sf() as s:
            return list(
                s.scalars(
                    select(Attachment)
                    .where(Attachment.is_image == True, Attachment.downloaded == False)  # noqa: E712
                    .limit(limit)
                )
            )

    # ------------------------------------------------------------------ state
    def get_state(self, channel_id: int) -> ChannelState:
        with self._sf() as s:
            st = s.get(ChannelState, channel_id)
            if st is None:
                st = ChannelState(channel_id=channel_id)
                s.add(st)
                s.commit()
            return st

    def update_state(
        self, channel_id: int, last_message_id: int | None, new_count: int, guild_id: int | None = None
    ) -> None:
        with self._sf() as s:
            st = s.get(ChannelState, channel_id)
            if st is None:
                st = ChannelState(channel_id=channel_id)
                s.add(st)
            st.last_poll_at = datetime.now(timezone.utc)
            if last_message_id is not None:
                if st.last_message_id is None or last_message_id > st.last_message_id:
                    st.last_message_id = last_message_id
            # default= в ORM применяется только на INSERT; при fresh-объекте
            # внутри этой же сессии поле ещё может быть None — страхуемся.
            st.messages_total = (st.messages_total or 0) + new_count
            if guild_id is not None:
                st.guild_id = guild_id
            s.commit()

    def mark_oldest(self, channel_id: int, oldest_message_id: int | None) -> None:
        """Запоминает самый старый загруженный id (для резюма backfill)."""
        if oldest_message_id is None:
            return
        with self._sf() as s:
            st = s.get(ChannelState, channel_id)
            if st is None:
                st = ChannelState(channel_id=channel_id)
                s.add(st)
            if st.oldest_message_id is None or oldest_message_id < st.oldest_message_id:
                st.oldest_message_id = oldest_message_id
            s.commit()

    # ------------------------------------------------------------- analytics
    def stats(self) -> dict[str, int]:
        from sqlalchemy import func

        with self._sf() as s:
            return {
                "users": s.scalar(select(func.count()).select_from(UserProfile)) or 0,
                "messages": s.scalar(select(func.count()).select_from(Message)) or 0,
                "attachments": s.scalar(select(func.count()).select_from(Attachment)) or 0,
                "images_downloaded": s.scalar(
                    select(func.count())
                    .select_from(Attachment)
                    .where(Attachment.downloaded == True)  # noqa: E712
                )
                or 0,
                "roles": s.scalar(select(func.count()).select_from(UserRoleTag)) or 0,
            }
