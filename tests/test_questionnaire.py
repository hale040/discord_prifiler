"""Тесты сборщика анкет (app.services.profile_export.questionnaire).

Покрывают контракт «галка анкеты в чате»:
  * qa_posts_autofill — привязка поста-анкеты (первый пост автора) к участнику;
  * fetch_questionnaires — выдача заполненных анкет + мягкая деградация;
  * fetch_qa_replies — комментарии к постам-анкетам (реплаи и ветки тредов);
  * render_block / render_replies_block — разметка для промпта LLM;
  * inject_into_dialog — вставка анкеты служебной «репликой» в ленту.
"""
from __future__ import annotations

from app.services.profile_export import questionnaire as q


def _q(conn, sql):
    """Первый запрос-скаляр сиддинга (SQLite подставляет литералы сам)."""
    return conn.execute(sql)[0]


def _seed(conn):
    """Один канал с галкой «анкеты» + контрольный канал без галки.

    11 и 22 — участники без анкеты (кандидаты на autofill), 33 — ручная
    анкета без якоря; 1000/1002 — посты первого уровня, 1001 — сообщение
    ветки треда (thread_root_id), 1003/1004 — реплаи на пост-анкету.
    """
    for sql in [
        "INSERT INTO chats (channel_id, guild_id, name, collects_qa)"
        " VALUES (100, 1, 'Анкеты', 1)",
        "INSERT INTO chats (channel_id, guild_id, name, collects_qa)"
        " VALUES (200, 1, 'Обычный', 0)",
        "INSERT INTO chat_members (channel_id, author_id, questionnaire)"
        " VALUES (100, 11, '')",
        "INSERT INTO chat_members (channel_id, author_id, questionnaire)"
        " VALUES (100, 22, '')",
        "INSERT INTO chat_members (channel_id, author_id, questionnaire,"
        " message_id) VALUES (100, 33, 'ручная анкета', NULL)",
        "INSERT INTO messages (id, channel_id, author_id, content,"
        " clean_content, timestamp, reference_id, thread_root_id) VALUES"
        " (1000, 100, 11, 'Я Вася, мне 30, люблю рыбу',"
        " 'Я Вася, мне 30, люблю рыбу', '2026-01-01T10:00:00', NULL, NULL)",
        "INSERT INTO messages (id, channel_id, author_id, content,"
        " clean_content, timestamp, reference_id, thread_root_id) VALUES"
        " (1001, 100, 11, 'дополнение в треде', 'дополнение в треде',"
        " '2026-01-01T10:01:00', NULL, 1000)",
        "INSERT INTO messages (id, channel_id, author_id, content,"
        " clean_content, timestamp, reference_id, thread_root_id) VALUES"
        " (1002, 100, 22, 'Я Петя, я врач', 'Я Петя, я врач',"
        " '2026-01-01T10:02:00', NULL, NULL)",
        "INSERT INTO messages (id, channel_id, author_id, content,"
        " clean_content, timestamp, reference_id, thread_root_id) VALUES"
        " (1003, 100, 33, 'Вася вообще не рыбачит', 'Вася вообще не рыбачит',"
        " '2026-01-01T10:03:00', 1000, 1000)",
        "INSERT INTO messages (id, channel_id, author_id, content,"
        " clean_content, timestamp, reference_id, thread_root_id) VALUES"
        " (1004, 100, 22, 'Петя тоже рыбак?', 'Петя тоже рыбак?',"
        " '2026-01-01T10:04:00', 1000, NULL)",
        "INSERT INTO messages (id, channel_id, author_id, content,"
        " clean_content, timestamp, reference_id, thread_root_id) VALUES"
        " (1005, 200, 11, 'пост в канале без галки',"
        " 'пост в канале без галки', '2026-01-01T10:05:00', NULL, NULL)",
        "INSERT INTO user_profiles (user_id, username, global_name)"
        " VALUES (11, 'vasya', 'Вася')",
        "INSERT INTO user_profiles (user_id, username, global_name)"
        " VALUES (22, 'petya', 'Петя')",
        "INSERT INTO user_profiles (user_id, username, global_name)"
        " VALUES (33, 'side', NULL)",
    ]:
        conn.execute_write(sql)


# ---------------------------------------------------------------- autofill

def test_autofill_binds_first_post_and_copies_text(conn):
    _seed(conn)
    n = q.qa_posts_autofill(conn)
    assert n == 2, "привязать нужно посты участников 11 и 22"
    row = _q(conn, "SELECT * FROM chat_members WHERE author_id=11")
    assert row["message_id"] == 1000
    assert row["questionnaire"] == "Я Вася, мне 30, люблю рыбу"


