"""Controller-слой: связывает Model (Repository), Service (Discord+Collection) и View.

Контроллер:
  * строит зависимости;
  * подписывается на события клиента (on_ready / gateway-события);
  * запускает/останавливает периодический сбор;
  * проксирует прогресс в консоль-вью.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import Settings
from app.models.db import init_db
from app.models.repository import Repository
from app.services.collection_service import CollectionService
from app.services.discord_gateway import UserbotClient
from app.views.console_view import ConsoleView

log = logging.getLogger("controller")


class CollectorController:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.view = ConsoleView()

        # Model
        session_factory = init_db(settings)
        log.info("Схема БД проверена/обновлена (Alembic head). Старт клиента…")
        self.repo = Repository(session_factory)

        # Service (Discord user-client)
        self.client = UserbotClient(
            settings.discord_token,
            gateway_callback=self.on_gateway_event,
            ready_callback=self.on_client_ready,
        )

        # Service (collection logic)
        self.collector = CollectionService(settings, self.repo, self.client)
        self.collector.view = self.view  # сервис может докладывать о прогрессе

        if settings.start_date is not None:
            self.view.on_start_date(settings.start_date)
        else:
            self.view.on_start_date(None)

        self._started = asyncio.Event()
        self._stats_task: asyncio.Task | None = None

    # ----------------------------------------------------------- events in
    async def on_client_ready(self, client: UserbotClient) -> None:
        me = client.user
        self.view.on_ready(getattr(me, "name", "?"), getattr(me, "id", 0))
        try:
            await self.collector.start()
        except Exception as exc:  # noqa: BLE001
            self.view.on_error("collector.start", exc)
            raise
        self._started.set()
        if self._stats_task is None:
            self._stats_task = asyncio.create_task(self._stats_loop(), name="stats-loop")

    async def on_gateway_event(self, action: str, payload: dict[str, Any]) -> None:
        """Live-события Discord — усиление поллера.

        Основной источник данных — периодический history-запрос; live-события
        используются, чтобы не ждать следующего интервала и фиксировать
        редактирования/удаления сообщений.
        Клиент (UserbotClient) передаёт готовые объекты в ``payload["message"]``.
        """
        msg = payload.get("message")
        # --- события без объекта сообщения (реакции/presence): v10 ---------
        if msg is None:
            try:
                if action in ("REACTION_ADD", "REACTION_REMOVE"):
                    mid = payload.get("message_id")
                    chid = payload.get("channel_id")
                    if not mid:
                        return
                    # реакции пишутся по всем сообщениям ленты — это лента
                    # взаимодействий; filter по каналу только если известен
                    if chid and int(chid) != self.settings.channel_id:
                        return
                    await asyncio.to_thread(
                        self.repo.record_reaction_event,
                        int(mid),
                        payload.get("user_id"),
                        payload.get("emoji") or "",
                        "add" if action == "REACTION_ADD" else "remove",
                    )
                elif action == "PRESENCE_UPDATE":
                    uid = payload.get("user_id")
                    if uid:
                        await asyncio.to_thread(
                            self.repo.record_presence, [dict(payload, user_id=int(uid))]
                        )
            except Exception as exc:  # noqa: BLE001
                log.debug("live %s обработан с ошибкой: %s", action, exc)
            return
        # --- фильтр «отслеживать»: собираем только целевой канал (.env) и
        # чаты с chats.tracked=TRUE, отмеченные оператором в админке ------
        try:
            chid = int(getattr(getattr(msg, "channel", None), "id", 0) or 0)
        except (TypeError, ValueError):
            return
        if not self._is_tracked_channel(chid):
            return
        try:
            if action == "MESSAGE_CREATE":
                state = self.repo.get_state(self.settings.channel_id)
                saved = await self.collector.handle_live_message(msg, state.guild_id)
                if saved:
                    if chid == self.settings.channel_id:
                        self.repo.update_state(self.settings.channel_id, msg.id, 1, state.guild_id)
                    self.view.on_new_messages(1, msg.id)
            elif action == "MESSAGE_EDIT":
                await self.collector.handle_live_edit(msg, payload.get("before"))
            elif action == "MESSAGE_DELETE":
                await self.collector.handle_live_delete(msg)
        except Exception as exc:  # noqa: BLE001
            log.debug("live %s обработан с ошибкой: %s", action, exc)

    # ------------------------------------------------------ tracked-фильтр
    def _is_tracked_channel(self, channel_id: int) -> bool:
        """Собираем live-сообщения только целевого канала (.env) и чатов с
        chats.tracked=TRUE (галка «отслеживать» в админке).

        Кэш на 30 секунд — чтобы не дёргать БД на каждое сообщение; галка из
        админки начинает действовать в течение полуминуты. Если таблицы chats
        ещё нет (старая схема) — поведение как раньше: только .env-канал.
        """
        if channel_id == self.settings.channel_id:
            return True
        now = asyncio.get_event_loop().time()
        cache = getattr(self, "_tracked_cache", None)
        if cache is None or now - cache[0] > 30:
            try:
                ids = set(self.repo.tracked_channel_ids())
            except Exception:  # noqa: BLE001 — старая схема без chats/tracked
                ids = set()
            self._tracked_cache = (now, ids)
            cache = self._tracked_cache
        return channel_id in cache[1]

    # ------------------------------------------------------------ run loop
    async def _stats_loop(self) -> None:
        """Периодически показываем сводку по собранному + достраиваем граф ответов."""
        while True:
            await asyncio.sleep(60)
            try:
                # связи «кто кому отвечает» достраиваются из уже сохранённых
                # reply-ссылок (идемпотентно: обрабатываются только незакрытые строки)
                linked = await asyncio.to_thread(self.repo.link_reply_references)
                if linked:
                    log.info("Достроено связей ответов: %s", linked)
                self.view.on_stats(self.repo.stats())
            except Exception as exc:  # noqa: BLE001
                log.debug("stats failed: %s", exc)

    async def run(self) -> None:
        """Точка входа контроллера: крутим клиент до отмены.

        Устойчивость к обрывам связи (требование «сборщик не должен умирать
        при потере соединения»):
          * штатный путь — keep_alive/restart_on_disconnect внутри
            discord.Client: WS-обрыв переподключается самой библиотекой,
            .start() не завершается;
          * если всё же вылетело (fatal gateway error, исчерпан reconnect
            backoff, сетевой сбой на handshake, ошибка в on_ready) — НЕ
            выходим: чиним клиента и стартуем заново с экспоненциальным
            бэкоффом (10s → 2× → cap 300s). Останавливаемся только по
            CancelledError (сигнал SIGINT/SIGTERM обрабатывается в main.py).
        Логика уровня процесса (перезапуск под systemd/supervisor) остаётся
        запасной — но теперь она не единственная защита.

        ВАЖНО: finally c shutdown() убран намеренно. В старой версии он делал
        run() непригодным для переподключения: shutdown() отменял _stats_task
        и закрывал collector, а повторный on_ready их не восстанавливал —
        после первого же обрыва сбор замолкал, процесс «жил», но ничего не
        собирал. Теперь очистка — обязанность main.py (сигнал остановки) и
        ветки LoginFailure выше по стеку.
        """
        attempt = 0
        while True:
            try:
                await self.client.start()
                # start() вернулся без исключения (нетипично) — считаем это
                # потерей коннекта и переподключаемся, а не завершаем процесс.
                log.warning("client.start() завершился без ошибки — переподключение")
            except asyncio.CancelledError:
                # осмысленная остановка процесса: чинимся перед выходом
                await self.shutdown()
                raise
            except Exception as exc:  # noqa: BLE001 — тут любые сетевые/fatal
                attempt += 1
                delay = min(10 * 2 ** (attempt - 1), 300)
                log.warning(
                    "Клиент упал (попытка %s): %r — переподключение через %ss",
                    attempt, exc, delay,
                )
            else:
                delay = min(10 * 2 ** attempt, 300) if attempt else 10
            try:
                if not self.client.is_closed():
                    await self.client.close()
            except Exception:  # noqa: BLE001
                pass
            # discord.Client нельзя повторно запустить после close():
            # пересоздаём клиента, сохраняя коллбэки контроллера.
            self.client = UserbotClient(
                self.settings.discord_token,
                gateway_callback=self.on_gateway_event,
                ready_callback=self.on_client_ready,
            )
            await asyncio.sleep(delay)
            # цикл сбора пересоздаётся штатно в on_client_ready при
            # следующем ready (он идемпотентен).

    async def shutdown(self) -> None:
        if self._stats_task:
            self._stats_task.cancel()
        await self.collector.stop()
        try:
            await self.client.close()
        except Exception:  # noqa: BLE001
            pass
        self.view.on_stats(self.repo.stats())
        log.info("Остановлено.")
