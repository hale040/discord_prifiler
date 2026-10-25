"""psych_profiles.methodology_snapshot + дедуп реакций с учётом action

Две честные схемные правки вместо рантайм-DDL:

1. psych_profiles.methodology_snapshot (JSONB, nullable) — колонка, которую
   пишет import_analysis(methodology=...) из админки («🧠 Обновить психо
   профиль» падала с TypeError и отсутствующей колонкой).

2. uq_reaction_event_dedup: старый ключ UNIQUE (message_id, user_id, emoji)
   хранил только последнее состояние реакции (add/remove), история терялась.
   Новый ключ добавляет action: add и remove сосуществуют как отдельные
   события. Перед созданием индекса удаляем повторы (message_id,user_id,
   emoji,action), оставляя самое раннее событие — иначе CREATE UNIQUE
   упадёт на исторических данных.

PSQL-нюанс: user_id nullable, поэтому сравнение через IS NOT DISTINCT FROM.

Revision ID: a00000000005
Revises: a00000000004
Create Date: 2026-10-25 18:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a00000000005"
down_revision: str | None = "a00000000004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn.engine)

    # 1. снимок методики анализа
    if insp.has_table("psych_profiles"):
        cols = {c["name"] for c in insp.get_columns("psych_profiles")}
        if "methodology_snapshot" not in cols:
            op.add_column(
                "psych_profiles",
                sa.Column("methodology_snapshot",
                          sa.dialects.postgresql.JSONB(), nullable=True),
            )

    # 2. дедуп событий реакций: ключ (message_id, user_id, emoji, action)
    if insp.has_table("reaction_events"):
        op.drop_constraint(
            "uq_reaction_event_dedup", "reaction_events", type_="unique")
        # чистим повторы под новый ключ (оставляем самое раннее событие)
        op.execute(
            """DELETE FROM reaction_events a
               USING reaction_events b
               WHERE a.message_id = b.message_id
                 AND a.user_id IS NOT DISTINCT FROM b.user_id
                 AND a.emoji = b.emoji
                 AND a.action = b.action
                 AND a.id > b.id"""
        )
        op.create_unique_constraint(
            "uq_reaction_event_dedup", "reaction_events",
            ["message_id", "user_id", "emoji", "action"])


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn.engine)

    if insp.has_table("reaction_events"):
        op.drop_constraint(
            "uq_reaction_event_dedup", "reaction_events", type_="unique")
        op.execute(
            """DELETE FROM reaction_events a
               USING reaction_events b
               WHERE a.message_id = b.message_id
                 AND a.user_id IS NOT DISTINCT FROM b.user_id
                 AND a.emoji = b.emoji
                 AND a.id > b.id"""
        )
        op.create_unique_constraint(
            "uq_reaction_event_dedup", "reaction_events",
            ["message_id", "user_id", "emoji"])

    if insp.has_table("psych_profiles"):
        cols = {c["name"] for c in insp.get_columns("psych_profiles")}
        if "methodology_snapshot" in cols:
            op.drop_column("psych_profiles", "methodology_snapshot")
