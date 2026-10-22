"""Конфигурация сервиса профилирования Discord."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _env_int(name: str, default: int) -> int:
    """Читает int из env; пустое/битое значение -> default (проверка в load_settings)."""
    raw = os.getenv(name, "")
    try:
        return int(raw) if raw.strip() else default
    except ValueError:
        return default


def _parse_since(raw: str) -> datetime | None:
    """Парсит START_DATE / SINCE: '2024-01-01', '2024-01-01 12:30' или ISO8601.

    Naive-датеты трактуются как UTC. Пустое значение -> None (без ограничения).
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("T", " ").replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(
            f"START_DATE={raw!r} не разбирается. Формат: ГГГГ-ММ-ДД[ ЧЧ:ММ[:СС]] или ISO8601."
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(slots=True)
class Settings:
    """Все настройки загружаются из переменных окружения (см. .env.example)."""

    # --- Подключение (user-token, НЕ bot) ---
    discord_token: str = field(default_factory=lambda: os.getenv("DISCORD_TOKEN", ""))

    # Целевой канал для сбора данных (id текстового канала/треда)
    channel_id: int = field(default_factory=lambda: _env_int("CHANNEL_ID", 0))

    # Периодичность опроса канала, сек
    poll_interval_sec: int = field(default_factory=lambda: _env_int("POLL_INTERVAL_SEC", 300))
    # Сохранять полный сырой JSON сообщений в messages.raw_json (диск).
    # По умолчанию ВЫКЛЮЧЕНО: все нужные поля уже смаплены в колонки, а
    # raw_json раздувал базу в разы. Включить: STORE_RAW_JSON=1.
    store_raw_json: bool = field(
        default_factory=lambda: os.getenv("STORE_RAW_JSON", "").strip().lower()
        in {"1", "true", "yes", "on"}
    )

    # Максимум сообщений за один проход history-запроса
    fetch_limit: int = field(default_factory=lambda: _env_int("FETCH_LIMIT", 100))

    # Дата начала сбора (UTC). Сообщения старше неё не парсятся и не сохраняются.
    # Формат: '2024-01-01' или '2024-01-01 12:30[:SS]' или ISO8601 с зоной.
    # Naive-дата трактуется как UTC. Пусто / 0 / 'all' -> собирать всю историю.
    start_date: datetime | None = field(
        default_factory=lambda: _parse_since(
            "" if os.getenv("START_DATE", "").strip() in {"0", "all", "none"}
            else os.getenv("START_DATE", "")
        )
    )

    # Задержка между запросами к API, чтобы имитировать поведение живого
    # пользователя (human-like pacing). Случайная величина в диапазоне.
    request_delay_min: float = field(default_factory=lambda: float(os.getenv("REQUEST_DELAY_MIN", "2.5") or 2.5))
    request_delay_max: float = field(default_factory=lambda: float(os.getenv("REQUEST_DELAY_MAX", "7.0") or 7.0))

    # Сохранять вложения (картинки) на диск.
    # ВНИМАНИЕ: скачивание включается ТОЛЬКО явным DOWNLOAD_ATTACHMENTS=1 в .env.
    # Без этой строки (или при 0/false/no) файлы не загружаются — в БД пишутся
    # только метаданные (url, sha256, размер), чего достаточно для анализа текста.
    download_attachments: bool = field(
        default_factory=lambda: os.getenv("DOWNLOAD_ATTACHMENTS", "").strip().lower() in {"1", "true", "yes", "on"}
    )
    attachments_dir: str = os.getenv("ATTACHMENTS_DIR", "data/attachments")

    # Рефреш профилей: раз в N дней пере-запрашивать профили всех известных
    # пользователей (bio, бейджи, connections/Steam-логин, presence -> игры).
    profile_refresh_days: float = field(
        default_factory=lambda: float(os.getenv("PROFILE_REFRESH_DAYS", "14") or 14)
    )

    # База данных
    database_url: str = os.getenv(
        "DATABASE_URL",
        # PostgreSQL — единственная поддерживаемая СУБД (см. app/models/db.py)
        "postgresql+psycopg://profiler:profiler@localhost:5682/profiling",
    )

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
    if s.download_attachments:
        # каталог под картинки создаём только когда скачивание явно включено
        os.makedirs(s.attachments_dir, exist_ok=True)
    os.makedirs("data", exist_ok=True)
    return s
