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
    ProfileSnapshot,
    UserGame,
    UserRoleTag,
    UserProfile,
    utcnow,
)

log = logging.getLogger("repository")

HASHTAG_RE = re.compile(r"(?<!\w)(?:#[a-zA-Z0-9_]{2,})|(?<!\w)<#\d+>")
USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")


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
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Создать/обновить глобальный профиль + серверный профиль + теги-роли.

        roles — динамика ролей: существующим ролям обновляется last_seen,
        новые получают first_seen=last_seen=now; исчезнувшие помечаются
        last_lost_at (роль не удаляется — история важна для анализа).
        snapshot — снимок профиля (bio/status/banner/connections): пишется в
        profile_snapshots только если содержимое изменилось с прошлого раза.
        """
        now = utcnow()
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

            if roles is not None:
                existing = {
                    r.role_id: r
                    for r in s.scalars(select(UserRoleTag).where(UserRoleTag.user_id == user_id))
                }
                seen_ids: set[int] = set()
                for r in roles:
                    rid = r["role_id"]
                    seen_ids.add(rid)
                    cur = existing.get(rid)
                    if cur is None:
                        s.add(UserRoleTag(user_id=user_id, first_seen=now, last_seen=now, **r))
                    else:
                        cur.last_seen = now
                        cur.last_lost_at = None  # вернулась после отсутствия
                        for k, v in r.items():
                            if k != "role_id" and hasattr(cur, k) and v is not None:
                                setattr(cur, k, v)
                for rid, cur in existing.items():
                    if rid not in seen_ids and cur.last_lost_at is None:
                        cur.last_lost_at = now

            if snapshot:
                prev = s.scalars(
                    select(ProfileSnapshot)
                    .where(ProfileSnapshot.user_id == user_id)
                    .order_by(ProfileSnapshot.id.desc())
                    .limit(1)
                ).first()
                same = (
                    prev is not None
                    and prev.about == snapshot.get("about")
                    and prev.status_text == snapshot.get("status_text")
                    and prev.banner_hash == snapshot.get("banner_hash")
                    and prev.avatar_hash == snapshot.get("avatar_hash")
                    and prev.connections_json == snapshot.get("connections_json")
                )
                if not same:
                    s.add(
                        ProfileSnapshot(
                            user_id=user_id, taken_at=now, changed=True,
                            about=snapshot.get("about"),
                            status_text=snapshot.get("status_text"),
                            banner_hash=snapshot.get("banner_hash"),
                            avatar_hash=snapshot.get("avatar_hash"),
                            connections_json=snapshot.get("connections_json"),
                        )
                    )

            s.commit()

    def get_user(self, user_id: int) -> UserProfile | None:
        with self._sf() as s:
            return s.scalars(select(UserProfile).where(UserProfile.user_id == user_id)).first()

    # ------------------------------------------------------------------ games
    def replace_user_games(self, user_id: int, games: list[dict[str, Any]]) -> int:
        """Upsert списка игр/активностей пользователя. Обновляет last_seen у
        совпавших по имени, добавляет новые; старые НЕ удаляет (история игр
        полезна для анализа). Возвращает число новых записей."""
        if not games:
            return 0
        now = datetime.now(timezone.utc)
        added = 0
        with self._sf() as s:
            existing = {
                g.name.lower(): g
                for g in s.scalars(select(UserGame).where(UserGame.user_id == user_id))
            }
            for g in games:
                name = (g.get("name") or "").strip()[:256]
                if not name:
                    continue
                row = existing.get(name.lower())
                if row is None:
                    s.add(
                        UserGame(
                            user_id=user_id,
                            name=name,
                            kind=g.get("kind"),
                            details=(g.get("details") or "")[:512] or None,
                            state=(g.get("state") or "")[:512] or None,
                            url=g.get("url"),
                            application_id=g.get("application_id"),
                        )
                    )
                    existing[name.lower()] = True  # защита от дублей внутри пачки
                    added += 1
                elif isinstance(row, UserGame):
                    row.last_seen = now
                    if g.get("details") and not row.details:
                        row.details = (g["details"] or "")[:512]
                    if g.get("url") and not row.url:
                        row.url = g["url"]
            s.commit()
        return added

    def all_user_ids(self) -> list[int]:
        """Все известные user_id — для периодического рефреша профилей."""
        with self._sf() as s:
            return list(s.scalars(select(UserProfile.user_id).order_by(UserProfile.user_id)))

    def profile_needs_refresh(self, user_id: int, days: float) -> bool:
        """True, если профиль не обновлялся дольше `days` дней (или никогда)."""
        u = self.get_user(user_id)
        if u is None or u.last_updated is None:
            return True
        ts = u.last_updated
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
        return age_days >= days

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

    def update_message_edited(
        self, message_id: int, edited_at: datetime | None, content: str
    ) -> None:
        """Зафиксировать редактирование: новый контент + пересчёт метрик.

        Метрики (word/char/hashtag/mentions/reactions) важны для будущего
        психотипирования — при редактировании они пересчитываются заново.
        Если edited_at не пришёл из события (либа sometimes теряет поле),
        подставляем текущее время, чтобы факт правки всё равно сохранился.
        """
        with self._sf() as s:
            msg = s.get(Message, message_id)
            if msg is None:
                return
            msg.edited_at = edited_at or datetime.now(timezone.utc)
            msg.content = content or ""
            msg.clean_content = content or ""
            msg.word_count = len((content or "").split())
            msg.char_count = len(content or "")
            msg.hashtag_count = len(HASHTAG_RE.findall(content or ""))
            msg.mention_count = len(USER_MENTION_RE.findall(content or "")) + len(
                ROLE_MENTION_RE.findall(content or "")
            )
            s.commit()

    def mark_message_deleted(self, message_id: int) -> bool:
        """Пометить сообщение удалённым (данные сохраняем — это история)."""
        with self._sf() as s:
            msg = s.get(Message, message_id)
            if msg is None:
                return False
            if msg.deleted_at is None:
                msg.deleted_at = datetime.now(timezone.utc)
                s.commit()
            return True

    # ---------------------------------------------------- связи диалога (reply graph)
    def link_reply_references(self) -> int:
        """Достроить связи «сообщение → на кого ответ» по reply-цепочкам.

        Для каждого сохранённого сообщения с ``reference_id`` (ответ на другое
        сообщение): если родитель уже в БД — проставляем ``reply_to_author_id``
        из автора родителя. Если родителя нет (удалён/вне периода сбора) и это
        обычный reply, связь НЕ угадывается по упоминаниям: <@id> в тексте
        чаще всего пинг третьих лиц, а не адресат. Для сообщений БЕЗ
        reference_id считаем адресатом первое resolved-упоминание (reply без
        reference обычно сопровождается явным пином адресата).

        Идемпотентно: повторный пропуск ничего не меняет. Возвращает число
        обновлённых строк.
        """
        with self._sf() as s:
            rows = s.execute(
                select(
                    Message.id,
                    Message.reference_id,
                    Message.author_id,
                    Message.mention_users_json,
                ).where(
                    Message.reply_to_author_id.is_(None),
                )
            ).all()
            updated = 0
            for mid, ref_id, author_id, mentions_json in rows:
                target = None
                if ref_id is not None and ref_id != mid:
                    parent_author = s.scalar(
                        select(Message.author_id).where(Message.id == ref_id)
                    )
                    if parent_author is not None and parent_author != author_id:
                        target = parent_author
                elif ref_id is None and mentions_json:
                    import json

                    try:
                        mentioned = json.loads(mentions_json)
                    except (ValueError, TypeError):
                        mentioned = []
                    for uid in mentioned:
                        if int(uid) != author_id:
                            target = int(uid)
                            break
                if target is not None:
                    s.execute(
                        Message.__table__.update()
                        .where(Message.id == mid)
                        .values(reply_to_author_id=target)
                    )
                    updated += 1
            s.commit()
            return updated

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
