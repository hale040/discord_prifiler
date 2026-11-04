"""Регрессия на «левые сообщения» в ленте промпта (выгрузка профиля).

1) build_dialog: пинги считаются только по ПОЛНОМУ токену <@id>/<@!id>.
   Обрезанный "<@123" без ">" — не обращение, чужое сообщение в ленту не берётся.
2) qa_text: пустая JSON-обёртка -> "" (раньше возвращался сырой {"v":1,...},
   который попадал прямо в промпт).
"""
import re
from app.services.profile_export import dialog as d
from app.services.profile_export.questionnaire import qa_text

T = 435676094033559554


def _mk(i, uid, sec, content="", ref=None):
    return {"id": i, "author_id": uid,
            "timestamp": f"2026-11-03T12:00:{sec:02d}+00:00",
            "content": content, "clean_content": content, "reference_id": ref}


def test_broken_mention_not_linked():
    msgs = [_mk(1, T, 0), _mk(2, 555, 10, f"<@{T} привет")]  # битый токен
    sel, mode = d.build_dialog(msgs, T)
    assert {m["id"] for m in sel} == {1}, \
        "обрезанный mention не должен связывать чужое сообщение"


def test_full_ping_is_linked():
    msgs = [_mk(1, T, 0), _mk(4, 999, 20, f"<@!{T}> как дела?")]
    sel, mode = d.build_dialog(msgs, T)
    assert {m["id"] for m in sel} == {1, 4}, "полный пинг = обращение, берём"


def test_strict_regex_shape():
    pat = d._STRICT_MENTION_RE.pattern
    assert pat.endswith(">") and ">?" not in pat


def test_qa_text_empty_wrapper():
    assert qa_text('{"v":1,"text":"","answers":[]}') == ""
    assert qa_text('{"v":1,"text":"жизнь","answers":[]}') == "жизнь"
    assert qa_text(None) == "" and qa_text("") == ""
