"""baseline: схема проекта до Alembic (эквивалент self-made миграций v1..v15)

Полная стартовая схема сгенерирована из метаданных моделей
(app/models/entities.py); у user_profiles добавлено UNIQUE(user_id) — в живых
базах его создал шаг v9, на нём опираются FK user_roles/guild_member_profiles.

Живые базы НЕ прогоняют эту ревизию: ensure_schema стемпит их на head
(app/models/db.py). Для новых баз upgrade() создаёт схему за один шаг.

Revision ID: a00000000001
Revises:
Create Date: 2026-10-24 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a00000000001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE messages ( 	id BIGSERIAL NOT NULL,  	channel_id BIGINT NOT NULL,  	guild_id BIGINT,  	author_id BIGINT NOT NULL,  	content TEXT NOT NULL,  	clean_content TEXT NOT NULL,  	tts BOOLEAN NOT NULL,  	type INTEGER NOT NULL,  	flags INTEGER NOT NULL,  	reference_id BIGINT,  	reply_to_author_id BIGINT,  	edited_at TIMESTAMP WITH TIME ZONE,  	deleted_at TIMESTAMP WITH TIME ZONE,  	timestamp TIMESTAMP WITH TIME ZONE NOT NULL,  	pinned BOOLEAN NOT NULL,  	webhook_id BIGINT,  	word_count INTEGER NOT NULL,  	char_count INTEGER NOT NULL,  	has_attachment BOOLEAN NOT NULL,  	embed_count INTEGER NOT NULL,  	mention_count INTEGER NOT NULL,  	hashtag_count INTEGER NOT NULL,  	reaction_total INTEGER NOT NULL,  	reactions_json JSONB,  	mention_users_json JSONB,  	mention_roles_json JSONB,  	mention_channels_json JSONB,  	stickers_json JSONB,  	raw_json JSONB,  	collected_at TIMESTAMP WITH TIME ZONE NOT NULL,  	PRIMARY KEY (id) )"""
    )
    op.execute(
        """CREATE TABLE attachments ( 	id BIGSERIAL NOT NULL,  	message_id BIGINT NOT NULL,  	filename VARCHAR(512) NOT NULL,  	url VARCHAR(2048) NOT NULL,  	proxy_url VARCHAR(2048),  	content_type VARCHAR(256),  	size INTEGER NOT NULL,  	width INTEGER,  	height INTEGER,  	is_image BOOLEAN NOT NULL,  	kind VARCHAR(32),  	is_spoiler BOOLEAN NOT NULL,  	stored_path VARCHAR(1024),  	downloaded BOOLEAN NOT NULL,  	sha256 VARCHAR(64),  	PRIMARY KEY (id),  	FOREIGN KEY(message_id) REFERENCES messages (id) )"""
    )
    op.execute(
        """CREATE TABLE audit_log ( 	id BIGSERIAL NOT NULL,  	created_at TIMESTAMP WITH TIME ZONE NOT NULL,  	actor VARCHAR(128) NOT NULL,  	action VARCHAR(64) NOT NULL,  	target VARCHAR(256) NOT NULL,  	details TEXT,  	result VARCHAR(512) NOT NULL,  	PRIMARY KEY (id) )"""
    )
    op.execute(
        """CREATE TABLE channel_states ( 	channel_id BIGSERIAL NOT NULL,  	guild_id BIGINT,  	last_message_id BIGINT,  	oldest_message_id BIGINT,  	last_poll_at TIMESTAMP WITH TIME ZONE,  	messages_total INTEGER NOT NULL,  	PRIMARY KEY (channel_id) )"""
    )
    op.execute(
        """CREATE TABLE user_profiles ( 	id BIGSERIAL NOT NULL,  	user_id BIGINT NOT NULL UNIQUE,  	username VARCHAR(256) NOT NULL,  	discriminator VARCHAR(32) NOT NULL,  	global_name VARCHAR(256),  	mention VARCHAR(256),  	bot BOOLEAN NOT NULL,  	is_system BOOLEAN NOT NULL,  	avatar_url VARCHAR(1024),  	banner_url VARCHAR(1024),  	accent_color INTEGER,  	created_at_discord TIMESTAMP WITH TIME ZONE,  	mutual BOOLEAN,  	about TEXT,  	public_flags BIGINT,  	flags_raw JSONB,  	badges_json JSONB,  	connections_json JSONB,  	premium_type INTEGER,  	dm_fetch BOOLEAN,  	dm_channel_id BIGINT,  	dm_last_message_id BIGINT,  	dm_first_message_id BIGINT,  	dm_synced_at TIMESTAMP WITH TIME ZONE,  	dm_messages_total INTEGER NOT NULL,  	dm_last_error TEXT,  	dm_fail_count INTEGER NOT NULL,  	dm_status TEXT,  	pending_dm BOOLEAN NOT NULL,  	dm_channel_manual BOOLEAN NOT NULL,  	premium_since TIMESTAMP WITH TIME ZONE,  	locale VARCHAR(64),  	timezone_name VARCHAR(128),  	raw_json JSONB,  	priority BOOLEAN NOT NULL,  	priority_note VARCHAR(512),  	first_seen TIMESTAMP WITH TIME ZONE NOT NULL,  	last_updated TIMESTAMP WITH TIME ZONE NOT NULL,  	PRIMARY KEY (id) )"""
    )
    op.execute(
        """CREATE TABLE guild_member_profiles ( 	id BIGSERIAL NOT NULL,  	user_id BIGINT NOT NULL,  	guild_id BIGINT NOT NULL,  	nick VARCHAR(256),  	joined_at TIMESTAMP WITH TIME ZONE,  	boosting_since TIMESTAMP WITH TIME ZONE,  	pending BOOLEAN,  	mobile BOOLEAN,  	deaf BOOLEAN,  	mute BOOLEAN,  	timeout_until TIMESTAMP WITH TIME ZONE,  	avatar_url VARCHAR(1024),  	permissions_raw VARCHAR(128),  	color INTEGER,  	top_role VARCHAR(256),  	raw_json JSONB,  	PRIMARY KEY (id),  	CONSTRAINT uq_user_guild UNIQUE (user_id, guild_id),  	FOREIGN KEY(user_id) REFERENCES user_profiles (user_id) )"""
    )
    op.execute(
        """CREATE TABLE interaction_aggregates ( 	id BIGSERIAL NOT NULL,  	source_user_id BIGINT NOT NULL,  	target_user_id BIGINT NOT NULL,  	replies_given INTEGER NOT NULL,  	reactions_received INTEGER NOT NULL,  	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,  	PRIMARY KEY (id),  	CONSTRAINT uq_interaction_pair UNIQUE (source_user_id, target_user_id) )"""
    )
    op.execute(
        """CREATE TABLE message_revisions ( 	id BIGSERIAL NOT NULL,  	message_id BIGINT NOT NULL,  	author_id BIGINT,  	edit_number INTEGER NOT NULL,  	prev_content TEXT,  	new_content TEXT,  	edited_at TIMESTAMP WITH TIME ZONE,  	recorded_at TIMESTAMP WITH TIME ZONE NOT NULL,  	PRIMARY KEY (id) )"""
    )
    op.execute(
        """CREATE TABLE presence_snapshots ( 	id BIGSERIAL NOT NULL,  	user_id BIGINT NOT NULL,  	status VARCHAR(32),  	status_text VARCHAR(256),  	activity_name VARCHAR(256),  	activity_kind VARCHAR(32),  	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,  	PRIMARY KEY (id) )"""
    )
    op.execute(
        """CREATE TABLE profile_snapshots ( 	id BIGSERIAL NOT NULL,  	user_id BIGINT NOT NULL,  	taken_at TIMESTAMP WITH TIME ZONE NOT NULL,  	about TEXT,  	status_text VARCHAR(256),  	banner_hash VARCHAR(64),  	avatar_hash VARCHAR(64),  	connections_json JSONB,  	changed BOOLEAN NOT NULL,  	PRIMARY KEY (id),  	FOREIGN KEY(user_id) REFERENCES user_profiles (id) )"""
    )
    op.execute(
        """CREATE TABLE psych_profiles ( 	id BIGSERIAL NOT NULL,  	user_id BIGINT NOT NULL,  	model TEXT,  	prompt_hash TEXT,  	source_lines INTEGER,  	period_from TIMESTAMP WITH TIME ZONE,  	period_to TIMESTAMP WITH TIME ZONE,  	messages_analyzed INTEGER,  	big_five_openness FLOAT,  	big_five_conscientiousness FLOAT,  	big_five_extraversion FLOAT,  	big_five_agreeableness FLOAT,  	big_five_neuroticism FLOAT,  	mbti_type VARCHAR(8),  	mbti_confidence FLOAT,  	enneagram_type VARCHAR(8),  	enneagram_confidence FLOAT,  	confidence_overall FLOAT,  	severity_max VARCHAR(8),  	result_json JSONB,  	is_current BOOLEAN NOT NULL,  	created_at TIMESTAMP WITH TIME ZONE NOT NULL,  	PRIMARY KEY (id) )"""
    )
    op.execute(
        """CREATE TABLE psych_signals ( 	id BIGSERIAL NOT NULL,  	profile_id BIGINT,  	user_id BIGINT NOT NULL,  	signal_key TEXT,  	quote_norm TEXT NOT NULL,  	quote TEXT,  	message_id BIGINT,  	context TEXT,  	severity VARCHAR(8),  	created_at TIMESTAMP WITH TIME ZONE NOT NULL,  	PRIMARY KEY (id),  	CONSTRAINT uq_psych_signals_dedup UNIQUE (user_id, signal_key, quote_norm),  	FOREIGN KEY(profile_id) REFERENCES psych_profiles (id) ON DELETE CASCADE )"""
    )
    op.execute(
        """CREATE TABLE reaction_events ( 	id BIGSERIAL NOT NULL,  	message_id BIGINT NOT NULL,  	channel_id BIGINT,  	guild_id BIGINT,  	user_id BIGINT,  	msg_author_id BIGINT,  	emoji VARCHAR(128) NOT NULL,  	action VARCHAR(8) NOT NULL,  	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,  	PRIMARY KEY (id),  	CONSTRAINT uq_reaction_event_dedup UNIQUE (message_id, user_id, emoji) )"""
    )
    op.execute(
        """CREATE TABLE user_games ( 	id BIGSERIAL NOT NULL,  	user_id BIGINT NOT NULL,  	name VARCHAR(256) NOT NULL,  	kind VARCHAR(32),  	details VARCHAR(512),  	state VARCHAR(512),  	url VARCHAR(1024),  	application_id BIGINT,  	first_seen TIMESTAMP WITH TIME ZONE NOT NULL,  	last_seen TIMESTAMP WITH TIME ZONE NOT NULL,  	PRIMARY KEY (id),  	CONSTRAINT uq_user_game UNIQUE (user_id, name),  	FOREIGN KEY(user_id) REFERENCES user_profiles (user_id) ON DELETE CASCADE )"""
    )
    op.execute(
        """CREATE TABLE user_interests ( 	id BIGSERIAL NOT NULL,  	user_id BIGINT NOT NULL,  	topic TEXT NOT NULL,  	topic_norm TEXT NOT NULL,  	evidence TEXT,  	evidence_count INTEGER NOT NULL,  	first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,  	last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,  	PRIMARY KEY (id),  	CONSTRAINT uq_user_interests_norm UNIQUE (user_id, topic_norm) )"""
    )
    op.execute(
        """CREATE TABLE user_roles ( 	id BIGSERIAL NOT NULL,  	user_id BIGINT NOT NULL,  	role_id BIGINT NOT NULL,  	guild_id BIGINT NOT NULL,  	role_name VARCHAR(256) NOT NULL,  	position INTEGER NOT NULL,  	color INTEGER,  	is_mentionable BOOLEAN,  	permissions_raw VARCHAR(128),  	first_seen TIMESTAMP WITH TIME ZONE NOT NULL,  	last_seen TIMESTAMP WITH TIME ZONE NOT NULL,  	last_lost_at TIMESTAMP WITH TIME ZONE,  	PRIMARY KEY (id),  	CONSTRAINT uq_user_role UNIQUE (user_id, role_id),  	FOREIGN KEY(user_id) REFERENCES user_profiles (user_id) )"""
    )
    op.execute(
        """CREATE INDEX ix_messages_author_ts ON public.messages USING btree (author_id, "timestamp")"""
    )
    op.execute(
        """CREATE INDEX ix_messages_channel_id ON public.messages USING btree (channel_id)"""
    )
    op.execute(
        """CREATE INDEX ix_messages_reference_id ON public.messages USING btree (reference_id)"""
    )
    op.execute(
        """CREATE INDEX ix_messages_timestamp ON public.messages USING btree ("timestamp")"""
    )
    op.execute(
        """CREATE INDEX ix_messages_channel_ts ON public.messages USING btree (channel_id, "timestamp")"""
    )
    op.execute(
        """CREATE INDEX ix_messages_author_id ON public.messages USING btree (author_id)"""
    )
    op.execute(
        """CREATE INDEX ix_messages_guild_id ON public.messages USING btree (guild_id)"""
    )
    op.execute(
        """CREATE INDEX ix_messages_live_ts ON public.messages USING btree ("timestamp") WHERE (deleted_at IS NULL)"""
    )
    op.execute(
        """CREATE INDEX ix_messages_reply_to_author_id ON public.messages USING btree (reply_to_author_id)"""
    )
    op.execute(
        """CREATE INDEX ix_attachments_message_id ON public.attachments USING btree (message_id)"""
    )
    op.execute(
        """CREATE INDEX ix_audit_log_created_at ON public.audit_log USING btree (created_at)"""
    )
    op.execute(
        """CREATE INDEX ix_user_profiles_priority ON public.user_profiles USING btree (priority)"""
    )
    op.execute(
        """CREATE INDEX ix_guild_member_profiles_user_id ON public.guild_member_profiles USING btree (user_id)"""
    )
    op.execute(
        """CREATE INDEX ix_guild_member_profiles_guild_id ON public.guild_member_profiles USING btree (guild_id)"""
    )
    op.execute(
        """CREATE INDEX ix_interaction_aggregates_target_user_id ON public.interaction_aggregates USING btree (target_user_id)"""
    )
    op.execute(
        """CREATE INDEX ix_interaction_aggregates_source_user_id ON public.interaction_aggregates USING btree (source_user_id)"""
    )
    op.execute(
        """CREATE INDEX ix_message_revisions_author_id ON public.message_revisions USING btree (author_id)"""
    )
    op.execute(
        """CREATE INDEX ix_message_revisions_message_id ON public.message_revisions USING btree (message_id)"""
    )
    op.execute(
        """CREATE INDEX ix_message_revisions_msg_edit ON public.message_revisions USING btree (message_id, edit_number)"""
    )
    op.execute(
        """CREATE INDEX ix_presence_snapshots_user_id ON public.presence_snapshots USING btree (user_id)"""
    )
    op.execute(
        """CREATE INDEX ix_presence_user_time ON public.presence_snapshots USING btree (user_id, observed_at)"""
    )
    op.execute(
        """CREATE INDEX ix_profile_snapshots_user_id ON public.profile_snapshots USING btree (user_id)"""
    )
    op.execute(
        """CREATE INDEX ix_psych_profiles_user_id ON public.psych_profiles USING btree (user_id)"""
    )
    op.execute(
        """CREATE INDEX ix_psych_profiles_is_current ON public.psych_profiles USING btree (is_current)"""
    )
    op.execute(
        """CREATE INDEX ix_psych_current_user ON public.psych_profiles USING btree (user_id, created_at)"""
    )
    op.execute(
        """CREATE INDEX ix_psych_signals_user_id ON public.psych_signals USING btree (user_id)"""
    )
    op.execute(
        """CREATE INDEX ix_psych_signals_profile_id ON public.psych_signals USING btree (profile_id)"""
    )
    op.execute(
        """CREATE INDEX ix_reaction_events_author_time ON public.reaction_events USING btree (msg_author_id, occurred_at)"""
    )
    op.execute(
        """CREATE INDEX ix_reaction_events_message_id ON public.reaction_events USING btree (message_id)"""
    )
    op.execute(
        """CREATE INDEX ix_reaction_events_msg_author_id ON public.reaction_events USING btree (msg_author_id)"""
    )
    op.execute(
        """CREATE INDEX ix_reaction_events_user_id ON public.reaction_events USING btree (user_id)"""
    )
    op.execute(
        """CREATE INDEX ix_user_games_user_id ON public.user_games USING btree (user_id)"""
    )
    op.execute(
        """CREATE INDEX ix_user_interests_user_id ON public.user_interests USING btree (user_id)"""
    )
    op.execute(
        """CREATE INDEX ix_user_roles_role_id ON public.user_roles USING btree (role_id)"""
    )
    op.execute(
        """CREATE INDEX ix_user_roles_user_id ON public.user_roles USING btree (user_id)"""
    )
    op.execute(
        """CREATE INDEX ix_user_roles_guild_id ON public.user_roles USING btree (guild_id)"""
    )
    # --- PG-функции и триггеры агрегатов взаимодействий (v10) --------------
    from app.models.entities import _TRG_REACTION_PAIR, _TRG_REPLY_PAIR
    bind = op.get_bind()
    bind.execute(sa.text(_TRG_REPLY_PAIR))
    bind.execute(sa.text("DROP TRIGGER IF EXISTS trg_messages_reply_pair ON messages"))
    bind.execute(sa.text(
        "CREATE TRIGGER trg_messages_reply_pair AFTER UPDATE OF "
        "reply_to_author_id ON messages FOR EACH ROW "
        "WHEN (OLD.reply_to_author_id IS NULL "
        "AND NEW.reply_to_author_id IS NOT NULL) "
        "EXECUTE FUNCTION trg_messages_reply_pair()"))
    bind.execute(sa.text(_TRG_REACTION_PAIR))
    bind.execute(sa.text("DROP TRIGGER IF EXISTS trg_reaction_events_pair ON reaction_events"))
    bind.execute(sa.text(
        "CREATE TRIGGER trg_reaction_events_pair AFTER INSERT ON "
        "reaction_events FOR EACH ROW EXECUTE FUNCTION "
        "trg_reaction_events_pair()"))

