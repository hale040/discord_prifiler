"""Модели SQLAlchemy: вся собираемая информация для последующего профайлинга."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Журнал действий админки (кто / что / когда / результат)
# ---------------------------------------------------------------------------
class AuditLog(Base):
    """Пишется admin_app.py при каждом изменяющем действии и входе."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    actor: Mapped[str] = mapped_column(String(128), default="admin")
    action: Mapped[str] = mapped_column(String(64), default="")  # login/sql_update/...
    target: Mapped[str] = mapped_column(String(256), default="")  # таблица/id/файл
    details: Mapped[str | None] = mapped_column(Text, nullable=True)  # SQL/параметры
    result: Mapped[str] = mapped_column(String(512), default="ok")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Audit {self.id} {self.action} by {self.actor}>"


# ---------------------------------------------------------------------------
# Профили пользователей (аккаунт Discord + профиль на сервере)
# ---------------------------------------------------------------------------
class UserProfile(Base):
    """Глобальный профиль пользователя Discord. Хранит ВСЕ доступные данные."""

    __tablename__ = "user_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)

    username: Mapped[str] = mapped_column(String(256), default="")
    discriminator: Mapped[str] = mapped_column(String(32), default="0")
    global_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    mention: Mapped[str | None] = mapped_column(String(256), nullable=True)

    bot: Mapped[bool] = mapped_column(Boolean, default=False)
    system_user: Mapped[bool] = mapped_column(Boolean, default=False)

    avatar_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    banner_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    accent_color: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at_discord: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    mutual: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # «О себе» / bio
    about: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Публичные флаги аккаунта (DISCORD_EMPLOYEE, NITRO и т.п.)
    public_flags: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    flags_raw: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON-список строк

    # Бейджи (JSON: name/description)
    badges_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Социальные сети из профиля (Discord profile connections)
    connections_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Premium/подписка
    premium_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    premium_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Локаль/часовой пояс, если доступны
    locale: Mapped[str | None] = mapped_column(String(64), nullable=True)
    timezone_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Полный сырой дамп объекта (на случай полей, которые ORM не раскрыла)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Приоритетный пользователь: вручную ставится в админке; экспорт/фильтры
    # умеют выбирать только таких (см. export_profiles.py --priority-only).
    priority: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    priority_note: Mapped[str | None] = mapped_column(String(512), nullable=True)

    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    guild_profiles: Mapped[list[GuildMemberProfile]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    roles: Mapped[list[UserRoleTag]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# Серверный профиль участника (nickname, роли, join date, баны/кики...)
# ---------------------------------------------------------------------------
class GuildMemberProfile(Base):
    __tablename__ = "guild_member_profiles"
    __table_args__ = (UniqueConstraint("user_id", "guild_id", name="uq_user_guild"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_profiles.user_id"), index=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)

    nick: Mapped[str | None] = mapped_column(String(256), nullable=True)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    boosting_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pending: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    mobile: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    deaf: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    mute: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    timeout_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    permissions_raw: Mapped[str | None] = mapped_column(String(128), nullable=True)
    color: Mapped[int | None] = mapped_column(Integer, nullable=True)
    top_role: Mapped[str | None] = mapped_column(String(256), nullable=True)

    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[UserProfile] = relationship(back_populates="guild_profiles")


# ---------------------------------------------------------------------------
# Теги/роли пользователя на серверах
# ---------------------------------------------------------------------------
class UserRoleTag(Base):
    """Роли (теги) пользователя. Отдельная таблица — удобно для профайлинга."""

    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_role"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_profiles.user_id"), index=True)
    role_id: Mapped[int] = mapped_column(BigInteger, index=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    role_name: Mapped[str] = mapped_column(String(256), default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    color: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_mentionable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    permissions_raw: Mapped[str | None] = mapped_column(String(128), nullable=True)

    user: Mapped[UserProfile] = relationship(back_populates="roles")


# ---------------------------------------------------------------------------
# Сообщения
# ---------------------------------------------------------------------------
class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # snowflake Discord
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    author_id: Mapped[int] = mapped_column(BigInteger, index=True)

    content: Mapped[str] = mapped_column(Text, default="")
    clean_content: Mapped[str] = mapped_column(Text, default="")
    tts: Mapped[bool] = mapped_column(Boolean, default=False)
    type: Mapped[int] = mapped_column(Integer, default=0)
    flags: Mapped[int] = mapped_column(Integer, default=0)
    reference_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)  # reply-to
    # Автор сообщения, на которое дан ответ (reply_to_author_id). Заполняется
    # из БД-графа ответов (Repository.link_reply_references) — даёт связи
    # «кто кому отвечает» даже когда само родительское сообщение не сохранено.
    reply_to_author_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )

    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    webhook_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Статистика текста — пригодится для психотипирования
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    has_attachment: Mapped[bool] = mapped_column(Boolean, default=False)
    embed_count: Mapped[int] = mapped_column(Integer, default=0)
    mention_count: Mapped[int] = mapped_column(Integer, default=0)
    hashtag_count: Mapped[int] = mapped_column(Integer, default=0)
    reaction_total: Mapped[int] = mapped_column(Integer, default=0)
    reactions_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Упоминания ролей/каналов/пользователей (JSON-массивы id)
    mention_users_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    mention_roles_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    mention_channels_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    stickers_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    attachments: Mapped[list[Attachment]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# Вложения (картинки и пр.)
# ---------------------------------------------------------------------------
class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), index=True)

    filename: Mapped[str] = mapped_column(String(512), default="")
    url: Mapped[str] = mapped_column(String(2048), default="")
    proxy_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(256), nullable=True)
    size: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_image: Mapped[bool] = mapped_column(Boolean, default=False)
    is_spoiler: Mapped[bool] = mapped_column(Boolean, default=False)
    stored_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    downloaded: Mapped[bool] = mapped_column(Boolean, default=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    message: Mapped[Message] = relationship(back_populates="attachments")


# ---------------------------------------------------------------------------
# Состояние опроса канала (курсор last fetched message)
# ---------------------------------------------------------------------------
class ChannelState(Base):
    __tablename__ = "channel_states"

    channel_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Самый старый загруженный id (для резюма backfill без ограничения по дате)
    oldest_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    messages_total: Mapped[int] = mapped_column(Integer, default=0)
