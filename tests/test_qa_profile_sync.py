"""Анкета → user_profiles.about: сохранение данных анкеты в профиль.

smoke-тест Repository.sync_qa_to_profile на SQLite (сырой DDL — модельные
JSONB-колонки SQLite не компилирует). Проверяет: анкета пишется в about,
ручной bio не перетирается, пустая анкета очищает только синхронизированный.
"""
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def repo(tmp_path):
    from app.models.repository import Repository

    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}")

    @event.listens_for(eng, "connect")
    def _funcs(dbapi_con, _rec):
        dbapi_con.create_function("btrim", 1, lambda v: v.strip() if v else v)

    with eng.begin() as c:
        c.execute(text("CREATE TABLE user_profiles (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                       " user_id BIGINT UNIQUE, username TEXT DEFAULT '', about TEXT)"))
        c.execute(text("CREATE TABLE chats (channel_id INTEGER PRIMARY KEY,"
                       " collects_qa BOOLEAN DEFAULT 0)"))
        c.execute(text("CREATE TABLE chat_members (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                       " channel_id BIGINT, author_id BIGINT, questionnaire TEXT,"
                       " message_id BIGINT, updated_at TIMESTAMP,"
                       " UNIQUE(channel_id, author_id))"))
    return Repository(sessionmaker(bind=eng))


def _seed(repo, cid=100, uid=7, text_v="Вопросы ответы", qa=True):
    with repo._sf() as s:
        s.execute(text("INSERT OR IGNORE INTO chats (channel_id, collects_qa)"
                       " VALUES (:c, :q)"), {"c": cid, "q": qa})
        if text_v is not None:
            s.execute(text("INSERT INTO chat_members (channel_id, author_id,"
                           " questionnaire, updated_at) VALUES (:c,:a,:t, now_)".replace(
                               "now_", "'2026-01-01'")), {"c": cid, "a": uid, "t": text_v})
        s.commit()


def test_writes_into_user_profiles_about(repo):
    _seed(repo)
    assert repo.sync_qa_to_profile(7) == "Вопросы ответы"
    with repo._sf() as s:
        about = s.execute(text("SELECT about FROM user_profiles WHERE user_id=7")).scalar()
    assert about == "Вопросы ответы"


def test_manual_bio_not_overwritten(repo):
    _seed(repo, text_v="Анкета")
    with repo._sf() as s:
        s.execute(text("INSERT INTO user_profiles (user_id, username, about)"
                       " VALUES (7,'bob','мой био')"))
        s.commit()
    assert repo.sync_qa_to_profile(7) is None
    with repo._sf() as s:
        assert s.execute(text("SELECT about FROM user_profiles WHERE user_id=7")).scalar() == "мой био"


def test_resync_updates_previous_questionnaire(repo):
    _seed(repo, text_v="Старая")
    repo.sync_qa_to_profile(7)
    with repo._sf() as s:
        s.execute(text("UPDATE chat_members SET questionnaire='Обновлённая',"
                       " updated_at='2026-05-05'"))
        s.commit()
    assert repo.sync_qa_to_profile(7) == "Обновлённая"
    with repo._sf() as s:
        assert s.execute(text("SELECT about FROM user_profiles WHERE user_id=7")).scalar() == "Обновлённая"


def test_empty_questionnaire_clears_only_synced(repo):
    # была синхронизированная анкета -> её удалили/очистили -> about чистится
    _seed(repo, text_v="Анкета из чата")
    with repo._sf() as s:
        s.execute(text("INSERT INTO user_profiles (user_id, username, about)"
                       " VALUES (7,'x','')"))
        s.commit()
    assert repo.sync_qa_to_profile(7) == "Анкета из чата"
    with repo._sf() as s:
        s.execute(text("UPDATE chat_members SET questionnaire=NULL"))
        s.commit()
    assert repo.sync_qa_to_profile(7) is None
    with repo._sf() as s:
        assert s.execute(text("SELECT about FROM user_profiles WHERE user_id=7")).scalar() is None


def test_no_chat_member_is_noop(repo):
    assert repo.sync_qa_to_profile(999) is None