def downgrade() -> None:
    op.execute("""DROP TABLE IF EXISTS "user_roles" CASCADE""")
    op.execute("""DROP TABLE IF EXISTS "user_interests" CASCADE""")
    op.execute("""DROP TABLE IF EXISTS "user_games" CASCADE""")
    op.execute("""DROP TABLE IF EXISTS "reaction_events" CASCADE""")
    op.execute("""DROP TABLE IF EXISTS "psych_signals" CASCADE""")
    op.execute("""DROP TABLE IF EXISTS "psych_profiles" CASCADE""")
    op.execute("""DROP TABLE IF EXISTS "profile_snapshots" CASCADE""")
    op.execute("""DROP TABLE IF EXISTS "presence_snapshots" CASCADE""")
    op.execute("""DROP TABLE IF EXISTS "message_revisions" CASCADE""")
    op.execute("""DROP TABLE IF EXISTS "interaction_aggregates" CASCADE""")
    op.execute("""DROP TABLE IF EXISTS "guild_member_profiles" CASCADE""")
    op.execute("""DROP TABLE IF EXISTS "user_profiles" CASCADE""")
    op.execute("""DROP TABLE IF EXISTS "channel_states" CASCADE""")
    op.execute("""DROP TABLE IF EXISTS "audit_log" CASCADE""")
    op.execute("""DROP TABLE IF EXISTS "attachments" CASCADE""")
    op.execute("""DROP TABLE IF EXISTS "messages" CASCADE""")
