"""legacy schema fixes: self-made v1..v15 поверх существующих баз

Ревизия для ЖИВЫХ баз, созданных до перехода на Alembic (ad-hoc ALTER'ы и
исторические self-made шаги v1..v15). Все операции идемпотентны:
- ADD COLUMN IF NOT EXISTS — доливают недостающие dm_* поля (v11..v15);
- CREATE INDEX IF NOT EXISTS — частичные индексы ЛС-цикла;
- bigserial-PK (шаг v9) — приводит id-колонки к BIGINT+sequence;
- триггеры interaction_aggregates (шаг v10) — CREATE OR REPLACE.

На пустых базах (схема создана baseline-ревизией a00000000001) это no-op.

Revision ID: a00000000002
Revises: a00000000001
Create Date: 2026-10-24 12:10:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a00000000002"
down_revision: str | None = "a00000000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    engine = op.get_bind().engine
    # Импортируем исторические шаги как готовый идемпотентный DDL.
    from app.models.migrations import (
        _pg_bigserial_pk,
        _table_exists,
        _v10_dynamic_signals,
        _v11_dm_collection,
        _v12_dm_diagnostics,
        _v13_dm_manual_channel,
        _v14_dm_status,
        _v15_dm_pending_flag,
    )
    from app.models.entities import Base

    if not _table_exists(engine, "user_profiles"):
        return  # схема ещё не создана вовсе — ничего «доливать» не нужно

    # v9-нормы: BIGINT PK + sequence для таблиц с autoincrement-id
    for tname in Base.metadata.tables:
        try:
            _pg_bigserial_pk(engine, tname)
        except Exception:  # noqa: BLE001 — дешёвая страховка, шаг идемпотентен
            pass

    _v10_dynamic_signals(engine)
    _v11_dm_collection(engine)
    _v12_dm_diagnostics(engine)
    _v13_dm_manual_channel(engine)
    _v14_dm_status(engine)
    _v15_dm_pending_flag(engine)


def downgrade() -> None:
    pass
