"""View-слой: консольное представление состояния сервиса.

Никакой бизнес-логики — только форматированный вывод того, что контроллер
ему передаёт (статистика БД, события сбора, ошибки).
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone


def setup_logging(level: str = "INFO") -> None:
    fmt = "%(asctime)s | %(levelname)-7s | %(name)-16s | %(message)s"
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt, "%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # discord.py-self слишком разговорчив на DEBUG-уровне в INFO не нужен шум
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)


class ConsoleView:
    """Печатает прогресс профайлинга в консоль."""

    def on_ready(self, who: str, user_id: int) -> None:
        self._line("READY", f"Вход выполнен от имени пользователя: {who} (id={user_id})")

    def on_backfill_page(self, page: int, saved: int, total: int, cursor: int) -> None:
        self._line("HISTORY", f"страница #{page}: сохранено {saved}/{total}, oldest cursor={cursor}")

    def on_new_messages(self, count: int, cursor: int | None) -> None:
        if count:
            self._line("POLL", f"+{count} новых сообщений (курсор={cursor})")
        else:
            self._line("POLL", "новых сообщений нет")

    def on_profile_saved(self, user_id: int, username: str, roles: int) -> None:
        self._line("PROFILE", f"user {username} ({user_id}) сохранён, ролей/тегов: {roles}")

    def on_attachment_saved(self, att_id: int, path: str, size: int) -> None:
        self._line("MEDIA", f"вложение {att_id} -> {path} ({size} байт)")

    def on_stats(self, stats: dict[str, int]) -> None:
        parts = [f"{k}={v}" for k, v in stats.items()]
        self._line("STATS", " | ".join(parts))

    def on_error(self, where: str, exc: BaseException) -> None:
        self._line("ERROR", f"{where}: {exc!r}", level=logging.ERROR)

    @staticmethod
    def _line(tag: str, msg: str, level: int = logging.INFO) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        logging.getLogger("view").log(level, "[%s] %s %s", ts, tag, msg)
