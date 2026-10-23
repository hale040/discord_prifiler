"""app.services.profile_export.env — часть модуля экспорта «дел» для LLM.

Автономный экспорт из PostgreSQL (SQLAlchemy + stdlib), без Discord.
Сгенерирован разделением export_profiles.py; публичный CLI — export_profiles.py.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def load_env_file(path: str = ".env") -> None:
    """Минималистичный загрузчик .env (stdlib, без зависимостей).

    Значения из самого файла НЕ перекрывают переменные окружения процесса.
    Используется для EXPORT_* настроек выгрузки (например EXPORT_MAX_LINES).
    """
    try:
        p = Path(path)
        if not p.is_file():
            return
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.split(" #", 1)[0].strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass


def load_json(raw):
    """Разбор JSON-колонки. Колонки *_json — PostgreSQL JSONB, драйвер
    возвращает уже разобранные list/dict; строковые значения поддержаны для
    совместимости со старыми дампами."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


def iso(dt):
    """Нормализует datetime/строку в ISO. SQLite может вернуть строку без tz."""
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.isoformat()


def parse_ts(value) -> datetime:
    """datetime/datetime.date (PostgreSQL) или ISO-строка -> aware datetime (UTC)."""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def row_get(row: dict, key: str, default=None):
    """Безопасное чтение колонки: старые/урезанные БД могут не иметь части полей."""
    return row.get(key, default) if isinstance(row, dict) else default


def _env_int(name: str, default: int) -> int:
    """Чтение целого из ENV (.env уже подгружен); некорректное значение -> default."""
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except ValueError:
        return default
