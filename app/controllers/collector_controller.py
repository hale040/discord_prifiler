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
        if msg is None:
            return
        try:
            if int(getattr(getattr(msg, "channel", None), "id", 0) or 0) != self.settings.channel_id:
                return
        except (TypeError, ValueError):
            return
        try:
            if action == "MESSAGE_CREATE":
                state = self.repo.get_state(self.settings.channel_id)
                saved = await self.collector.handle_live_message(msg, state.guild_id)
                if saved:
                    self.repo.update_state(self.settings.channel_id, msg.id, 1, state.guild_id)
                    self.view.on_new_messages(1, msg.id)
            elif action == "MESSAGE_EDIT":
                await self.collector.handle_live_edit(msg, payload.get("before"))
            elif action == "MESSAGE_DELETE":
                await self.collector.handle_live_delete(msg)
        except Exception as exc:  # noqa: BLE001
            log.debug("live %s обработан с ошибкой: %s", action, exc)

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
        """Точка входа контроллера: крутим клиент до отмены/ошибки."""
        try:
            await self.client.start()
        except asyncio.CancelledError:
            raise
        finally:
            await self.shutdown()

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
