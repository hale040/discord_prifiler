"""Конфигурация сервиса профилирования Discord."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_int(name: str, default: int) -> int:
    """Читает int из env; пустое/битое значение -> default (проверка в load_settings)."""
    raw = os.getenv(name, "")
    try:
        return int(raw) if raw.strip() else default
    except ValueError:
        return default


@dataclass(slots=True)
class Settings:
    """Все настройки загружаются из переменных окружения (см. .env.example)."""

    # --- Подключение (user-token, НЕ bot) ---
    discord_token: str = field(default_factory=lambda: os.getenv("DISCORD_TOKEN", ""))

    # Целевой канал для сбора данных (id текстового канала/треда)
    channel_id: int = field(default_factory=lambda: _env_int("CHANNEL_ID", 0))

    # Периодичность опроса канала, сек
    poll_interval_sec: int = field(default_factory=lambda: _env_int("POLL_INTERVAL_SEC", 300))

    # Максимум сообщений за один проход history-запроса
    fetch_limit: int = field(default_factory=lambda: _env_int("FETCH_LIMIT", 100))

    # Задержка между запросами к API, чтобы имитировать поведение живого
    # пользователя (human-like pacing). Случайная величина в диапазоне.
    request_delay_min: float = field(default_factory=lambda: float(os.getenv("REQUEST_DELAY_MIN", "2.5") or 2.5))
    request_delay_max: float = field(default_factory=lambda: float(os.getenv("REQUEST_DELAY_MAX", "7.0") or 7.0))

    # Сохранять вложения (картинки) на диск
    download_attachments: bool = os.getenv("DOWNLOAD_ATTACHMENTS", "1") == "1"
    attachments_dir: str = os.getenv("ATTACHMENTS_DIR", "data/attachments")

    # База данных
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///data/profiling.db")

    # Синхронный URL для SQLAlchemy (driver переключается в db.py)
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


def load_settings() -> Settings:
    s = Settings()
    if not s.discord_token:
        raise RuntimeError(
            "DISCORD_TOKEN не задан. Нужен user-token аккаунта от имени которого идёт сбор."
        )
    if not s.channel_id:
        raise RuntimeError("CHANNEL_ID не задан — укажите id целевого канала.")
    os.makedirs(s.attachments_dir, exist_ok=True)
    os.makedirs("data", exist_ok=True)
    return s
