#!/usr/bin/env python3
"""Диагностика user_roles: почему столько строк и что с ними не так.

Отвечает на вопросы:
  * сколько @everyone-мусора (role_id == guild_id);
  * распределение ролей по серверам;
  * есть ли «общесерверные» роли, которые носит каждый (боты, верификация) —
    они легальны, но для профилирования малополезны;
  * топ ролей по числу носителей.

Запуск: venv/bin/python scripts/roles_report.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.models.db import create_db_engine, default_database_url, require_postgres  # noqa: E402


def main() -> int:
    engine = create_db_engine(require_postgres(default_database_url()))
    with engine.begin() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM user_roles")).scalar()
        everyone = conn.execute(
            text("SELECT COUNT(*) FROM user_roles WHERE role_id = guild_id")).scalar()
        users = conn.execute(
            text("SELECT COUNT(DISTINCT user_id) FROM user_profiles")).scalar() or 1
        print(f"Всего строк: {total}")
        print(f"  из них @everyone (мусор): {everyone}")
        print(f"  настоящих ролей: {total - everyone}")
        print(f"  профилей пользователей: {users}")
        if total - everyone:
            avg = (total - everyone) / users
            print(f"  в среднем реальных ролей на пользователя: {avg:.1f} "
                  f"({'норма' if avg <= 5 else 'многовато — проверь ниже'})")

        print("\nПо серверам:")
        for gid, c in conn.execute(text(
                "SELECT guild_id, COUNT(*) FROM user_roles GROUP BY guild_id ORDER BY 2 DESC")):
            print(f"  guild {gid}: {c} строк")

        print("\nРоли, которые носит почти каждый профиль (>80% пользователей) — "
              "кандидаты на исключение из анализа (не удалять, просто знать):")
        rows = conn.execute(text("""
            SELECT r.role_name, r.guild_id, COUNT(DISTINCT r.user_id) c
            FROM user_roles r GROUP BY r.role_name, r.guild_id HAVING COUNT(DISTINCT r.user_id) >= 1
            ORDER BY c DESC LIMIT 15""")).all()
        for name, gid, c in rows:
            share = c / users * 100
            flag = "  <- массовая" if share > 80 else ""
            print(f"  {c:>4} ({share:5.1f}%)  guild={gid}  {name}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
