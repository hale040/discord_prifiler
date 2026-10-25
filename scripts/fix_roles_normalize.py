#!/usr/bin/env python3
"""Ручная нормализация ролей (миграция a00000000004 в виде скрипта).

Делает то же, что ревизия, но запускается руками и показывает ДО/ПОСЛЕ:
  1) переносит уникальные (guild_id, role_id) -> name из user_roles в roles;
  2) удаляет колонку user_roles.role_name.

Использование:
    python scripts/fix_roles_normalize.py           # dry-run (только отчёт)
    python scripts/fix_roles_normalize.py --apply   # применить

Идемпотентен: повторный запуск ничего не ломает. Если миграция уже
применена при старте приложения — скрипт лишь подтвердит состояние.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    apply = "--apply" in sys.argv[1:]
    from dotenv import load_dotenv
    load_dotenv()
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL не задан (.env)"); return 2
    import psycopg

    dsn = url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.user_roles'), to_regclass('public.roles')")
        ur_t, roles_t = cur.fetchone()
        if not ur_t:
            print("таблицы user_roles нет — нечего нормализовать"); return 0
        cur.execute("""SELECT column_name FROM information_schema.columns
                        WHERE table_name='user_roles' AND column_name='role_name'""")
        has_name = cur.fetchone() is not None
        cur.execute("SELECT COUNT(*) FROM user_roles")
        links = cur.fetchone()[0]
        print(f"ДО: user_roles={links} строк, колонка role_name={'есть' if has_name else 'НЕТ'},"
              f" таблица roles={'есть' if roles_t else 'нет'}")
        if not has_name and roles_t:
            cur.execute("SELECT COUNT(*) FROM roles")
            n = cur.fetchone()[0]
            print(f"УЖЕ НОРМАЛИЗОВАНО: справочник roles содержит {n} ролей.")
            return 0
        if not apply:
            print("DRY-RUN: ничего не изменено. Запусти с --apply для применения:")
            print("  python scripts/fix_roles_normalize.py --apply")
            return 0
        cur.execute("""CREATE TABLE IF NOT EXISTS roles (
               id BIGSERIAL PRIMARY KEY,
               guild_id BIGINT NOT NULL,
               role_id BIGINT NOT NULL,
               name VARCHAR(256) NOT NULL DEFAULT '',
               position INTEGER NOT NULL DEFAULT 0,
               color INTEGER,
               is_mentionable BOOLEAN,
               permissions_raw VARCHAR(128),
               first_seen TIMESTAMPTZ,
               last_seen TIMESTAMPTZ,
               CONSTRAINT uq_role_guild UNIQUE (guild_id, role_id))""")
        if has_name:
            cur.execute("""INSERT INTO roles (guild_id, role_id, name, position, color,
                                              is_mentionable, permissions_raw, first_seen, last_seen)
                           SELECT DISTINCT ON (ur.guild_id, ur.role_id)
                                  ur.guild_id, ur.role_id, ur.role_name, ur.position, ur.color,
                                  ur.is_mentionable, ur.permissions_raw, f.first_seen, l.last_seen
                             FROM user_roles ur
                             JOIN LATERAL (SELECT MIN(first_seen) AS first_seen FROM user_roles x
                                            WHERE x.guild_id=ur.guild_id AND x.role_id=ur.role_id) f ON TRUE
                             JOIN LATERAL (SELECT MAX(last_seen) AS last_seen FROM user_roles x
                                            WHERE x.guild_id=ur.guild_id AND x.role_id=ur.role_id) l ON TRUE
                            ORDER BY ur.guild_id, ur.role_id, ur.last_seen DESC
                           ON CONFLICT (guild_id, role_id) DO NOTHING""")
            print(f"справочник roles: добавлено {cur.rowcount} ролей")
            cur.execute("ALTER TABLE user_roles DROP COLUMN IF EXISTS role_name")
            print("user_roles.role_name удалена")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_user_roles_guild_role ON user_roles (guild_id, role_id)")
        cur.execute("SELECT COUNT(*) FROM roles")
        nr = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM user_roles")
        nl = cur.fetchone()[0]
        print(f"ПОСЛЕ: roles={nr} (справочник), user_roles={nl} (чистые связи)")
        print("ОБНОВИ alembic_version на a00000000004, если база ещё не стемплена:"
              " alembic stamp head")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
