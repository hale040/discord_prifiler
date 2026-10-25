#!/usr/bin/env python3
"""Разовая чистка user_roles от @everyone-мусора (запуск вручную).

Симптом: в user_roles на каждого пользователя попадала строка роли @everyone
(role_id == guild_id) — discord.Member.roles всегда включает её. Маппер уже
исправлен (services/mappers.py); этот скрипт убирает накопленные строки и
показывает статистику ДО/ПОСЛЕ. DATABASE_URL читается из .env (как в остальных
CLI-скриптах проекта).

Использование:
    venv/bin/python scripts/cleanup_everyone_roles.py            # dry-run (отчёт)
    venv/bin/python scripts/cleanup_everyone_roles.py --apply    # удалить

Скрипт идемпотентен: повторный запуск ничего лишнего не удалит.
(Аналогичная чистка входит в alembic-ревизию a00000000003 — если миграция
уже применена при старте main.py, отдельно запускать скрипт не нужно.)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.models.db import create_db_engine, default_database_url, require_postgres  # noqa: E402


def main() -> int:
    apply_changes = "--apply" in sys.argv[1:]
    engine = create_db_engine(require_postgres(default_database_url()))

    with engine.begin() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM user_roles")).scalar()
        junk = conn.execute(
            text("SELECT COUNT(*) FROM user_roles WHERE role_id = guild_id")
        ).scalar()
        users_affected = conn.execute(
            text("SELECT COUNT(DISTINCT user_id) FROM user_roles WHERE role_id = guild_id")
        ).scalar()
        print(f"ДО:        строк в user_roles: {total}")
        print(f"@everyone: {junk} строк у {users_affected} пользователей")

        if not junk:
            print("Мусора нет — нечего чистить.")
            return 0

        if not apply_changes:
            print(f"\nDRY-RUN. Для удаления запусти: {sys.argv[0]} --apply")
            return 0

        res = conn.execute(text("DELETE FROM user_roles WHERE role_id = guild_id"))
        left = conn.execute(text("SELECT COUNT(*) FROM user_roles")).scalar()
        print(f"УДАЛЕНО:   {res.rowcount}")
        print(f"ПОСЛЕ:     строк в user_roles: {left}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
