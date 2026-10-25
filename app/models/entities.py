"""Модели SQLAlchemy: вся собираемая информация для последующего профайлинга."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import BIGINT, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Discord snowflake'и — 64-битные id. В PostgreSQL обычный SERIAL/BIGSERIAL
# не может выдать такое значение (sequence ограничен int32/int64-счётчиком с
# START 1), поэтому для таблиц с внешними id (messages/attachments) PK
# объявляется как plain BIGINT без автоинкремента — id присваивает приложение.
SnowflakeId = BIGINT


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Журнал действий админки (кто / что / когда / результат)
# ---------------------------------------------------------------------------
class AuditLog(Base):
    """Пишется admin_app.py при каждом изменяющем действии и входе."""

    __tablename__ = "audit_log"

    # BIGINT PK + bigserial на PostgreSQL (см. _pg_bigserial_pk в migrations.py)
    id: Mapped[int] = mapped_column(SnowflakeId, primary_key=True, autoincrement=True)
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

    # BIGINT PK + bigserial на PostgreSQL (см. _pg_bigserial_pk в migrations.py)
    id: Mapped[int] = mapped_column(SnowflakeId, primary_key=True, autoincrement=True)
    # user_id ссылается на UserProfile.user_id (snowflake Discord), а не на
    # суррогатный id — поэтому UNIQUE на user_id обязателен для FK.
    user_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True
    )  # см. выше: unique + index для ForeignKey(user_profiles.user_id)

    username: Mapped[str] = mapped_column(String(256), default="")
    discriminator: Mapped[str] = mapped_column(String(32), default="0")
    global_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    mention: Mapped[str | None] = mapped_column(String(256), nullable=True)

    bot: Mapped[bool] = mapped_column(Boolean, default=False)
    # ВАЖНО: колонка называется is_system, а не system_user: в PostgreSQL
    # USER/SYSTEM — зарезервированные слова (зарезервированы для использования
    # в качестве функций), и CREATE TABLE ... system_user BOOLEAN падает с
    # "syntax error at or near". SQLAlchemy экранирует только полностью
    # зарезервированные имена (USER -> "user"), для *_user она этого не делает.
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)

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
    flags_raw: Mapped[str | None] = mapped_column(JSONB, nullable=True)  # JSON-список строк

    # Бейджи (JSON: name/description)
    badges_json: Mapped[str | None] = mapped_column(JSONB, nullable=True)

    # Социальные сети из профиля (Discord profile connections)
    connections_json: Mapped[str | None] = mapped_column(JSONB, nullable=True)

    # Premium/подписка
    premium_type: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- ЛС-сбор (миграция v11) -------------------------------------------
    # Флаг включён кнопкой «💬 Собрать ЛС» в карточке пользователя; цикл
    # dm-loop в CollectionService выгружает историю диалога только для тех,
    # у кого dm_fetch = True. Курсор last_dm_id — самый старый загруженный
    # id (резюм после рестарта без повторной скачки того же).
    dm_fetch: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    dm_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dm_last_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dm_first_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dm_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    dm_messages_total: Mapped[int] = mapped_column(Integer, default=0)
    # Диагностика (миграция v12): последняя ошибка ЛС-цикла и счётчик
    # подряд идущих сбоев. После DM_MAX_CONSECUTIVE_FAILS (по умолч. 5)
    # сбор для пользователя приостанавливается — чтобы «нет доступа к ЛС»
    # не повторялось каждые 5 минут вечно.
    dm_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    dm_fail_count: Mapped[int] = mapped_column(Integer, default=0)
    # Статус последнего прохода dm-loop (миграция v14): running — идёт
    # выгрузка прямо сейчас; ok — проход завершился успешно; error — сбой.
    # Админка показывает живой прогресс и включает кнопку «▶ Выполнить
    # сейчас» только когда статус не running (защита от параллельных выгрузок).
    dm_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Сигнал «выполнить сейчас» (миграция v15): админка и коллектор — разные
    # процессы, прямого вызова в event loop коллектора нет. Кнопка «▶ Выполнить
    # сейчас» ставит pending_dm=True; живой dm-loop при каждом тике выбирает
    # таких пользователей вне очереди (не дожидаясь DM_POLL_INTERVAL_SEC),
    # атомарно сбрасывает флаг (claim_pending_dm) и выполняет проход диалога.
    pending_dm: Mapped[bool] = mapped_column(Boolean, default=False)
    # Ручной DM-канал (миграция v13): id задан из админки, когда ЛС закрыты
    # и create_dm/fetch_channel недоступны, но канал уже открыт в клиенте.
    # dm-loop читает историю по этому id и НЕ перезаписывает его и не сбрасывает
    # курсоры при «канал недоступен».
    dm_channel_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    premium_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Локаль/часовой пояс, если доступны
    locale: Mapped[str | None] = mapped_column(String(64), nullable=True)
    timezone_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Полный сырой дамп объекта (на случай полей, которые ORM не раскрыла)
    raw_json: Mapped[str | None] = mapped_column(JSONB, nullable=True)

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
    games: Mapped[list["UserGame"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# Игры пользователя (активности из профиля Discord: Playing/Streaming и т.п.)
# Отдельная таблица со связью к user_profiles; обновляется при рефреше
# профилей (PROFILE_REFRESH_DAYS). Steam-логин хранится в connections_json
# у user_profiles (ссылку на профиль Steam не тянем — только логин).
# ---------------------------------------------------------------------------
class UserGame(Base):
    __tablename__ = "user_games"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_user_game"),)

    # BIGINT PK + bigserial на PostgreSQL (см. _pg_bigserial_pk в migrations.py)
    id: Mapped[int] = mapped_column(SnowflakeId, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user_profiles.user_id", ondelete="CASCADE"), index=True
    )

    name: Mapped[str] = mapped_column(String(256))  # название игры/активности
    kind: Mapped[str | None] = mapped_column(String(32), nullable=True)  # playing/streaming/listening/watching/custom
    details: Mapped[str | None] = mapped_column(String(512), nullable=True)
    state: Mapped[str | None] = mapped_column(String(512), nullable=True)
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)  # стрим-ссылка (не в промпт)
    application_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[UserProfile] = relationship(back_populates="games")


# ---------------------------------------------------------------------------
# Серверный профиль участника (nickname, роли, join date, баны/кики...)
# ---------------------------------------------------------------------------
class GuildMemberProfile(Base):
    __tablename__ = "guild_member_profiles"
    __table_args__ = (UniqueConstraint("user_id", "guild_id", name="uq_user_guild"),)

    # BIGINT PK + bigserial на PostgreSQL (см. _pg_bigserial_pk в migrations.py)
    id: Mapped[int] = mapped_column(SnowflakeId, primary_key=True, autoincrement=True)
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

    raw_json: Mapped[str | None] = mapped_column(JSONB, nullable=True)

    user: Mapped[UserProfile] = relationship(back_populates="guild_profiles")


# ---------------------------------------------------------------------------
# Теги/роли пользователя на серверах
# ---------------------------------------------------------------------------
class UserRoleTag(Base):
    """Роли (теги) пользователя. Отдельная таблица — удобно для профайлинга."""

    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_role"),)

    # BIGINT PK + bigserial на PostgreSQL (см. _pg_bigserial_pk в migrations.py)
    id: Mapped[int] = mapped_column(SnowflakeId, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_profiles.user_id"), index=True)
    role_id: Mapped[int] = mapped_column(BigInteger, index=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    # Название роли — НЕ здесь: оно живёт в справочнике roles (одно на роль),
    # иначе переименование роли в Discord пришлось бы чинить в каждой строке.
    position: Mapped[int] = mapped_column(Integer, default=0)
    color: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_mentionable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    permissions_raw: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Динамика ролей: первая/последняя встреча и момент, когда роль пропала
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_lost_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[UserProfile] = relationship(back_populates="roles")


class RoleDefinition(Base):
    """Справочник ролей сервера: одна строка на (guild_id, role_id).

    Единственный источник названия/цвета/позиции роли. user_roles — только
    связь пользователь×роль с динамикой. Обновляется тем же потоком, что и
    связи (см. repository.sync_role_defs), поэтому переименование роли в
    Discord подхватывается само.
    """

    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("guild_id", "role_id", name="uq_role_guild"),)

    id: Mapped[int] = mapped_column(SnowflakeId, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    role_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(256), default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    color: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_mentionable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    permissions_raw: Mapped[str | None] = mapped_column(String(128), nullable=True)
    first_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Сообщения
# ---------------------------------------------------------------------------
class Message(Base):
    __tablename__ = "messages"

    # Индексы под типовые запросы профилирования (PostgreSQL):
    # - транскрипт одного автора за период (ORDER BY timestamp);
    # - лента канала при поллинге/backfill;
    # - частичный индекс: удалённые сообщения в выборках не участвуют.
    __table_args__ = (
        Index("ix_messages_author_ts", "author_id", "timestamp"),
        Index("ix_messages_channel_ts", "channel_id", "timestamp"),
        Index(
            "ix_messages_live_ts",
            "timestamp",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    # PK = snowflake Discord (явный id, без sequence) — plain BIGINT на PG
    id: Mapped[int] = mapped_column(SnowflakeId, primary_key=True)
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
    reactions_json: Mapped[str | None] = mapped_column(JSONB, nullable=True)

    # Упоминания ролей/каналов/пользователей (JSON-массивы id)
    mention_users_json: Mapped[str | None] = mapped_column(JSONB, nullable=True)
    mention_roles_json: Mapped[str | None] = mapped_column(JSONB, nullable=True)
    mention_channels_json: Mapped[str | None] = mapped_column(JSONB, nullable=True)

    stickers_json: Mapped[str | None] = mapped_column(JSONB, nullable=True)
    raw_json: Mapped[str | None] = mapped_column(JSONB, nullable=True)

    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    attachments: Mapped[list[Attachment]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# Вложения (картинки и пр.)
# ---------------------------------------------------------------------------
class Attachment(Base):
    __tablename__ = "attachments"

    # PK = snowflake id вложения Discord (явный id, без sequence)
    id: Mapped[int] = mapped_column(SnowflakeId, primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), index=True)

    filename: Mapped[str] = mapped_column(String(512), default="")
    url: Mapped[str] = mapped_column(String(2048), default="")
    proxy_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(256), nullable=True)
    size: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_image: Mapped[bool] = mapped_column(Boolean, default=False)
    kind: Mapped[str | None] = mapped_column(String(32), nullable=True)  # image|meme|screenshot|document|archive|audio|video|other
    is_spoiler: Mapped[bool] = mapped_column(Boolean, default=False)
    stored_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    downloaded: Mapped[bool] = mapped_column(Boolean, default=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    message: Mapped[Message] = relationship(back_populates="attachments")


# ---------------------------------------------------------------------------
# Снимки профиля (история изменений био/статуса/баннера/ссылок во времени)
# ---------------------------------------------------------------------------
class ProfileSnapshot(Base):
    __tablename__ = "profile_snapshots"

    # BIGINT PK + bigserial на PostgreSQL (см. _pg_bigserial_pk в migrations.py)
    id: Mapped[int] = mapped_column(SnowflakeId, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_profiles.id"), index=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    about: Mapped[str | None] = mapped_column(Text, nullable=True)        # bio
    status_text: Mapped[str | None] = mapped_column(String(256), nullable=True)  # кастомный статус
    banner_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)   # смена баннера по хешу URL
    avatar_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    connections_json: Mapped[str | None] = mapped_column(JSONB, nullable=True)  # привязки на момент снимка
    changed: Mapped[bool] = mapped_column(Boolean, default=True)  # отличается ли от предыдущего снимка

    user: Mapped[UserProfile] = relationship()


# ---------------------------------------------------------------------------
# Состояние опроса канала (курсор last fetched message)
# ---------------------------------------------------------------------------
class PsychProfile(Base):
    """Психологический профиль пользователя — ответ нейросети-аналитика.

    Полный JSON ответа хранится в result_json (без разбивки на поля);
    ключевые числовые/категориальные метрики дублируются в колонки для
    фильтрации и сравнений. Дисклеймер не сохраняется: вместо него
    фиксируются период анализа и число записей (period_from/to,
    messages_analyzed). Старые версии не удаляются — активна одна (is_current).
    """

    __tablename__ = "psych_profiles"

    # BIGINT PK + bigserial на PostgreSQL (см. _pg_bigserial_pk в migrations.py)
    id: Mapped[int] = mapped_column(SnowflakeId, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    model: Mapped[str | None] = mapped_column(Text)            # id модели LLM
    prompt_hash: Mapped[str | None] = mapped_column(Text)      # контроль транскрипта
    source_lines: Mapped[int | None] = mapped_column(Integer)  # строк чата в промпте
    period_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    messages_analyzed: Mapped[int | None] = mapped_column(Integer)
    big_five_openness: Mapped[float | None] = mapped_column(Float, nullable=True)
    big_five_conscientiousness: Mapped[float | None] = mapped_column(Float, nullable=True)
    big_five_extraversion: Mapped[float | None] = mapped_column(Float, nullable=True)
    big_five_agreeableness: Mapped[float | None] = mapped_column(Float, nullable=True)
    big_five_neuroticism: Mapped[float | None] = mapped_column(Float, nullable=True)
    mbti_type: Mapped[str | None] = mapped_column(String(8))
    mbti_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    enneagram_type: Mapped[str | None] = mapped_column(String(8))
    enneagram_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence_overall: Mapped[float | None] = mapped_column(Float, nullable=True)
    severity_max: Mapped[str | None] = mapped_column(String(8))  # high|med|low
    result_json: Mapped[str | None] = mapped_column(JSONB)        # полный ответ LLM
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        # список текущих профилей одного пользователя (админка/экспорт)
        Index("ix_psych_current_user", "user_id", "created_at"),
    )


class PsychSignal(Base):
    """Атомарные сигналы/примеры из ответа LLM (evidence).

    Дедуп по нормализованной цитате на уровне пользователя: повторный
    импорт того же анализа не раздувает базу (ON CONFLICT DO NOTHING).
    """

    __tablename__ = "psych_signals"

    # BIGINT PK + bigserial на PostgreSQL (см. _pg_bigserial_pk в migrations.py)
    id: Mapped[int] = mapped_column(SnowflakeId, primary_key=True, autoincrement=True)
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("psych_profiles.id", ondelete="CASCADE"), index=True, nullable=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    signal_key: Mapped[str | None] = mapped_column(Text)   # slug(signal)
    quote_norm: Mapped[str] = mapped_column(Text)          # нормализованная цитата
    quote: Mapped[str | None] = mapped_column(Text)        # исходная цитата <=200 симв.
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    context: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str | None] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Дедуп по (user_id, signal_key, quote_norm): обычный UNIQUE — именно на
    # эти колонки ссылается ON CONFLICT в analysis_importer.
    __table_args__ = (
        UniqueConstraint(
            "user_id", "signal_key", "quote_norm", name="uq_psych_signals_dedup"
        ),
    )


class UserInterest(Base):
    """Интересы: накапливаются между срезами диалога (merge UPSERT)."""

    __tablename__ = "user_interests"

    # BIGINT PK + bigserial на PostgreSQL (см. _pg_bigserial_pk в migrations.py)
    id: Mapped[int] = mapped_column(SnowflakeId, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    topic: Mapped[str] = mapped_column(Text)
    topic_norm: Mapped[str] = mapped_column(Text)           # lower+collapse для дедупа
    evidence: Mapped[str | None] = mapped_column(Text)      # последняя цитата
    evidence_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "user_id", "topic_norm", name="uq_user_interests_norm"
        ),
    )


class ChannelState(Base):
    __tablename__ = "channel_states"

    # PK = snowflake id канала (явный id, без sequence)
    channel_id: Mapped[int] = mapped_column(SnowflakeId, primary_key=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Самый старый загруженный id (для резюма backfill без ограничения по дате)
    oldest_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    messages_total: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------------------
# Ревизии сообщений (миграция v10)
# Каждый on_message_edit сохраняет ПРЕЖНИЙ текст до правки. Без этого история
# редактирований теряется: update_message_edited перезаписывает content, и
# «как человек меняет формулировки» (важнейший сигнал для психопрофилирования)
# становится недоступен. edit_number — порядковый номер правки сообщения.
# ---------------------------------------------------------------------------
class MessageRevision(Base):
    __tablename__ = "message_revisions"

    # BIGINT PK + bigserial на PostgreSQL (см. _pg_bigserial_pk в migrations.py)
    id: Mapped[int] = mapped_column(SnowflakeId, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(BigInteger, index=True)
    author_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    edit_number: Mapped[int] = mapped_column(Integer, default=1)
    prev_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_message_revisions_msg_edit", "message_id", "edit_number"),
    )


# ---------------------------------------------------------------------------
# Событийная модель реакций (миграция v10)
# reactions_json в messages — это снимок «на момент опроса»: видно только
# текущее состояние (эмодзи/счётчик), но НЕ видно кто реагировал, когда и
# когда снял реакцию. Для анализа динамики отношений (кто чью поддержку
# выдаёт, взаимность реакций) нужна событийная лента REACTION_ADD/REMOVE.
# ---------------------------------------------------------------------------
class ReactionEvent(Base):
    __tablename__ = "reaction_events"

    # BIGINT PK + bigserial на PostgreSQL (см. _pg_bigserial_pk в migrations.py)
    id: Mapped[int] = mapped_column(SnowflakeId, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    msg_author_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    emoji: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(8))  # add | remove
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "message_id", "user_id", "emoji", name="uq_reaction_event_dedup"
        ),
        Index("ix_reaction_events_author_time", "msg_author_id", "occurred_at"),
    )


# ---------------------------------------------------------------------------
# Presence-снапшоты (миграция v10)
# Присутствие (online/idle/dnd/offline + кастомный статус) обновляется rarely
# и в profile_snapshots попадает только при смене био/аватара. Отдельная
# событийная таблица даёт историю смены статусов: режимы сна/работы,
# «невидимка», частота смены кастомного статуса.
# ---------------------------------------------------------------------------
class PresenceSnapshot(Base):
    __tablename__ = "presence_snapshots"

    # BIGINT PK + bigserial на PostgreSQL (см. _pg_bigserial_pk в migrations.py)
    id: Mapped[int] = mapped_column(SnowflakeId, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)  # online|idle|dnd|offline|invisible
    status_text: Mapped[str | None] = mapped_column(String(256), nullable=True)  # custom status state
    activity_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    activity_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_presence_user_time", "user_id", "observed_at"),
    )


# ---------------------------------------------------------------------------
# Персистентные агрегаты взаимодействий (миграция v10)
# Метрики диалогичности («кто кому отвечает», взаимность реакций) раньше
# считались на лету при экспорте тяжёлыми запросами. Пары directed-агрегатов
# (A→B) обновляются триггерами на PostgreSQL; SQLite-совместимость не нужна
# (проект PG-only, см. заголовок migrations.py).
# ---------------------------------------------------------------------------
_TRG_REPLY_PAIR = """
CREATE OR REPLACE FUNCTION trg_messages_reply_pair() RETURNS trigger AS $$
BEGIN
    IF NEW.reply_to_author_id IS NOT NULL AND NEW.author_id IS NOT NULL THEN
        INSERT INTO interaction_aggregates
            (source_user_id, target_user_id, replies_given, updated_at)
        VALUES (NEW.author_id, NEW.reply_to_author_id, 1, now())
        ON CONFLICT (source_user_id, target_user_id) DO UPDATE
            SET replies_given = interaction_aggregates.replies_given + 1,
                updated_at = now();
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_TRG_REACTION_PAIR = """
CREATE OR REPLACE FUNCTION trg_reaction_events_pair() RETURNS trigger AS $$
BEGIN
    IF NEW.action = 'add'
       AND NEW.user_id IS NOT NULL AND NEW.msg_author_id IS NOT NULL
       AND NEW.user_id <> NEW.msg_author_id THEN
        INSERT INTO interaction_aggregates
            (source_user_id, target_user_id, reactions_received, updated_at)
        VALUES (NEW.msg_author_id, NEW.user_id, 1, now())
        ON CONFLICT (source_user_id, target_user_id) DO UPDATE
            SET reactions_received = interaction_aggregates.reactions_received + 1,
                updated_at = now();
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


class InteractionAggregate(Base):
    __tablename__ = "interaction_aggregates"

    # BIGINT PK + bigserial на PostgreSQL (см. _pg_bigserial_pk в migrations.py)
    id: Mapped[int] = mapped_column(SnowflakeId, primary_key=True, autoincrement=True)
    source_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    replies_given: Mapped[int] = mapped_column(Integer, default=0)      # A ответил B
    reactions_received: Mapped[int] = mapped_column(Integer, default=0) # B отреагировал на A
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("source_user_id", "target_user_id", name="uq_interaction_pair"),
    )
