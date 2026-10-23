#!/usr/bin/env python3
"""Экспорт собранных данных в формат, подготовленный для нейросетевого профайлинга.

Тонкая CLI-обёртка: вся логика живёт в пакете app.services.profile_export
(разбит по слоям: env / db / dialog / stats / textclean / transcript /
dossier / prompt_api / cli). К Discord НЕ подключается, только PostgreSQL.

Для каждого пользователя формирует «дело» — структурированный JSON с:
  - профилем (bio, бейджи, connections, flags, аккаунт-метаданные);
  - серверными атрибутами (ник, роли/теги, дата вступления, таймауты);
  - статистикой коммуникации (частоты, длины, реакции + эмоциональные
    кластеры эмодзи, ответы, активность по часам/дням недели);
  - текстовой лентой сообщений (chronological transcript; лимит — ВЕС
    итогового файла промпта: EXPORT_PROMPT_MAX_KB в .env или --prompt-max-kb N
    (килобайты, 1 КБ = 1024 байта UTF-8); 0 = полная лента без среза);
  - медиа-сигналами (картинки: локальные пути/URL).

Выход:
  out/profiles/<user_id>_<username>.json   — полное дело одного пользователя
  out/prompts/<user_id>_<username>.md      — готовый промпт для LLM-профайлера
  out/dataset.jsonl                         — строка-на-пользователя (fine-tune/batch)
  out/index.json                            — сводка по всем пользователям

Запуск:
  python export_profiles.py [--db postgresql+psycopg://user:pass@host:port/db] [--out out]
        [пользователь] [--users id1,id2] [--min-messages 5] [--prompt-max-kb N]
        [--per-user-json] [--no-prompts]

Настройки по умолчанию читаются из .env (подгружается пакетом сам):
  EXPORT_PROMPT_MAX_KB=300   — максимальный вес ИТОГОВОГО файла промпта (.md)
                               в килобайтах; лента усекается по весу (остаются
                               самые СВЕЖИЕ целые строки). 0 = без лимита.
  EXPORT_CONTEXT_WINDOW=120  — окно временной связности диалога, сек.
Флаги командной строки переопределяют .env.

Программный API (для админки): from app.services.profile_export import
build_prompt_for_user — собрать дело+промпт одного пользователя из БД.
"""
from __future__ import annotations

import sys
from pathlib import Path

# запуск как скрипта из любого cwd: корень проекта должен быть в sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.services.profile_export.cli import main

if __name__ == "__main__":
    main()
