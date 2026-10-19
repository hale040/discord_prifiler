"""Утилиты: human-like задержки, JSON-сериализация."""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("utils")


async def human_delay(min_sec: float, max_sec: float) -> None:
    """Случайная пауза — имитация человека, читающего чат.

    Плюс с небольшой вероятностью «задумывается надолго» (до 3x max),
    что типично для реального пользователя.
    """
    delay = random.uniform(min_sec, max_sec)
    if random.random() < 0.08:
        delay *= random.uniform(1.5, 3.0)
    await asyncio.sleep(delay)


class DiscordJSONEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, datetime):
            return o.isoformat()
        if hasattr(o, "_json"):  # discord.py RAW models / enums
            try:
                return o._json
            except Exception:
                pass
        if isinstance(o, bytes):
            return o.decode("utf-8", errors="replace")
        return str(o)


def to_json(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, cls=DiscordJSONEncoder, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        log.debug("to_json failed: %s", exc)
        return None


def snowflake_time(snowflake: int) -> datetime:
    """Время создания объекта Discord из snowflake-id (для дат авторства)."""
    return datetime.fromtimestamp(((snowflake >> 22) + 1420070400000) / 1000, tz=timezone.utc)
