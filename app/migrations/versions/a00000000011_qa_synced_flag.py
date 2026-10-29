"""user_profiles.qa_synced — пометка «about записан из анкеты»

  * user_profiles.qa_synced BOOLEAN NOT NULL DEFAULT FALSE: sync_qa_to_profile
    взводит флаг при записи анкеты в about и снимает при очистке. Флаг нужен,
    чтобы после правки текста анкеты отличать синхронизированную запись от
    bio, заполненного человеком (по совпадению текста это уже невозможно).
  * backfill: помечаем существующие about, совпадающие с чьей-то анкетой того
    же автора в chat_members.

Все шаги идемпотентны — ревизия безопасно переприменяется.

Revision ID: a00000000011
Revises: a00000000010
Create Date: 2026-10-29 20:30:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a00000000011"
down_revision: str | None = "a00000000010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn.engine)
    if not insp.has_table("user_profiles"):
        return
    cols = {c["name"] for c in insp.get_columns("user_profiles")}
    if "qa_synced" not in cols:
        op.add_column("user_profiles", sa.Column(
            "qa_synced", sa.Boolean(), nullable=False,
            server_default=sa.false()))
    # backfill только если есть табка анкет
    if insp.has_table("chat_members") and insp.has_table("chats"):
        conn.exec_driver_sql(
            "UPDATE user_profiles up SET qa_synced = TRUE "
            "WHERE EXISTS (SELECT 1 FROM chat_members cm "
            "JOIN chats c ON c.channel_id = cm.channel_id "
            "WHERE cm.author_id = up.user_id "
            "AND cm.questionnaire IS NOT NULL "
            "AND btrim(cm.questionnaire) <> '' "
            "AND COALESCE(c.collects_qa, TRUE) "
            "AND cm.questionnaire = up.about)")


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn.engine)
    if not insp.has_table("user_profiles"):
        return
    cols = {c["name"] for c in insp.get_columns("user_profiles")}
    if "qa_synced" in cols:
        op.drop_column("user_profiles", "qa_synced")
