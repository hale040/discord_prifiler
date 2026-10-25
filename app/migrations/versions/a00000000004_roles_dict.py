"""roles dictionary: нормализация — названия ролей вынесены из user_roles

user_roles хранил role_name дубликатом на каждую связь пользователь×роль.
При переименовании роли в Discord имя «протухало» во всех строках и его
нужно было чинить руками. Теперь справочник ролей — отдельная таблица roles
(guild_id, role_id) UNIQUE, а user_roles остаётся чистой связью с динамикой
(first_seen/last_seen/last_lost_at).

История не теряется: старые имена переносятся в roles (MIN(first_seen) как
момент появления), после чего колонка user_roles.role_name удаляется.

Revision ID: a00000000004
Revises: a00000000003
Create Date: 2026-10-25 14:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a00000000004"
down_revision: str | None = "a00000000003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn.engine)
    if not insp.has_table("user_roles"):
        return
    op.execute(
        """CREATE TABLE IF NOT EXISTS roles (
               id BIGSERIAL PRIMARY KEY,
               guild_id BIGINT NOT NULL,
               role_id BIGINT NOT NULL,
               name VARCHAR(256) NOT NULL DEFAULT '',
               position INTEGER NOT NULL DEFAULT 0,
               color INTEGER,
               is_mentionable BOOLEAN,
               permissions_raw VARCHAR(128),
               first_seen TIMESTAMPTZ,
               last_seen TIMESTAMPTZ,
               CONSTRAINT uq_role_guild UNIQUE (guild_id, role_id))"""
    )
    cols = {c["name"] for c in insp.get_columns("user_roles")}
    if "role_name" in cols:
        # переносим уникальные (guild_id, role_id): берём строку с наибольшим
        # last_seen — там самое свежее имя; агрегаты first/last по всем носителям
        conn.execute(sa.text(
            """INSERT INTO roles (guild_id, role_id, name, position, color,
                                  is_mentionable, permissions_raw, first_seen, last_seen)
               SELECT DISTINCT ON (ur.guild_id, ur.role_id)
                      ur.guild_id, ur.role_id, ur.role_name, ur.position, ur.color,
                      ur.is_mentionable, ur.permissions_raw,
                      f.first_seen, l.last_seen
                 FROM user_roles ur
                 JOIN LATERAL (SELECT MIN(first_seen) AS first_seen FROM user_roles x
                                WHERE x.guild_id = ur.guild_id AND x.role_id = ur.role_id) f ON TRUE
                 JOIN LATERAL (SELECT MAX(last_seen)  AS last_seen FROM user_roles x
                                WHERE x.guild_id = ur.guild_id AND x.role_id = ur.role_id) l ON TRUE
               ORDER BY ur.guild_id, ur.role_id, ur.last_seen DESC
               ON CONFLICT (guild_id, role_id) DO NOTHING"""
        ))
        op.drop_column("user_roles", "role_name")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_roles_guild_role ON user_roles (guild_id, role_id)"
    )


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn.engine)
    if not insp.has_table("user_roles"):
        return
    if "role_name" not in {c["name"] for c in insp.get_columns("user_roles")}:
        op.add_column("user_roles", sa.Column("role_name", sa.String(256),
                                              nullable=False, server_default=""))
        if insp.has_table("roles"):
            conn.execute(sa.text(
                """UPDATE user_roles ur SET role_name = r.name
                     FROM roles r
                    WHERE r.guild_id = ur.guild_id AND r.role_id = ur.role_id"""
            ))
    op.execute("DROP TABLE IF EXISTS roles")
