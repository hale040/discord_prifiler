"""messages.thread_root_id — корень треда для постов-анкет

Анкета в чате = пост первого уровня дерева (thread). Чтобы отличать
комментарии из анкет от обычных сообщений, схеме нужна колонка
thread_root_id: заполняется из msg.thread.id / reference.thread_id при
сборе; NULL = сообщение не в треде (т.е. потенциальный пост 1-го уровня).

Revision ID: a00000000007
Revises: a00000000006
Create Date: 2026-10-28 15:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a00000000007"
down_revision: str | None = "a00000000006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn.engine)
    if not insp.has_table("messages"):
        return
    cols = {c["name"] for c in insp.get_columns("messages")}
    if "thread_root_id" not in cols:
        op.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS "
                   "thread_root_id BIGINT")
        op.execute("CREATE INDEX IF NOT EXISTS ix_messages_thread_root "
                   "ON messages (thread_root_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_messages_thread_root")
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS thread_root_id")
