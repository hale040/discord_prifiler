"""Серверы, чаты и анкеты: guilds/chats/chat_members + перенос данных

Новая иерархия работы оператора: ID сервера -> список чатов -> участники ->
анкета (TEXT). Анкеты попадают в промпт LLM как первичный источник сведений.

Перенос данных (данные не теряются — таблицы наполняются из существующей
истории сообщений):
  * guilds   — все guild_id, встречавшиеся в messages;
  * chats    — все каналы этих гильдий (из channel_states и messages),
               имя канала подтягивается из channel_states.name, если колонка
               есть (старые схемы без неё — оставляем ''), счётчик сообщений;
  * chat_members — авторы сообщений каждого канала (DISTINCT author_id);
    questionnaire остаётся NULL до ручного заполнения в админке.

Все шаги идемпотентны (IF NOT EXISTS / ON CONFLICT DO NOTHING) — ревизия
безопасно переприменяется поверх частично созданной схемы.

Revision ID: a00000000006
Revises: a00000000005
Create Date: 2026-10-28 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a00000000006"
down_revision: str | None = "a00000000005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn.engine)

    op.execute("""
        CREATE TABLE IF NOT EXISTS guilds (
            guild_id BIGINT PRIMARY KEY,
            name VARCHAR(256) NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ
        )""")
    op.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            channel_id BIGINT PRIMARY KEY,
            guild_id BIGINT,
            name VARCHAR(256) NOT NULL DEFAULT '',
            collects_qa BOOLEAN NOT NULL DEFAULT FALSE,
            messages_total INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ
        )""")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chats_guild_id ON chats (guild_id)")
    op.execute("""
        CREATE TABLE IF NOT EXISTS chat_members (
            id BIGSERIAL PRIMARY KEY,
            channel_id BIGINT NOT NULL,
            author_id BIGINT NOT NULL,
            questionnaire TEXT,
            message_id BIGINT,
            updated_at TIMESTAMPTZ,
            CONSTRAINT uq_chat_member UNIQUE (channel_id, author_id)
        )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_members_channel "
               "ON chat_members (channel_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_members_author "
               "ON chat_members (author_id)")

    if not insp.has_table("messages"):
        return

    # --- перенос: гильдии из истории сообщений -----------------------------
    conn.execute(sa.text("""
        INSERT INTO guilds (guild_id)
        SELECT DISTINCT guild_id FROM messages WHERE guild_id IS NOT NULL
        ON CONFLICT (guild_id) DO NOTHING"""))

    # --- перенос: чаты (каналы) этих гильдий --------------------------------
    has_cs = insp.has_table("channel_states")
    cs_name = ("cs.name" if has_cs and "name" in
               {c["name"] for c in insp.get_columns("channel_states")} else "''")
    cs_join = ("LEFT JOIN channel_states cs ON cs.channel_id = m.channel_id"
               if has_cs else "")
    conn.execute(sa.text(f"""
        INSERT INTO chats (channel_id, guild_id, name, messages_total)
        SELECT m.channel_id, min(m.guild_id), COALESCE(MAX({cs_name}), ''),
               count(*)
          FROM messages m
          {cs_join}
         WHERE m.guild_id IS NOT NULL
         GROUP BY m.channel_id
        ON CONFLICT (channel_id) DO UPDATE
          SET messages_total = EXCLUDED.messages_total,
              guild_id = COALESCE(chats.guild_id, EXCLUDED.guild_id),
              name = CASE WHEN chats.name = '' THEN EXCLUDED.name ELSE chats.name END"""))

    # --- перенос: участники чатов = авторы сообщений ------------------------
    conn.execute(sa.text("""
        INSERT INTO chat_members (channel_id, author_id)
        SELECT DISTINCT channel_id, author_id FROM messages
         WHERE guild_id IS NOT NULL AND author_id IS NOT NULL
        ON CONFLICT (channel_id, author_id) DO NOTHING"""))


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS chat_members")
    op.execute("DROP TABLE IF EXISTS chats")
    op.execute("DROP TABLE IF EXISTS guilds")
