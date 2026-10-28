"""chats.download_files + автопривязка участников к чатам с галкой «анкеты»

  * chats.download_files BOOLEAN NOT NULL DEFAULT FALSE — галка «скачивать
    файлы»: attachment-worker скачивает ВСЕ вложения (не только картинки)
    таких чатов, если размер <= 5 МБ;
  * перенос данных: chat_members пополняется авторами сообщений чатов, у
    которых collects_qa=TRUE. Без этого галка «анкеты» выглядела неработающей:
    привязывать анкеты было не к кому (участники создавались только при
    первичной миграции a00000000006 из старого среза сообщений).

Все шаги идемпотентны — ревизия безопасно переприменяется.

Revision ID: a00000000010
Revises: a00000000009
Create Date: 2026-10-28 23:30:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a00000000010"
down_revision: str | None = "a00000000009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn.engine)
    if not insp.has_table("chats"):
        return
    cols = {c["name"] for c in insp.get_columns("chats")}
    if "download_files" not in cols:
        op.add_column("chats", sa.Column(
            "download_files", sa.Boolean(), nullable=False,
            server_default=sa.false()))
    # данные: авторы сообщений отмеченных чатов становятся участниками
    if insp.has_table("chat_members") and insp.has_table("messages"):
        conn.execute(sa.text(
            "INSERT INTO chat_members (channel_id, author_id) "
            "SELECT DISTINCT m.channel_id, m.author_id FROM messages m "
            "  JOIN chats c ON c.channel_id = m.channel_id "
            " WHERE c.collects_qa "
            " ON CONFLICT (channel_id, author_id) DO NOTHING"))


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn.engine)
    if not insp.has_table("chats"):
        return
    cols = {c["name"] for c in insp.get_columns("chats")}
    if "download_files" in cols:
        op.drop_column("chats", "download_files")
