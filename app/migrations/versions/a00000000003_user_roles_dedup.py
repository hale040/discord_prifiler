"""user_roles dedup: @everyone-роли не являются настоящими ролями

Причина раздувания таблицы user_roles: discord.Member.roles всегда включает
@everyone (role_id == guild_id). Она есть у каждого участника сервера, поэтому
на каждый наблюдаемый профиль попадала лишняя строка. Маппер исправлен
(services/mappers.py), эта ревизия чистит накопленный мусор в существующих БД.

Осторожно: last_lost_at НЕ трогаем — @everyone никогда не «терялась», это
артефакт записи, а не сигнал динамики ролей.

Revision ID: a00000000003
Revises: a00000000002
Create Date: 2026-10-25 13:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a00000000003"
down_revision: str | None = "a00000000002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn.engine).has_table("user_roles"):
        return
    res = conn.execute(sa.text(
        "DELETE FROM user_roles WHERE role_id = guild_id"
    ))
    print(f"[a00000000003] user_roles: удалено @everyone-строк: {res.rowcount}")


def downgrade() -> None:
    # Данные-мусор обратно не восстанавливаем осознанно.
    pass
