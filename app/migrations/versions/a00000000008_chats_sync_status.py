"""Статус отложенной синхронизации каналов: chats.sync_requested/synced_at/sync_error

Раньше «Обновить чаты с сервера» выполнялся синхронно внутри HTTP-запроса
Flask: подключение к Discord занимало до 45 секунд, страница висела, а при
ошибке результат терялся. Теперь админка только ставит сигнал
sync_requested=TRUE, а сборщик (collection_service._channels_loop) в своём
цикле подключается к Discord, подтягивает каналы и проставляет статус:
  * synced_at   — время успешного получения списка (свежесть видна в UI);
  * sync_error  — текст ошибки последнего запроса (NULL после успеха).

Все шаги идемпотентны — ревизия безопасно переприменяется.

Revision ID: a00000000008
Revises: a00000000007
Create Date: 2026-10-28 18:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a00000000008"
down_revision: str | None = "a00000000007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn.engine)
    if not insp.has_table("chats"):
        return
    cols = {c["name"] for c in insp.get_columns("chats")}
    if "sync_requested" not in cols:
        op.add_column("chats", sa.Column(
            "sync_requested", sa.Boolean(), nullable=False,
            server_default=sa.false()))
    if "synced_at" not in cols:
        op.add_column("chats", sa.Column(
            "synced_at", sa.DateTime(timezone=True), nullable=True))
    if "sync_error" not in cols:
        op.add_column("chats", sa.Column(
            "sync_error", sa.Text(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn.engine)
    if not insp.has_table("chats"):
        return
    cols = {c["name"] for c in insp.get_columns("chats")}
    for col in ("sync_error", "synced_at", "sync_requested"):
        if col in cols:
            op.drop_column("chats", col)
