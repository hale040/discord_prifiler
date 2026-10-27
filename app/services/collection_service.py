"""Service-слой: периодический сбор данных из канала + скачивание вложений.

Канал опрашивается так, как это делает живой пользователь:

  * «досылка» новых сообщений через ``history(after=cursor)`` — ровно так
    работает UI при открытии канала;
  * «прокрутка ленты вверх»small-страницами через ``history(before=..., limit=N)``
    для добора всей истории;
  * между запросами — случайные human-like паузы (см. utils.human_delay);
  * профили авторов добираются эндпоинтом ``/users/{id}/profile``
    (доступен только user-токену) с отдельной очереди и паузами;
  * картинки качаются в фоне, тоже с паузами.

Никаких исходящих сообщений/реакций — только чтение.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import time
from datetime import datetime, timezone
from typing import Any

import discord

from app.config import Settings
from app.models.repository import Repository
from app.services import mappers
from app.services.discord_gateway import UserbotClient
from app.utils.helpers import human_delay, to_json

log = logging.getLogger("collector")

# Discord epoch: 2015-01-01T00:00:00Z. Snowflake >> 22 + epoch = timestamp ms.
DISCORD_EPOCH_MS = 1_420_070_400_000


def snowflake_for(dt: datetime) -> int:
    """Минимальный snowflake id для сообщений, отправленных >= dt."""
    ms = int(dt.timestamp() * 1000) - DISCORD_EPOCH_MS
    return max(ms, 0) << 22


def _to_snowflake(value: int | None) -> discord.Object | None:
    """discord.py-self ожидает в history(after=/before=) объект с атрибутом .id.

    Числовой snowflake заворачиваем в discord.Object — иначе внутри
    abc.Messageable.history происходит AttributeError: 'int' object ...
    """
    if value is None:
        return None
    return discord.Object(id=int(value))


class CollectionService:
    # Класс-реестр активного сервиса: админка (отдельный процесс) находит
    # им экземпляр, чтобы вызвать немедленный проход ЛС «▶ Выполнить сейчас».
    _active_instance: "CollectionService | None" = None

    @classmethod
    def active(cls) -> "CollectionService | None":
        return cls._active_instance

    def __init__(self, settings: Settings, repo: Repository, client: UserbotClient) -> None:
        self.s = settings
        self.repo = repo
        self.client = client
        CollectionService._active_instance = self
        # view может быть подменён контроллером для доклада о прогрессе
        self.view = None
        # кэш уже опрофиленных пользователей в рамках процесса
        self._profiled: set[int] = set()
        self._queued_profiles: set[int] = set()
        self._profile_queue: asyncio.Queue[int] = asyncio.Queue()
        self._running = False
        self._tasks: list[asyncio.Task] = []
        # курсоры в рамках процесса: куда догружали историю (после restart —
        # резюм по oldest_message_id из БД)
        self._backfill_cursor: int | None = None
        # пользователи с активным немедленным проходом «▶ Выполнить сейчас»
        # (v14): dm-loop их пропускает, повторный клик не создаёт вторую выгрузку
        self._dm_manual_set: set[int] = set()
        # предыдущий poll-обход был усечён лимитом API (429/retry_after):
        # следующий проход выполняется немедленно, без паузы POLL_INTERVAL_SEC
        self._poll_incomplete = False
        # backfill идёт (история ещё не догружена) — live-события впереди
        # курсора это норма, gap-детектор молчит
        self._backfill_running = False

    def _notify(self, method: str, *args) -> None:
        if self.view is not None:
            try:
                getattr(self.view, method)(*args)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------- public API
    async def handle_live_message(self, msg: discord.Message, guild_id: int | None) -> bool:
        """Обработка live MESSAGE_CREATE (вызывается контроллером)."""
        return await self._handle_message(msg, guild_id)

    async def handle_live_edit(self, msg: Any, before: Any = None) -> bool:
        """Обработка live MESSAGE_EDIT: фиксируем новый контент и метрики."""
        mid = getattr(msg, "id", None) or getattr(before, "id", None)
        if not mid:
            return False
        content = getattr(msg, "content", None)
        clean = getattr(msg, "clean_content", None)
        # если либа прислала «пустой» partial без содержимого — берём из before
        if content is None and before is not None:
            content = getattr(before, "content", "")
        if clean is None:
            clean = content
        edited_at = getattr(msg, "edited_at", None) or getattr(before, "edited_at", None)
        if edited_at is not None and edited_at.tzinfo is None:
            edited_at = edited_at.replace(tzinfo=timezone.utc)
        await asyncio.to_thread(
            self.repo.update_message_edited, int(mid), edited_at, clean or ""
        )
        self._notify("on_message_edited", int(mid))
        return True

    async def handle_live_delete(self, msg_or_id: Any) -> bool:
        """Обработка live MESSAGE_DELETE: помечаем удалённым, данные храним."""
        mid = getattr(msg_or_id, "id", msg_or_id)
        try:
            mid = int(mid)
        except (TypeError, ValueError):
            return False
        marked = await asyncio.to_thread(self.repo.mark_message_deleted, mid)
        if marked:
            self._notify("on_message_deleted", mid)
        return marked

    # ------------------------------------------------------------------ run
    async def start(self) -> None:
        """Запуск фоновых циклов (вызывается после on_ready)."""
        if self._running:
            return
        self._running = True
        channel = await self.client.safe_fetch_channel(self.s.channel_id)
        if channel is None:
            raise RuntimeError(f"Канал {self.s.channel_id} недоступен — завершение.")
        guild_id = getattr(getattr(channel, "guild", None), "id", None)
        log.info(
            "Целевой канал: #%s (guild=%s). Старт периодического сбора.",
            getattr(channel, "name", channel.id), guild_id,
        )
        self._tasks = [
            asyncio.create_task(self._poll_loop(channel, guild_id), name="poll-loop"),
            asyncio.create_task(self._profile_worker(), name="profile-worker"),
            asyncio.create_task(self._attachment_worker(), name="attachment-worker"),
            asyncio.create_task(self._refresh_loop(), name="profile-refresh-loop"),
            asyncio.create_task(self._dm_loop(), name="dm-loop"),
        ]

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    # ------------------------------------------------------------- poll loop
    async def _poll_loop(self, channel: discord.abc.Messageable, guild_id: int | None) -> None:
        """Основной цикл: проверяем новые сообщения, затем ждём POLL_INTERVAL_SEC.

        Порядок выдержан специально: **сразу после запуска** (и сразу после
        завершения backfill) выполняется проверка новых сообщений без ожидания,
        далее цикл работает по схеме «обход завершён → спим ~5 мин → новый обход».
        """
        # Первый проход — полная выгрузка истории от старых к новым,
        # затем переходим в режим инкрементальной «досылки».
        try:
            self._backfill_running = True
            await self._backfill_history(channel, guild_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Ошибка первичной выгрузки истории")
        finally:
            self._backfill_running = False

        consecutive_errors = 0
        first_pass = True  # при запуске не спим: первая проверка — сразу
        while self._running:
            if not first_pass and not self._poll_incomplete:
                interval = self.s.poll_interval_sec * random.uniform(0.85, 1.25)
                log.debug("Ожидание перед следующим обходом: ~%.0f сек.", interval)
                await asyncio.sleep(interval)  # пауза между обходами (~5 мин)
            elif self._poll_incomplete:
                # предыдущий обход был усечён лимитом API — догоняем сразу
                log.info("Предыдущий обход оборвался на лимите — немедленное продолжение.")
            first_pass = False
            try:
                await self._fetch_new_messages(channel, guild_id)
                consecutive_errors = 0
            except discord.LoginFailure:
                log.error("Сессия недействительна (LoginFailure) — останавливаю сбор.")
                self._running = False
                self.client._ready_fired = False  # на случай внешнего перезапуска
                if getattr(self.client, "is_logged_in", lambda: False)():
                    await self.client.close()
                return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                consecutive_errors += 1
                log.exception("Ошибка инкрементального опроса канала (подряд: %s)", consecutive_errors)
                if consecutive_errors >= 3:
                    # экспоненциальный backoff: 4x, 8x, ... но не дольше 30 мин
                    penalty = min(interval * (2 ** consecutive_errors), 1800)
                    log.warning("Бэкофф: следующий опрос через ~%.0f сек.", penalty)
                    await asyncio.sleep(penalty)
                    consecutive_errors = 0

    # ----------------------------------------------------------- history ops
    async def _fetch_new_messages(self, channel: discord.abc.Messageable, guild_id: int | None) -> int:
        """Достаём сообщения после последнего известного курсора (как скролл вниз).

        Обход оборван по лимиту API (например, при rate-limit 429) — это НЕ
        «догнали настоящее»: помечаем ``self._poll_incomplete``, и следующий
        проход выполнится сразу, без паузы POLL_INTERVAL_SEC, чтобы не терять
        сообщения из-за усечённой страницы.
        """
        self._poll_incomplete = False
        state = self.repo.get_state(channel.id)
        after = state.last_message_id
        new_count = 0
        max_id = after
        kwargs: dict[str, Any] = {"limit": min(self.s.fetch_limit, 100), "oldest_first": True}
        if after:
            kwargs["after"] = _to_snowflake(after)

        batch: list[discord.Message] = []
        truncated = False
        try:
            async for msg in channel.history(**kwargs):
                batch.append(msg)
        except discord.HTTPException as exc:
            # обрыв страницы (429/retry_after/сбой CDN): сохраняем то, что уже
            # успели получить, и продолжаем немедленно с новой точки курсора
            truncated = True
            log.warning("Обход истории прерван (%s) — сохраняю %s полученных, "
                        "следующая проверка будет немедленной", exc, len(batch))
        batch.sort(key=lambda m: m.id)
        # всю пачку пишем в БД одним коммитом — меньше fsync/WAL-чреды
        for msg in batch:
            saved = await asyncio.to_thread(self._handle_message_sync, msg, guild_id)
            new_count += int(saved)
            if max_id is None or msg.id > max_id:
                max_id = msg.id
            await human_delay(self.s.request_delay_min / 4, self.s.request_delay_max / 4)

        if batch or new_count:
            self.repo.update_state(channel.id, max_id, new_count, guild_id)
        if new_count:
            log.info("Новых сообщений: %s (курсор -> %s)", new_count, max_id)
        else:
            log.debug("Новых сообщений нет.")
        self._notify("on_new_messages", new_count, max_id)
        if truncated and batch:
            self._poll_incomplete = True
        return new_count

    async def _backfill_history(self, channel: discord.abc.Messageable, guild_id: int | None) -> None:
        """Выгрузка истории от START_DATE (или от начала канала) к текущему моменту.

        Идём «вниз по ленте» маленькими страницами ``history(after=cursor,
        oldest_first=True)`` — так же, как человек листает канал вниз.
        Если задан START_DATE, запросы начинаются сразу с нужной даты
        (snowflake-курсор), а не с самого начала чата — лишняя история даже
        не запрашивается у API.

        Защита от зацикливания (главный риск after-стратегии): если страница
        вернула только уже известные сообщения, курсор всё равно двигается на
        max(id) страницы + добавляется hard-cap по количеству пустых страниц.
        """
        state = self.repo.get_state(channel.id)
        # Курсор: от чего идём вперёд по времени.
        if self._backfill_cursor is not None:
            # продолжение догружения после reconnect в рамках процесса
            after = self._backfill_cursor
        elif self.s.start_date is not None and not state.last_message_id:
            after = snowflake_for(self.s.start_date)
            log.info(
                "Backfill с даты старта %s (snowflake-курсор %s).",
                self.s.start_date.isoformat(), after,
            )
        else:
            # Резюм после рестарта процесса / второй запуск: продолжаем ровно
            # от последнего собранного сообщения. Иначе Discord вернёт уже
            # сохранённую страницу (page_cap), saved=0 и backfill будет
            # бесконечно крутиться на одном месте.
            after = state.last_message_id or (
                snowflake_for(self.s.start_date) if self.s.start_date is not None else 0
            )
            if state.last_message_id:
                log.info("Backfill: резюм от последнего курсора %s.", after)
        total_pages = 0
        empty_pages = 0
        while self._running:
            kwargs: dict[str, Any] = {
                "limit": random.randint(25, 50),  # маленькие страницы — «человечнее»
                "after": _to_snowflake(after),
                "oldest_first": True,
            }
            batch: list[discord.Message] = []
            truncated = False
            try:
                async for msg in channel.history(**kwargs):
                    batch.append(msg)
            except discord.HTTPException as exc:
                # лимит API оборвал страницу: сохраняем полученное, выходим в
                # poll-режим; там _poll_incomplete заставит продолжить сразу,
                # без паузы POLL_INTERVAL_SEC (курсор по последнему сохранённому)
                truncated = True
                log.warning("Backfill прерван лимитом API (%s) после %s страниц.",
                            exc, total_pages)
            if not batch:
                if truncated:
                    log.info("Backfill: страница пуста и оборвана лимитом — продолжим в poll.")
                    self._backfill_cursor = after
                    return
                log.info(
                    "История догружена до текущего момента (%s страниц, курсор=%s).",
                    total_pages, after,
                )
                break
            batch.sort(key=lambda m: m.id)  # сохраняем строго хронологически
            saved = 0
            for msg in batch:
                # БД-запись уводим из event loop в поток — иначе многостраничный
                # backfill блокирует gateway и рвёт heartbeat-сессию.
                saved += int(await asyncio.to_thread(self._handle_message_sync, msg, guild_id))
            oldest_id = batch[0].id
            newest_id = batch[-1].id
            total_pages += 1
            # last_message_id — самый новый (для инкрементального poll),
            # oldest_message_id — самая старая загруженная точка (для резюма).
            self.repo.update_state(channel.id, newest_id, saved, guild_id)
            self.repo.mark_oldest(channel.id, oldest_id)
            # Двигаем курсор строго вперёд независимо от того, сколько
            # реально сохранилось: иначе при page_cap / дублях цикл
            # зацикливается на одной и той же странице.
            if newest_id <= after:
                log.warning(
                    "Страница не продвинула курсор (%s <= %s) — прерываю backfill.",
                    newest_id, after,
                )
                break
            # достройка связей «кто кому отвечает» по уже сохранённым reply-ссылкам
            try:
                linked = self.repo.link_reply_references()
                if linked:
                    log.info("Backfill: достроено связей ответов: %s", linked)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка достройки графа ответов (страница %s)", total_pages)
            after = newest_id
            self._backfill_cursor = newest_id
            if saved == 0:
                empty_pages += 1
                # long-term cap: страница целиком состоит из уже собранных
                # сообщений — скорее всего повторная выгрузка, останавливаемся
                # после нескольких таких страниц подряд.
                if empty_pages >= 5:
                    log.info(
                        "Backfill: %s страниц подряд без новых сообщений — стоп (курсор=%s).",
                        empty_pages, after,
                    )
                    break
            else:
                empty_pages = 0
            log.info(
                "Backfill: страница %s, сохранено %s/%s, диапазон id %s..%s",
                total_pages, saved, len(batch), oldest_id, newest_id,
            )
            self._notify(
                "on_backfill_page", total_pages, saved, len(batch), oldest_id
            )
            # большая «пауза на прочтение» между страницами истории
            await human_delay(self.s.request_delay_min, self.s.request_delay_max * 2)

    # ------------------------------------------------------------ processing
    def _handle_message_sync(self, msg: discord.Message, guild_id: int | None) -> bool:
        """Синхронное ядро обработки сообщения (БД-операции).

        Вызывается либо напрямую из event loop (live-события), либо через
        asyncio.to_thread во время bulk-backfill — чтобы долгие SQLite-коммиты
        не блокировали gateway-сокет (иначе Discord видит лаги и рвёт сессию).

        guild_id=None + is_dm=True — сообщение из ЛС; для таких фильтр по
        дате начала сбора не применяется (история диалога выгружается целиком).
        """
        # Фильтр по дате начала сбора: всё, что раньше START_DATE, игнорируем.
        if self.s.start_date is not None and guild_id is not None:
            ts = getattr(msg, "created_at", None)
            if ts is not None and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts is not None and ts < self.s.start_date:
                return False
        # Защита от потери сообщений при простое: live-событие может прийти,
        # когда poll-курсор отстаёт (сервис стоял, лимиты API). Прыжок
        # last_message_id через пропуск закрыл бы дыру навсегда — вместо этого
        # фиксируем gap и запускаем немедленный догон-обход (_fetch_new_messages
        # прочитает всё после старого курсора; дедупликация вставки безопасна).
        state = self.repo.get_state(msg.channel.id)
        if (
            guild_id is not None
            and state is not None
            and state.last_message_id
            and msg.id > state.last_message_id + 4096
            and not self._backfill_running
        ):
            if not self._poll_incomplete:
                log.info("Gap после курсора %s (новое %s) — немедленный догон истории.",
                         state.last_message_id, msg.id)
            self._poll_incomplete = True
        data = mappers.message_data(msg, guild_id, store_raw=self.s.store_raw_json)
        atts = mappers.attachments_data(msg)
        saved = self.repo.insert_message(data, atts)
        if not saved:
            # сообщение уже есть — обновим только факт редактирования
            if data.get("edited_at"):
                self.repo.update_message_edited(
                    data["id"], data["edited_at"], data.get("clean_content", "")
                )
            return False
        # автор + упомянутые => в очередь профилирования
        for uid in mappers.extract_user_ids(msg):
            self._queue_profile(uid)
        # участники, пришедшие прямо в payload сообщения (полные member-объекты)
        if getattr(msg, "guild", None) is not None:
            member_objs = [msg.author] + list(msg.mentions or [])
            for u in member_objs:
                if isinstance(u, discord.Member):
                    try:
                        gf, roles = mappers.guild_and_roles_from_member(u)
                        rdefs = [mappers.role_def_from_role(r, u.guild.id)
                                 for r in (getattr(u, "roles", None) or [])
                                 if hasattr(r, "id") and r.id != u.guild.id]
                        base = mappers.user_fields(u)
                        if gf or base:
                            self.repo.upsert_user_profile(u.id, base, gf or None, roles or None,
                                                          role_defs=rdefs or None)
                    except Exception:  # noqa: BLE001
                        log.debug("Не удалось сохранить server-данные из payload: %s", u.id)
        return True

    async def _handle_message(self, msg: discord.Message, guild_id: int | None) -> bool:
        return self._handle_message_sync(msg, guild_id)

    def _queue_profile(self, user_id: int, force: bool = False) -> None:
        if user_id in self._queued_profiles:
            return
        if not force and user_id in self._profiled:
            return
        self._queued_profiles.add(user_id)
        self._profile_queue.put_nowait(user_id)

    # ------------------------------------------------- profile refresh loop
    # ------------------------------------------------------------- ЛС-сбор
    async def _dm_loop(self) -> None:
        """Цикл выгрузки личных сообщений (ЛС).

        Работает ТОЛЬКО для пользователей, у которых в карточке админки
        нажата кнопка «💬 Собрать ЛС» (dm_fetch = True). Каждую итерацию
        читаем список из БД — включили кнопку, и на следующем тике диалог
        начал грузиться; выключили — цикл его пропускает.

        После первой полной выгрузки диалога проход становится дешёвым:
        one-shot запрос history(after=dm_first_message_id) забирает только
        новые сообщения с момента последней синхронизации.
        """
        await asyncio.sleep(10)  # дать онбордингу/главному poll-циклу стартовать
        while self._running:
            # --- внеочередные проходы «▶ Выполнить сейчас» (миграция v15) ---
            # Админка — отдельный процесс, прямого вызова в наш event loop нет;
            # сигнал живёт в БД (pending_dm). Обрабатываем их до обычной
            # очереди и коротким переопросом (каждые ~15 сек), чтобы кнопка
            # отзывалась быстро, а не к концу DM_POLL_INTERVAL_SEC.
            pending_deadline = time.monotonic() + getattr(
                self.s, "dm_poll_interval_sec", 300)
            while self._running and time.monotonic() < pending_deadline:
                try:
                    pend = await asyncio.to_thread(self.repo.pending_dm_user_ids)
                except Exception:  # noqa: BLE001
                    log.exception("dm-loop: не удалось прочитать pending_dm")
                    pend = []
                for uid in pend:
                    if not self._running:
                        break
                    # атомарно забираем сигнал: второй обработчик его не увидит
                    if not await asyncio.to_thread(self.repo.claim_pending_dm, uid):
                        continue
                    # ручная команда важнее автомата: возвращаем в игру даже
                    # приостановленного после серии сбоев пользователя
                    await asyncio.to_thread(
                        self.repo.update_dm_cursor, uid,
                        fail_count=0, last_error=None)
                    if uid in self._dm_manual_set:
                        continue
                    if not await asyncio.to_thread(self.repo.claim_dm_run, uid):
                        log.info("dm-now %s: выгрузка уже идёт — сигнал снят, "
                                 "повторный проход не запускаем", uid)
                        continue
                    log.info("dm-now %s: внеочередной проход по кнопке "
                             "«Выполнить сейчас»", uid)
                    await self._run_dm_sync(uid)
                    await human_delay(1.0, 2.0)
                await asyncio.sleep(15)  # частота опроса сигнала из админки

            try:
                ids = await asyncio.to_thread(self.repo.dm_fetch_user_ids)
            except Exception:  # noqa: BLE001
                log.exception("dm-loop: не удалось прочитать список dm_fetch")
                ids = []
            for uid in ids:
                if not self._running:
                    break
                # «выполнить сейчас» из админки уже занял этот диалог — пропускаем
                if uid in self._dm_manual_set:
                    continue
                # атомарно занимаем проход (v14): если status уже 'running' —
                # значит выгрузка идёт прямо сейчас, не лезем
                if not await asyncio.to_thread(self.repo.claim_dm_run, uid):
                    continue
                await self._run_dm_sync(uid)
                await human_delay(3.0, 6.0)  # пауза между пользователями
            await asyncio.sleep(getattr(self.s, "dm_poll_interval_sec", 300))

    async def request_dm_now(self, user_id: int) -> bool:
        """Немедленный проход ЛС по кнопке «▶ Выполнить сейчас» (админка).

        Возвращает False, если проход уже выполняется (dm-loop или предыдущий
        ручной запуск) — админка покажет «уже идёт» вместо второй выгрузки.
        Работает даже при приостановленном после серии сбоев пользователем:
        явная команда человека важнее автомата (счётчик сбоев сбрасывается).
        """
        prof = await asyncio.to_thread(self.repo.get_user, user_id)
        if prof is None or not prof.dm_fetch:
            return False
        if user_id in self._dm_manual_set:
            return False
        if not await asyncio.to_thread(self.repo.claim_dm_run, user_id):
            return False
        await asyncio.to_thread(
            self.repo.update_dm_cursor, user_id, fail_count=0, last_error=None)
        self._dm_manual_set.add(user_id)
        asyncio.create_task(
            self._manual_dm_pass(user_id), name=f"dm-now-{user_id}")
        return True

    async def _manual_dm_pass(self, user_id: int) -> None:
        try:
            added = await self._sync_dm_for(user_id)
            await asyncio.to_thread(
                self.repo.update_dm_cursor, user_id,
                last_error=None, fail_count=0, status="ok")
            log.info("dm-now %s: немедленный проход завершён (новых %d)",
                     user_id, added)
        except Exception as exc:  # noqa: BLE001
            log.warning("dm-now %s: сбой: %s", user_id, exc)
            await asyncio.to_thread(
                self.repo.update_dm_cursor, user_id,
                status="error", last_error=f"{type(exc).__name__}: {exc}"[:900])
        finally:
            self._dm_manual_set.discard(user_id)

    async def _run_dm_sync(self, uid: int) -> None:
        """Один проход dm-loop для пользователя + учёт статуса/диагностики."""
        try:
            await self._sync_dm_for(uid)
        except discord.Forbidden as exc:
            # Причина в discord.py-self — там Forbidden поднимается и
            # на 403, и на «нет канала/закрытый ЛС» (code 20003/50001).
            await self._dm_fail(
                uid,
                f"нет доступа к ЛС ({exc}); причины: закрытые ЛС от "
                "не-друзей / блокировка / диалог удалён")
        except Exception as exc:  # noqa: BLE001
            log.exception("dm-loop: сбой выгрузки ЛС для %s", uid)
            await self._dm_fail(uid, f"{type(exc).__name__}: {exc}")
        else:
            # успешный проход — очищаем диагностику, освобождаем статус (v14)
            await asyncio.to_thread(
                self.repo.update_dm_cursor, uid,
                last_error=None, fail_count=0, status="ok")

    async def _dm_fail(self, user_id: int, reason: str) -> None:
        """Записать сбой ЛС-цикла; после серии — приостановить пользователя."""
        limit = int(getattr(self.s, "dm_max_consecutive_fails", 5) or 5)
        prof = await asyncio.to_thread(self.repo.get_user, user_id)
        n = int(getattr(prof, "dm_fail_count", 0) or 0) + 1 if prof else 1
        msg = reason
        if n >= limit:
            msg = (f"{reason} · сбор ПРИОСТАНОВЛЕН после {n} сбоев подряд — "
                   "выключите и снова включите «💬 Собрать ЛС», когда ЛС откроются")
            log.warning("dm-loop %s: %s", user_id, msg)
        else:
            log.warning("dm-loop %s: нет доступа к ЛС (%d/%d): %s",
                        user_id, n, limit, reason)
        await asyncio.to_thread(
            self.repo.update_dm_cursor, user_id,
            last_error=msg[:900], fail_count=n, status="error")

    def _find_cached_dm(self, user_id: int):
        """Найти существующий DM-канал с пользователем в кэше клиента.

        Помогает, когда create_dm даёт 403 (закрытые ЛС), но канал уже есть
        (например, был открыт раньше) — историю читать всё равно можно.
        """
        try:
            for ch in getattr(self.client, "private_channels", []) or []:
                if type(ch).__name__ == "DMChannel" and getattr(ch, "recipient", None) is not None \
                        and ch.recipient.id == user_id:
                    return ch
        except Exception:  # noqa: BLE001
            log.debug("dm-loop %s: поиск DM в кэше не удался", user_id)
        return None

    async def _sync_dm_new(self, user_id: int, channel, after_id: int) -> int:
        """Инкрементальная докачка новых ЛС-сообщений (after=последний id).

        Возвращает число сохранённых новых сообщений. Ошибки сети/лимитов
        здесь не критичны: полный обратный проход всё равно дособерёт пропуск.
        """
        added = 0
        newest = after_id
        try:
            async for msg in channel.history(
                limit=self.s.fetch_limit, after=_to_snowflake(after_id), oldest_first=True
            ):
                saved = await asyncio.to_thread(self._handle_message_sync, msg, None)
                added += 1 if saved else 0
                newest = max(newest, msg.id)
        except Exception as exc:  # noqa: BLE001
            log.debug("dm-loop %s: инкремент пропущен (%s)", user_id, exc)
            return 0
        if added or newest != after_id:
            await asyncio.to_thread(
                self.repo.update_dm_cursor,
                user_id,
                channel_id=int(channel.id),
                newest_message_id=newest,
                synced=True,
            )
        return added

    async def _sync_dm_for(self, user_id: int) -> int:
        """Выгрузить историю ЛС с пользователем. Возвращает число новых сообщений.

        Канал ТОЛЬКО находится (get_channel/fetch_channel/GET /users/@me/
        channels), никогда не создаётся. Идём от новых к старым
        страницами history(before=курсор); курсор dm_last_message_id — самый
        старый загруженный id, поэтому рестарт продолжает без дублей.
        Обратная перемотка останавливается на начале истории или когда
        дошли до уже загруженной границы.
        """
        known_cid = None
        prof = await asyncio.to_thread(self.repo.get_user, user_id)
        if prof is not None and prof.dm_channel_id:
            known_cid = int(prof.dm_channel_id)

        # Только получение, никакого создания (open_dm/create_dm не вызываем):
        # get_channel -> fetch_channel(id) -> GET /users/@me/channels по
        # получателям. Если ни один путь не нашёл канал — ждём следующего
        # тика, курсоры не сбрасываем.
        channel, source = await self.client.resolve_dm_channel(known_cid, user_id)
        if channel is None:
            detail = f" (fetch_private_channels: {source})" if source else ""
            log.warning(
                "dm-loop %s: существующий ЛС-канал не найден%s — пропуск тика "
                "(создание диалога не выполняется). Укажи ID канала в карточке "
                "или открой диалог в клиенте Discord.",
                user_id, detail)
            return 0
        if known_cid and int(channel.id) != known_cid:
            log.info("dm-loop %s: канал по получателям %s (ожидался %s).",
                     user_id, int(channel.id), known_cid)

        before: int | None = None
        if known_cid == int(channel.id) and prof is not None and prof.dm_last_message_id:
            before = int(prof.dm_last_message_id)  # продолжаем от старого курсора
        elif (prof is not None and prof.dm_channel_id
              and int(prof.dm_channel_id) != int(channel.id)):
            # канал сменился — старые курсоры невалидны, начинаем заново
            await asyncio.to_thread(
                self.repo.update_dm_cursor, user_id, reset_cursor=True)
            prof = await asyncio.to_thread(self.repo.get_user, user_id)

        # Диалог уже выгружен до начала истории (есть курсор + newest id):
        # вместо дорогой полной перемотки — дешёвый инкремент новых сообщений.
        if (
            before is not None
            and prof is not None
            and prof.dm_first_message_id
            and getattr(self.s, "dm_full_rescan", False) is not True
        ):
            added_new = await self._sync_dm_new(user_id, channel, int(prof.dm_first_message_id))
            total = await asyncio.to_thread(self._count_dm_messages, int(channel.id))
            await asyncio.to_thread(
                self.repo.update_dm_cursor, user_id, messages_total=total, synced=True
            )
            return added_new
        newest_seen: int | None = None
        oldest_seen: int | None = None
        added_total = 0
        first_run = before is None

        while self._running:
            page: list[discord.Message] = []
            async for msg in channel.history(
                limit=50, before=_to_snowflake(before), oldest_first=False
            ):
                page.append(msg)
            if not page:
                break
            page.sort(key=lambda m: m.id)  # обрабатываем старые -> новые
            for m in page:
                saved = await asyncio.to_thread(self._handle_message_sync, m, None)
                added_total += 1 if saved else 0
            oldest_page = page[0].id
            newest_page = page[-1].id
            oldest_seen = min(oldest_seen or newest_page, oldest_page)
            newest_seen = max(newest_seen or oldest_page, newest_page)
            # фиксируем прогресс после каждой страницы — рестарт безопасен
            await asyncio.to_thread(
                self.repo.update_dm_cursor,
                user_id,
                channel_id=int(channel.id),
                oldest_message_id=oldest_page,
                newest_message_id=newest_seen,
                synced=True,
            )
            if len(page) < 50:
                break  # до самого начала истории
            if not first_run and oldest_page >= (before or 0):
                break  # курсор не сдвинулся — защита от зацикливания
            before = oldest_page
            await human_delay(self.s.request_delay_min, self.s.request_delay_max * 2)

        if added_total or first_run:
            total = await asyncio.to_thread(self._count_dm_messages, int(channel.id))
            await asyncio.to_thread(
                self.repo.update_dm_cursor, user_id, messages_total=total, synced=True
            )
        log.info(
            "dm-loop %s: канал %s, новых %d, oldest=%s",
            user_id, channel.id, added_total, oldest_seen,
        )
        return added_total

    def _count_dm_messages(self, channel_id: int) -> int:
        from sqlalchemy import func as sa_func
        from sqlalchemy import select as sa_select
        from app.models.entities import Message
        with self.repo._sf() as s:  # noqa: SLF001 — лёгкий счётчик по индексу
            return int(
                s.scalar(
                    sa_select(sa_func.count())
                    .select_from(Message)
                    .where(Message.channel_id == channel_id)
                )
                or 0
            )

    async def _refresh_loop(self) -> None:
        """Раз в PROFILE_REFRESH_DAYS (по умолчанию 14) пере-запрашивает
        профили всех известных пользователей: обновляются bio/бейджи/
        connections (в т.ч. Steam-логин), presence -> игры (user_games)."""
        days = max(0.01, float(getattr(self.s, "profile_refresh_days", 14)))
        while self._running:
            try:
                ids = await asyncio.to_thread(self.repo.all_user_ids)
                stale = [
                    uid for uid in ids
                    if await asyncio.to_thread(self.repo.profile_needs_refresh, uid, days)
                ]
                if stale:
                    log.info(
                        "REFRESH: %s профилей старше %.0f дн. — в очередь переопроса.",
                        len(stale), days,
                    )
                    for uid in stale:
                        self._profiled.discard(uid)  # разрешаем повторный опрос
                        self._queue_profile(uid, force=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("refresh loop: %s", exc)
            # проверка расписания раз в час; реальный фильтр — last_updated
            await asyncio.sleep(3600)


    # ------------------------------------------------------- profile worker
    async def _profile_worker(self) -> None:
        """Фоновое профайлирование: полный профиль + серверный Member + роли."""
        while self._running:
            try:
                user_id = await asyncio.wait_for(self._profile_queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            try:
                await self._profile_user(user_id)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка профилирования пользователя %s", user_id)
            finally:
                self._queued_profiles.discard(user_id)
                self._profile_queue.task_done()
            await human_delay(self.s.request_delay_min, self.s.request_delay_max)

    async def _profile_user(self, user_id: int) -> None:
        fields: dict[str, Any] = {}
        guild_fields: dict[str, Any] | None = None
        roles: list[dict[str, Any]] | None = None

        # 1) Полный пользовательский профиль (bio, badges, connections, mutual...)
        prof = await self.client.fetch_full_profile(user_id)
        if prof is not None:
            fields = mappers.user_fields(prof)
            raw_extra = {
                "primary_guild": _safe_attr(lambda: prof.primary_guild.name),
                "collectibles": _safe_json(lambda: list(prof.collectibles)),
            }
            merged = {k: v for k, v in raw_extra.items() if v}
            if merged:
                old_raw = fields.get("raw_json")
                base = _load_json(old_raw) if old_raw else {}
                base.update(merged)
                fields["raw_json"] = base

        # 2) Объект пользователя (username/avatar/created_at) — если профиль не пришёл
        if not fields:
            try:
                u = await self.client.fetch_user(user_id)
                fields = mappers.user_fields(u)
            except discord.DiscordException as exc:
                log.debug("fetch_user(%s): %s", user_id, exc)

        # 3) Серверный участник: nick/joined_at/роли/таймауты/права
        member = None
        rdefs: list[dict] = []
        state = self.repo.get_state(self.s.channel_id)
        gid = state.guild_id
        if gid is not None:
            member = await self.client.fetch_member_safe(gid, user_id)
            if member is not None:
                gf, rlist = mappers.guild_and_roles_from_member(member)
                rdefs = [mappers.role_def_from_role(r, member.guild.id)
                         for r in (getattr(member, "roles", None) or [])
                         if hasattr(r, "id") and r.id != member.guild.id]
                if gf:
                    guild_fields = gf
                roles = rlist or None
                # из Member можно достать и базовые поля быстрее, чем из profile
                for k, v in mappers.user_fields(member).items():
                    fields.setdefault(k, v)

        # 4) Игры/активности: из Member (presence) и из profile, если отдали.
        #    Steam-логин — из connections (сам Steam не трогаем).
        games: list[dict[str, Any]] = []
        if prof is not None:
            games.extend(mappers.games_from_activities(prof))
        steam_login = None
        if prof is not None:
            steam_login = mappers.steam_login_from(prof)
        if member is not None:
            for g in mappers.games_from_activities(member):
                if not any(g["name"].lower() == e["name"].lower() for e in games):
                    games.append(g)
            if steam_login is None:
                steam_login = mappers.steam_login_from(member)
        if steam_login and fields.get("connections_json"):
            pass  # логин уже внутри connections_json; отдельного поля не заводим

        # 5) Снимок профиля: био/кастомный статус/баннер/аватар/привязки —
        #    пишется в profile_snapshots только при фактическом изменении.
        snapshot: dict[str, Any] | None = None
        src = prof if prof is not None else member
        status_text = None
        if src is not None:
            for act in _safe_attr(lambda: list(src.activities or [])) or []:
                if str(getattr(act, "type", "")) == "ActivityType.custom_status" or \
                        getattr(act, "type", None) == discord.ActivityType.custom_status:
                    status_text = getattr(act, "state", None) or _safe_attr(
                        lambda a=act: a.emoji.name if getattr(a, "emoji", None) else None)
                    break
        import hashlib as _hl
        snap_about = fields.get("about")
        snap_conn = fields.get("connections_json")
        bhash = _hl.sha1((fields.get("banner_url") or "").encode()).hexdigest()[:16] \
            if fields.get("banner_url") else None
        ahash = _hl.sha1((fields.get("avatar_url") or "").encode()).hexdigest()[:16] \
            if fields.get("avatar_url") else None
        if any(v is not None for v in (snap_about, status_text, bhash, ahash, snap_conn)):
            snapshot = {
                "about": snap_about,
                "status_text": status_text,
                "banner_hash": bhash,
                "avatar_hash": ahash,
                "connections_json": snap_conn,
            }

        # 6) Локаль/таймзон — доступны только о себе через profile.metadata;
        #    сохраняем всё, что удалось получить.
        if fields or guild_fields:
            self.repo.upsert_user_profile(user_id, fields or {}, guild_fields, roles,
                                          snapshot=snapshot,
                                          role_defs=rdefs or None)
            # самолечение legacy-записей: старые версии маппера писали в
            # user_games kind='ActivityType.*' вместо lower('custom_status').
            # ВАЖНО: раньше здесь был purge с удалением kind IS NULL — он
            # стирал реальные игры (Discord не всегда отдаёт распознанный
            # enum типа активности). Теперь только нормализация kind.
            fixed = await asyncio.to_thread(self.repo.fix_legacy_game_kinds, user_id)
            if fixed:
                log.info("GAMES user %s: исправлено %s legacy-записей kind",
                         user_id, fixed)
            if games:
                added = self.repo.replace_user_games(user_id, games)
                if added:
                    log.info("GAMES user %s: +%s игр/активностей", user_id, added)
            else:
                # Games пустой: либо юзер офлайн (fetch_member отдаёт членов
                # без presence), либо у Member activities не пришли. Пробуем
                # вытащить из profile (он тоже может содержать activities).
                prof_games = mappers.games_from_activities(prof) if prof else []
                if prof_games:
                    added = self.repo.replace_user_games(user_id, prof_games)
                    if added:
                        log.info("GAMES user %s: +%s игр из profile", user_id, added)
            self._profiled.add(user_id)
            log.debug("Профиль сохранён: %s (+roles=%s)", user_id, len(roles or []))
            self._notify(
                "on_profile_saved",
                user_id,
                fields.get("username") or str(user_id),
                len(roles or []),
            )

    # ---------------------------------------------------- attachment worker
    async def _attachment_worker(self) -> None:
        """Фоновое скачивание картинок на диск (data/attachments/...)."""
        if not self.s.download_attachments:
            log.info(
                "Скачивание картинок ОТКЛЮЧЕНО (по умолчанию). Включается явным "
                "DOWNLOAD_ATTACHMENTS=1 в .env; сейчас сохраняются только метаданные вложений."
            )
            return
        while self._running:
            pending = self.repo.pending_image_attachments(limit=5)
            if not pending:
                await asyncio.sleep(10)
                continue
            for att in pending:
                if not self._running:
                    return
                try:
                    await self._download_attachment(att)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("Не удалось скачать вложение %s", att.id)
                await human_delay(self.s.request_delay_min, self.s.request_delay_max)

    async def _download_attachment(self, att) -> None:
        # путь: data/attachments/<message-shard>/<yyyy-mm>/<id>_<filename>
        dt = datetime.now(timezone.utc)
        subdir = os.path.join(self.s.attachments_dir, str(att.message_id // 1_000), f"{dt:%Y-%m}")
        os.makedirs(subdir, exist_ok=True)
        safe_name = "".join(c for c in att.filename if c.isalnum() or c in "._-")[:120] or f"{att.id}.bin"
        path = os.path.join(subdir, f"{att.id}_{safe_name}")
        if os.path.exists(path):
            self.repo.mark_attachment_downloaded(att.id, path, sha256_of_file(path))
            self._notify("on_attachment_saved", att.id, path, os.path.getsize(path))
            return

        # Скачиваем напрямую по сохранённому URL через CDN-транспорт библиотеки
        # (запрос идёт с теми же заголовками/сессией, что и у «браузера»).
        data: bytes | None = None
        try:
            data = await self.client.http.get_from_cdn(att.url)
        except discord.HTTPException as exc:
            log.debug("get_from_cdn(%s) failed: %s — пробуем proxy_url", att.id, exc)
            if att.proxy_url:
                try:
                    data = await self.client.http.get_from_cdn(att.proxy_url)
                except discord.HTTPException as exc2:
                    log.warning("Вложение %s недоступно: %s", att.id, exc2)
                    return
        if data is None:
            return
        with open(path, "wb") as f:
            f.write(data)
        self.repo.mark_attachment_downloaded(att.id, path, hashlib.sha256(data).hexdigest())
        log.debug("Скачано: %s (%s байт)", path, len(data))
        self._notify("on_attachment_saved", att.id, path, len(data))


# --------------------------------------------------------------------- helpers
def sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_json(s) -> dict:
    """JSONB возвращает dict напрямую; строка — для старых данных/заглушек."""
    import json

    if isinstance(s, dict):
        return s
    if not isinstance(s, str):
        return {}
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _safe_attr(fn):
    try:
        v = fn()
        return str(v) if v is not None and not isinstance(v, (int, float, bool, str)) else v
    except Exception:  # noqa: BLE001
        return None


def _safe_json(fn):
    """JSONB-колонки: возвращаем нативную Python-структуру (dict/list)."""
    try:
        return to_json(fn())
    except Exception:  # noqa: BLE001
        return None
