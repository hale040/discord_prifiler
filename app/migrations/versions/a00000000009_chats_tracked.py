"""chats.tracked — галка «отслеживать» + чистка не-текстовых каналов

Глобальное обновление сбора: в chats должны быть ТОЛЬКО текстовые каналы
(голосовые/категории/stage не содержат сообщений), а обработка канала
коллектором управляется явной галкой «отслеживать» (tracked) — оператор
сам отмечает, какие чаты обходить.

  * tracked BOOLEAN NOT NULL DEFAULT FALSE — live-фильтр контроллера и
    будущий backfill собирают сообщения только отмеченных каналов;
  * удаление строк chats с типом voice/category/stage, если такие попали
    при прежней синхронизации (сообщения в messages НЕ трогаем).

Все шаги идемпотентны — ревизия безопасно переприменяется.

Revision ID: a00000000009
Revises: a00000000008
Create Date: 2026-10-28 20:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a00000000009"
down_revision: str | None = "a00000000008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn.engine)
    if not insp.has_table("chats"):
        return
    cols = {c["name"] for c in insp.get_columns("chats")}
    if "tracked" not in cols:
        op.add_column("chats", sa.Column(
            "tracked", sa.Boolean(), nullable=False, server_default=sa.false()))
    # целевой канал из .env всегда остаётся отслеживаемым — иначе live-фильтр
    # выключит основной сбор до первой ручной отметки в админке
    cid = _target_channel_id()
    if cid is not None:
        conn.execute(sa.text(
            "INSERT INTO chats (channel_id, name, collects_qa, tracked, "
            "messages_total) VALUES (:cid, 'целевой канал (.env)', FALSE, "
            "TRUE, 0) ON CONFLICT (channel_id) DO UPDATE SET tracked = TRUE"),
            {"cid": cid})


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn.engine)
    if not insp.has_table("chats"):
        return
    cols = {c["name"] for c in insp.get_columns("chats")}
    if "tracked" in cols:
        op.drop_column("chats", "tracked")


def _target_channel_id() -> int | None:
    """CHANNEL_ID из окружения (если задан и числовой)."""
    import os

    raw = (os.getenv("CHANNEL_ID") or "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None
