"""Repository-слой (Model): персист данных через SQLAlchemy поверх PostgreSQL.

Вся работа с БД сосредоточена здесь — контроллеры и сервисы не пишут
SQL/ORM-запросы напрямую.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.models.entities import (
    RoleDefinition,
    Attachment,
    ChannelState,
    GuildMemberProfile,
    Message,
    MessageRevision,
    PresenceSnapshot,
    ProfileSnapshot,
    ReactionEvent,
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
        role_defs: Iterable[dict[str, Any]] | None = None,
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

            # Справочник ролей: name/color/position — один раз на роль.
            # Переименование роли в Discord обновляет ОДНУ строку roles, а не
            # все связи user_roles (см. миграцию a00000000004).
            for rd in role_defs or []:
                rid_, gid_ = rd.get("role_id"), rd.get("guild_id")
                if rid_ is None or gid_ is None:
                    continue
                cur = s.scalars(select(RoleDefinition).where(
                    RoleDefinition.guild_id == gid_,
                    RoleDefinition.role_id == rid_)).first()
                if cur is None:
                    cur = RoleDefinition(guild_id=gid_, role_id=rid_,
                                         first_seen=now, last_seen=now)
                    s.add(cur)
                else:
                    cur.last_seen = now
                for k in ("name", "position", "color", "is_mentionable",
                          "permissions_raw"):
                    v = rd.get(k)
                    if v is not None and hasattr(cur, k):
                        setattr(cur, k, v)

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
                    .where(ProfileSnapshot.user_id == user.id)
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
                            user_id=user.id, taken_at=now, changed=True,
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
    def purge_custom_status_games(self, user_id: int | None = None) -> int:
        """Удаляет из user_games записи-«мусор» от старых версий маппера:
        кастомный статус (kind содержит 'custom' / 'ActivityType') — это не
        игра, теперь он пишется только в profile_snapshots.status_text.

        custom-запись одна на пользователя (name == user_id), поэтому
        удаление безопасно и не трогает реальные игры. Возвращает число
        удалённых строк."""
        from sqlalchemy import or_
        q = delete(UserGame).where(
            or_(
                UserGame.kind.is_(None),
                UserGame.kind.ilike("%custom%"),
                UserGame.kind.ilike("%activitytype%"),
            )
        )
        if user_id is not None:
            q = q.where(UserGame.user_id == user_id)
        with self._sf() as s:
            n = s.execute(q).rowcount or 0
            s.commit()
        return n

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

    # -------------------------------------------------------------- ЛС-сбор
    def set_dm_fetch(self, user_id: int, enabled: bool) -> bool:
        """Включить/выключить сбор ЛС (кнопка в карточке пользователя).

        Если профиль ещё не собран (пользователь виден только по id из
        сообщений), создаём заглушку — dm-loop сам догрузит профиль через
        /users/{id}/profile.
        """
        from sqlalchemy import update
        with self._sf() as s:
            res = s.execute(
                update(UserProfile)
                .where(UserProfile.user_id == user_id)
                .values(dm_fetch=bool(enabled))
            )
            if res.rowcount == 0 and enabled:
                s.add(UserProfile(user_id=user_id, dm_fetch=True))
            s.commit()
            return True

    def set_dm_channel(self, user_id: int, channel_id: int | None) -> bool:
        """Задать DM-канал вручную (миграция v13).

        Используется, когда ЛС закрыты от не-друзей (create_dm -> 403), но
        диалог уже открыт в клиенте и id канала виден в адресной строке
        Discord (https://discord.com/channels/@me/<channel_id>).

        channel_id=None — снять ручную привязку (вернуться к авто-режиму).
        При установке нового id курсоры сбрасываются: старая привязка могла
        вести к другому диалогу. Если канал тот же — курсоры сохраняются
        (продолжаем выгрузку без повторов).
        """
        from sqlalchemy import update
        with self._sf() as s:
            prof = s.scalars(
                select(UserProfile).where(UserProfile.user_id == user_id)
            ).first()
            if prof is None:
                s.add(UserProfile(user_id=user_id, dm_fetch=True,
                                  dm_channel_id=channel_id,
                                  dm_channel_manual=channel_id is not None))
                s.commit()
                return True
            vals: dict[str, Any] = {
                "dm_channel_manual": channel_id is not None,
            }
            if channel_id is None:
                vals["dm_channel_id"] = None
            elif prof.dm_channel_id != channel_id:
                # новый ручной канал — старые курсоры невалидны
                vals.update({
                    "dm_channel_id": channel_id,
                    "dm_last_message_id": None,
                    "dm_first_message_id": None,
                    "dm_messages_total": 0,
                    "dm_last_error": None,
                    "dm_fail_count": 0,
                })
            else:
                # тот же канал: просто снимаем диагностику, чтобы dm-loop
                # сразу попробовал снова
                vals.update({"dm_last_error": None, "dm_fail_count": 0})
            s.execute(
                update(UserProfile)
                .where(UserProfile.user_id == user_id)
                .values(**vals)
            )
            s.commit()
            return True

    def dm_channel_ids(self) -> set[int]:
        """id каналов ЛС, для которых включён сбор — live-события из этих
        каналов обрабатываются даже когда основной цикл сбора выключен."""
        with self._sf() as s:
            return {int(cid) for cid in s.scalars(
                select(UserProfile.dm_channel_id)
                .where(UserProfile.dm_fetch.is_(True))
                .where(UserProfile.dm_channel_id.isnot(None))
            )}

    def dm_fetch_user_ids(self) -> list[int]:
        """user_id пользователей, у которых включён сбор ЛС (dm_fetch = True).

        Приостановленные после серии сбоев (dm_fail_count >= 5) не
        возвращаются — чтобы «нет доступа к ЛС» не повторялось вечно.
        Повторное нажатие кнопки в админке сбрасывает счётчик.
        """
        with self._sf() as s:
            return list(s.scalars(
                select(UserProfile.user_id)
                .where(UserProfile.dm_fetch.is_(True))
                .where(or_(
                    UserProfile.dm_fail_count.is_(None),
                    UserProfile.dm_fail_count < 5,
                ))
                .order_by(UserProfile.user_id)
            ))

    def pending_dm_user_ids(self) -> list[int]:
        """Пользователи с сигналом «▶ Выполнить сейчас» (миграция v15).

        Отдельно от dm_fetch_user_ids: внеочередной проход выполняется и для
        приостановленных после серии сбоев — явная команда человека важнее
        автомата (счётчик сбоев сбрасывает сам обработчик).
        """
        with self._sf() as s:
            return list(s.scalars(
                select(UserProfile.user_id)
                .where(UserProfile.pending_dm.is_(True))
                .order_by(UserProfile.user_id)
            ))

    def claim_pending_dm(self, user_id: int) -> bool:
        """Атомарно забрать сигнал «выполнить сейчас»: pending_dm True->False.

        Возвращает True, если флаг принадлежал нам (защита от двойного
        выполнения, если два прохода/процесса увидели один сигнал).
        """
        from sqlalchemy import update
        with self._sf() as s:
            res = s.execute(
                update(UserProfile)
                .where(UserProfile.user_id == user_id)
                .where(UserProfile.pending_dm.is_(True))
                .values(pending_dm=False)
            )
            s.commit()
            return res.rowcount > 0

    def set_pending_dm(self, user_id: int) -> bool:
        """Поставить сигнал «выполнить сейчас» из админки (v15).

        Сбрасывает диагностику сбоев (fail_count/last_error): ручная команда
        возвращает в игру и приостановленных пользователей. Профиль должен
        существовать; возвращает False, если строки нет.
        """
        from sqlalchemy import update
        with self._sf() as s:
            res = s.execute(
                update(UserProfile)
                .where(UserProfile.user_id == user_id)
                .values(pending_dm=True, dm_fail_count=0, dm_last_error=None)
            )
            s.commit()
            return res.rowcount > 0

    def claim_dm_run(self, user_id: int) -> bool:
        """Атомарно занять пользователя для немедленного прохода («выполнить сейчас»).

        Ставит dm_status='running' только если он ещё не 'running'
        (UPDATE ... WHERE dm_status IS DISTINCT FROM 'running'). Возвращает
        True, если место заняли мы — защита от параллельных выгрузок одного
        диалога из dm-loop и ручного запуска.
        """
        from sqlalchemy import update
        with self._sf() as s:
            res = s.execute(
                update(UserProfile)
                .where(UserProfile.user_id == user_id)
                .where(UserProfile.dm_status.is_distinct_from("running"))
                .values(dm_status="running")
            )
            s.commit()
            return res.rowcount > 0

    def release_dm_run(self, user_id: int, status: str | None = None) -> None:
        """Освободить занятие прохода: running -> ok/error/None."""
        self.update_dm_cursor(user_id, status=status)

    def update_dm_cursor(
        self,
        user_id: int,
        *,
        channel_id: int | None = None,
        oldest_message_id: int | None = None,
        newest_message_id: int | None = None,
        messages_total: int | None = None,
        synced: bool = False,
        reset_cursor: bool = False,
        last_error: str | None = ...,
        fail_count: int | None = ...,
        status: str | None = ...,
    ) -> None:
        """Прдвинуть курсоры ЛС-выгрузки. synced=True — поставить dm_synced_at=now.

        oldest_message_id записывается только если он меньше текущего курса
        (или курсора нет) — курсор всегда указывает на самый старый загруженный
        id, чтобы рестарт не пропускал и не повторял сообщения.

        last_error/fail_count (миграция v12): диагностика сбоев. Sentinel `...`
        означает «не трогать»; None для last_error — очистить ошибку.

        status (миграция v14): 'running'/'ok'/'error'/None — живой прогресс
        прохода dm-loop для виджета в админке. Sentinel `...` — не трогать.
        """
        from sqlalchemy import update
        with self._sf() as s:
            vals: dict[str, Any] = {}
            if channel_id is not None:
                vals["dm_channel_id"] = channel_id
            if messages_total is not None:
                vals["dm_messages_total"] = messages_total
            if newest_message_id is not None:
                vals["dm_first_message_id"] = newest_message_id
            if synced:
                vals["dm_synced_at"] = datetime.now(timezone.utc)
            if last_error is not ...:
                vals["dm_last_error"] = last_error
            if fail_count is not ...:
                vals["dm_fail_count"] = fail_count
            if status is not ...:
                vals["dm_status"] = status
            if reset_cursor:
                # ЛС-диалог открылся после «нет доступа» или канал сменился:
                # старые курсоры невалидны — начинаем выгрузку заново.
                vals.update({
                    "dm_channel_id": None,
                    "dm_last_message_id": None,
                    "dm_first_message_id": None,
                    "dm_messages_total": 0,
                    "dm_last_error": None,
                    "dm_fail_count": 0,
                })
            if vals:
                s.execute(
                    update(UserProfile)
                    .where(UserProfile.user_id == user_id)
                    .values(**vals)
                )
            if oldest_message_id is not None:
                cur = s.scalars(
                    select(UserProfile.dm_last_message_id)
                    .where(UserProfile.user_id == user_id)
                ).first()
                if cur is None or oldest_message_id < cur:
                    s.execute(
                        update(UserProfile)
                        .where(UserProfile.user_id == user_id)
                        .values(dm_last_message_id=oldest_message_id)
                    )
            s.commit()

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

        Динамика правок: ПРЕЖНИЙ текст сохраняется в message_revisions —
        без этого «как человек переформулирует мысли» недоступно анализу.
        Дедуп: если содержимое не изменилось (либа шлёт MESSAGE_EDIT и на
        смену embeds/реакций) — ревизия не пишется.
        """
        with self._sf() as s:
            msg = s.get(Message, message_id)
            if msg is None:
                return
            prev_content = msg.clean_content or ""
            new_content = content or ""
            if prev_content != new_content:
                last_edit = s.scalar(
                    select(func.max(MessageRevision.edit_number)).where(
                        MessageRevision.message_id == message_id
                    )
                ) or 0
                s.add(
                    MessageRevision(
                        message_id=message_id,
                        author_id=msg.author_id,
                        edit_number=last_edit + 1,
                        prev_content=prev_content,
                        new_content=new_content,
                        edited_at=edited_at or datetime.now(timezone.utc),
                    )
                )
            msg.edited_at = edited_at or datetime.now(timezone.utc)
            msg.content = new_content
            msg.clean_content = new_content
            msg.word_count = len(new_content.split())
            msg.char_count = len(new_content)
            msg.hashtag_count = len(HASHTAG_RE.findall(new_content))
            msg.mention_count = len(USER_MENTION_RE.findall(new_content)) + len(
                ROLE_MENTION_RE.findall(new_content)
            )
            s.commit()

    # ------------------------------------------------------- reaction events
    def record_reaction_event(
        self,
        message_id: int,
        user_id: int | None,
        emoji: str,
        action: str,
        *,
        channel_id: int | None = None,
        guild_id: int | None = None,
        msg_author_id: int | None = None,
    ) -> bool:
        """Сохранить событие реакции (add/remove). Идемпотентно по тройке
        (message_id, user_id, emoji, action): повторное одинаковое событие
        игнорируется, add после remove — новая строка истории (миграция
        a00000000005 расширила UNIQUE-ключ полем action). Возвращает True,
        если событие записано."""
        now = datetime.now(timezone.utc)
        with self._sf() as s:
            exists = s.scalars(
                select(ReactionEvent.id).where(
                    ReactionEvent.message_id == message_id,
                    ReactionEvent.user_id == user_id,
                    ReactionEvent.emoji == emoji,
                    ReactionEvent.action == action,
                ).limit(1)
            ).first()
            if exists is not None:
                return False
            s.add(
                ReactionEvent(
                    message_id=message_id,
                    user_id=user_id,
                    emoji=emoji[:128],
                    action=action,
                    channel_id=channel_id,
                    guild_id=guild_id,
                    msg_author_id=msg_author_id,
                    occurred_at=now,
                )
            )
            s.commit()
            return True

    # -------------------------------------------------------- presence log
    def record_presence(self, snapshots: list[dict[str, Any]]) -> int:
        """Пакетно записать presence-снапшоты. Дедуп против «шума»: строка
        не пишется, если у пользователя уже есть последний снимок с теми же
        status/status_text/activity_name. Возвращает число записанных строк."""
        if not snapshots:
            return 0
        written = 0
        with self._sf() as s:
            for snap in snapshots:
                uid = snap.get("user_id")
                if uid is None:
                    continue
                last = s.scalars(
                    select(PresenceSnapshot)
                    .where(PresenceSnapshot.user_id == uid)
                    .order_by(PresenceSnapshot.observed_at.desc())
                    .limit(1)
                ).first()
                if last is not None and (
                    last.status,
                    last.status_text,
                    last.activity_name,
                ) == (
                    snap.get("status"),
                    snap.get("status_text"),
                    snap.get("activity_name"),
                ):
                    continue
                s.add(
                    PresenceSnapshot(
                        user_id=int(uid),
                        status=snap.get("status"),
                        status_text=(snap.get("status_text") or "")[:256] or None,
                        activity_name=(snap.get("activity_name") or "")[:256] or None,
                        activity_kind=snap.get("activity_kind"),
                        observed_at=snap.get("observed_at") or utcnow(),
                    )
                )
                written += 1
            s.commit()
        return written

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
                    # mention_users_json — JSONB: ORM отдаёт list напрямую;
                    # строка поддержана для старых данных (двойная сериализация)
                    mentioned = mentions_json
                    if isinstance(mentioned, str):
                        import json

                        try:
                            mentioned = json.loads(mentioned)
                        except (ValueError, TypeError):
                            mentioned = []
                    if not isinstance(mentioned, list):
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
    def data_quality(self) -> dict[str, float]:
        """Индикатор полноты данных (Data completeness score).

        Доли (0..1) по целевой выборке приоритетных пользователей:
        - profile: заполнен about или global_name (статичная личность);
        - revisions: есть хотя бы одна сохранённая ревизия правки;
        - reactions: есть событийные реакции на их сообщения;
        - presence: есть presence-снапшоты.
        Пустая база => доли 0. Считается лёгкими EXISTS-запросами.
        """
        with self._sf() as s:
            users = s.scalars(
                select(UserProfile.user_id).where(UserProfile.priority == True)  # noqa: E712
            ).all()
            if not users:
                return {"users": 0, "profile": 0.0, "revisions": 0.0,
                        "reactions": 0.0, "presence": 0.0}
            n = len(users)
            uid_set = set(users)

            def frac(rows: Iterable) -> float:
                return round(len(set(rows) & uid_set) / n, 3)

            prof = frac(
                s.scalars(
                    select(UserProfile.user_id).where(
                        UserProfile.priority == True,  # noqa: E712
                        (UserProfile.about.isnot(None)) | (UserProfile.global_name.isnot(None)),
                    )
                ).all()
            )
            rev = frac(
                s.scalars(
                    select(MessageRevision.author_id)
                    .where(MessageRevision.author_id.in_(uid_set))
                    .distinct()
                ).all()
            )
            rxn = frac(
                s.scalars(
                    select(ReactionEvent.msg_author_id)
                    .where(ReactionEvent.msg_author_id.in_(uid_set))
                    .distinct()
                ).all()
            )
            pres = frac(
                s.scalars(
                    select(PresenceSnapshot.user_id)
                    .where(PresenceSnapshot.user_id.in_(uid_set))
                    .distinct()
                ).all()
            )
            return {"users": n, "profile": prof, "revisions": rev,
                    "reactions": rxn, "presence": pres}

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
                # --- v10: динамические сигналы --------------------------
                "revisions": s.scalar(select(func.count()).select_from(MessageRevision)) or 0,
                "reaction_events": s.scalar(select(func.count()).select_from(ReactionEvent)) or 0,
                "presence_snapshots": s.scalar(select(func.count()).select_from(PresenceSnapshot)) or 0,
            }
