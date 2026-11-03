"""chats.parse_from — дата, с которой канал парсится (календарь в админке)

  * chats.parse_from TIMESTAMPTZ NULL: backfill канала начинается от этой
    даты через snowflake-курсор history(after=...); NULL = глобальный
    START_DATE / весь канал.
  * идемпотентно: колонка добавляется только если её нет.

Revision ID: a00000000012
Revises: a00000000011
Create Date: 2026-11-03 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a00000000012"
down_revision: str | None = "a00000000011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn.engine)
    if not insp.has_table("chats"):
        return
    cols = {c["name"] for c in insp.get_columns("chats")}
    if "parse_from" not in cols:
        op.add_column("chats", sa.Column(
            "parse_from", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn.engine)
    if not insp.has_table("chats"):
        return
    cols = {c["name"] for c in insp.get_columns("chats")}
    if "parse_from" in cols:
        op.drop_column("chats", "parse_from")
