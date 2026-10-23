"""profile_export — экспорт «дел» пользователей Discord для LLM-профайлинга.

Модуль разбит из монолитного export_profiles.py по слоям:
  env.py        — загрузка .env и мелкие утилиты (json/datetime/row);
  db.py         — read-only доступ к PostgreSQL (ReadOnlyDb, fetch_*);
  dialog.py     — reply-цепочки и сборка диалога с собеседниками;
  stats.py      — статистика коммуникации + эмоциональные кластеры эмодзи;
  textclean.py  — redact PII, sanitize, вырезание URL;
  transcript.py — хронологическая лента сообщений (обрезка по ВЕСУ);
  dossier.py    — сборка «дела», компактный вариант и рендеринг промпта;
  prompt_api.py — программный API build_prompt_for_user() (для админки);
  cli.py        — аргументы и main() пакетной выгрузки в out/.

Точка входа CLI — корневой скрипт export_profiles.py (тонкая обёртка).
"""
from __future__ import annotations

from .env import load_env_file
from .db import ReadOnlyDb, open_db
from .dialog import CONTEXT_WINDOW_SEC, build_dialog, rebuild_links
from .stats import analyze_messages, analyze_reactions, classify_reaction, EMOTION_EMOJI
from .textclean import redact, sanitize, strip_urls_deep, strip_urls_text
from .transcript import DEFAULT_PROMPT_MAX_KB, make_transcript
from .dossier import PROFILE_SYSTEM_PROMPT, build_dossier, render_prompt_md
from .prompt_api import build_prompt_for_user
from .cli import parse_args, parse_bound, main

load_env_file()

__all__ = [
    "load_env_file", "ReadOnlyDb", "open_db",
    "CONTEXT_WINDOW_SEC", "build_dialog", "rebuild_links",
    "analyze_messages", "analyze_reactions", "classify_reaction", "EMOTION_EMOJI",
    "redact", "sanitize", "strip_urls_deep", "strip_urls_text",
    "DEFAULT_PROMPT_MAX_KB", "make_transcript",
    "PROFILE_SYSTEM_PROMPT", "build_dossier", "render_prompt_md",
    "build_prompt_for_user",
    "parse_args", "parse_bound", "main",
]
