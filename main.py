"""Точка входа: сервис-процесс сбора данных из Discord-чата (профайлинг).

Запуск:
    python main.py

Конфигурация — через переменные окружения (см. .env.example):
    DISCORD_TOKEN   — user-token аккаунта, от имени которого идёт сбор
                      (НЕ bot-token; использование user-token'а нарушает ToS Discord)
    CHANNEL_ID      — id целевого текстового канала

Бот работает как долгоживущий процесс: периодически опрашивает канал
(history-запросами, имитирующими скролл живым пользователем), догружает
всю историю, собирает профили/теги/роли участников и скачивает картинки.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

# python-dotenv опционален: если стоит — подтягиваем .env
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

from app.config import load_settings
from app.controllers.collector_controller import CollectorController
from app.views.console_view import setup_logging

log = logging.getLogger("main")


async def run() -> int:
    try:
        settings = load_settings()
    except RuntimeError as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 2

    setup_logging(settings.log_level)
    controller = CollectorController(settings)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop(sig_name: str) -> None:
        log.info("Получен сигнал %s — корректное завершение…", sig_name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop, sig.name)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda s, f: _request_stop(signal.Signals(s).name))

    client_task = asyncio.create_task(controller.run(), name="discord-client")
    stop_task = asyncio.create_task(stop_event.wait(), name="stop-watcher")

    done, _pending = await asyncio.wait(
        {client_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
    )

    if stop_task in done and client_task not in done:
        # сигнал остановки: закрываем клиента (его finally вызовет shutdown)
        await controller.client.close()
        try:
            await asyncio.wait_for(client_task, timeout=15)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            client_task.cancel()
        return 0

    # клиент завершился сам (например, LoginFailure / fatal gateway error)
    exc = client_task.exception()
    if exc is not None:
        log.error("Процесс завершился с ошибкой: %r", exc)
        await controller.shutdown()
        return 1
    return 0


def main() -> None:
    try:
        code = asyncio.run(run())
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
