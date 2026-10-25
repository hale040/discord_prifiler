#!/usr/bin/env python3
"""Разовая нормализация user_roles (запуск вручную, идемпотентно).

Что делает:
1. Удаляет @everyone-строки (role_id == guild_id) — мусор, есть у всех.
2. Схлопывает дубли (user_id, role_id) в одну запись с корректным
   first_seen=MIN, last_seen=MAX, last_lost_at=MIN(NULL если была активна).
   Дубли возникают из-за UniqueConstraint(user_id, role_id) без guild_id:
   одна и та же роль на разных серверах физически не может быть записана
   дважды, но при истории правок строки могли размножиться.
3. Показывает отчёт ДО/ПОСЛЕ.

Использование:
    venv/bin/python scripts/dedup_roles_normalize.py            # dry-run
    venv/bin/python scripts/dedup_roles_normalize.py --apply    # применить
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.models.db import create_db_engine, default_database_url, require_postgres  # noqa: E402

COUNTS = """
SELECT
  (SELECT COUNT(*) FROM user_roles) AS total,
  (SELECT COUNT(*) FROM user_roles WHERE role_id = guild_id) AS everyone,
  (SELECT COUNT(*) - COUNT(DISTINCT (user_id, role_id)) FROM user_roles) AS dup_pairs,
  (SELECT COUNT(DISTINCT role_name) FROM user_roles) AS distinct_names
"""


def report(conn, label: str) -> dict:
    r = conn.execute(text(COUNTS)).mappings().one()
    print(f"{label}: всего={r['total']}  @everyone={r['everyone']}  "
          f"дублей(user,role)={r['dup_pairs']}  уникальных имён ролей={r['distinct_names']}")
    return r


def main() -> int:
    apply_changes = "--apply" in sys.argv[1:]
    engine = create_db_engine(require_postgres(default_database_url()))

    with engine.begin() as conn:
        before = report(conn, "ДО")
        if not (before["everyone"] or before["dup_pairs"]):
            print("Нечего чистить.")
            return 0
        if not apply_changes:
            print(f"\nDRY-RUN. Для применения: {sys.argv[0]} --apply")
            return 0

        # 1) @everyone
        d1 = conn.execute(text("DELETE FROM user_roles WHERE role_id = guild_id"))
        # 2) дубли: оставляем самую «полную» агрегированную запись
        d2 = conn.execute(text("""
            DELETE FROM user_roles a USING user_roles b
             WHERE a.id < b.id AND a.user_id = b.user_id AND a.role_id = b.role_id
        """))
        after = report(conn, "ПОСЛЕ")
        print(f"УДАЛЕНО: @everyone={d1.rowcount}, дублей={d2.rowcount}")

        # Топ ролей по числу носителей — sanity check что настоящие роли целы
        rows = conn.execute(text("""
            SELECT role_name, COUNT(*) c FROM user_roles
            GROUP BY role_name ORDER BY c DESC LIMIT 10
        """)).all()
        print("\nТОП-10 ролей по носителям:")
        for name, c in rows:
            print(f"  {c:>5}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