def test_autofill_keeps_manual_questionnaire(conn):
    _seed(conn)
    q.qa_posts_autofill(conn)
    row = _q(conn, "SELECT * FROM chat_members WHERE author_id=33")
    assert row["questionnaire"] == "ручная анкета"
    assert row["message_id"] is None, "ручную анкету не надо привязывать к посту"


def test_autofill_skips_channels_without_flag(conn):
    _seed(conn)
    q.qa_posts_autofill(conn)
    assert _q(conn, "SELECT COUNT(*) c FROM chat_members WHERE channel_id=200")["c"] == 0, "в канал без галки «анкеты» участники не заводятся"


def test_autofill_is_idempotent(conn):
    _seed(conn)
    assert q.qa_posts_autofill(conn) == 2
    assert q.qa_posts_autofill(conn) == 0


def test_autofill_respects_guild_filter(conn):
    _seed(conn)
    assert q.qa_posts_autofill(conn, guild_id=999) == 0


# ------------------------------------------------------ fetch_questionnaires

def test_fetch_returns_filled_only(conn):
    _seed(conn)
    q.qa_posts_autofill(conn)
    items = q.fetch_questionnaires(conn, channel_ids=[100])
    assert {i["author_id"] for i in items} == {11, 22, 33}
    by = {i["author_id"]: i for i in items}
    assert by[11]["author_name"] == "Вася"
    assert by[11]["subject"] == 11, "subject — автор якорного сообщения"
    assert by[33]["subject"] is None, "без якоря subject пуст"


def test_fetch_degrades_without_tables(conn):
    conn.execute_write("DROP TABLE chat_members"); conn.execute_write("DROP TABLE chats")
    assert q.fetch_questionnaires(conn) == []
    assert q.qa_posts_autofill(conn) == 0
    assert q.fetch_qa_replies(conn) == []


# ---------------------------------------------------------- fetch_qa_replies

def test_replies_cover_reference_and_thread_branches(conn):
    _seed(conn)
    q.qa_posts_autofill(conn)
    rep = q.fetch_qa_replies(conn, channel_ids=[100])
    ids = {r["id"] for r in rep}
    assert ids == {1001, 1003}, "реплаи и сообщения ветки треда, без дублей"
    r33 = next(r for r in rep if r["author_id"] == 33)
    assert r33["qa_author_id"] == 11
    assert r33["qa_author_name"] == "Вася"


def test_render_replies_blocks_split_by_target(conn):
    _seed(conn)
    q.qa_posts_autofill(conn)
    rep = q.fetch_qa_replies(conn)
    txt = q.render_replies_block(rep, target_uid=11)
    assert "Отзывы на анкету целевого пользователя" in txt
    assert "Вася вообще не рыбачит" in txt
    assert "Комментарии целевого пользователя к анкетам других" in txt
    assert q.render_replies_block([], target_uid=11) == ""


# --------------------------------------------------------------- render/inject

def test_render_block_marks_source(conn):
    _seed(conn)
    q.qa_posts_autofill(conn)
    items = q.fetch_questionnaires(conn)
    txt = q.render_block(items, target_uid=11)
    assert "## Анкета" in txt and "[анкета]" in txt
    assert "### Анкета целевого пользователя" in txt
    assert "Я Вася" in txt
    assert "### Анкеты других участников" in txt


def test_inject_places_anchor_row_and_keeps_limit_honest(conn):
    _seed(conn)
    q.qa_posts_autofill(conn)
    items = q.fetch_questionnaires(conn)
    dialog = conn.execute(
        "SELECT id, channel_id, guild_id, author_id, content, clean_content,"
        " timestamp FROM messages WHERE channel_id=100 AND id <> 1004")
    merged, n = q.inject_into_dialog(dialog, {}, items, target_uid=11)
    assert n == len(items) > 0
    qa_rows = [m for m in merged if str(m["content"]).startswith("[анкета]")]
    assert len(qa_rows) == n
    # сортировка ленты не сломана
    ts = [str(m["timestamp"]) for m in merged]
    assert ts == sorted(ts)
    # исходник не мутируется
    assert len(dialog) == 6


def test_inject_noop_on_empty_inputs(conn):
    merged, n = q.inject_into_dialog([], {}, [], target_uid=1)
    assert (merged, n) == ([], 0)
