"""Окна связности build_dialog: env применяется, явные аргументы приоритетны,
входящие пинги вне окна не попадают в ленту (регрессия «несвязанных сообщений»)."""
import os

from app.services.profile_export.dialog import build_dialog


def mk(id_, uid, off_sec, content="", ref=None):
    h, m, s = 10 + off_sec // 3600, (off_sec % 3600) // 60, off_sec % 60
    return {"id": id_, "author_id": uid,
            "timestamp": f"2026-11-03T{h:02d}:{m:02d}:{s:02d}+00:00",
            "content": content, "reference_id": ref, "channel_id": 1,
            "clean_content": content}


T = 777
MSGS = [mk(1, T, 0), mk(5, 999, 10, f"<@{T}> ок"), mk(3, 555, 20),
        mk(2, 555, 900, f"<@{T}>"), mk(4, 888, 5000, f"<@{T}> hi")]


def ids_of(sel):
    return {m["id"] for m in sel}


def test_env_window_applies(monkeypatch):
    monkeypatch.setenv("EXPORT_CONTEXT_WINDOW", "60")
    sel, _ = build_dialog(MSGS, T)
    assert ids_of(sel) == {1, 3, 5}          # офтопик-пинги 15мин/80мин отсеяны


def test_explicit_arg_beats_env(monkeypatch):
    monkeypatch.setenv("EXPORT_CONTEXT_WINDOW", "60")
    sel, _ = build_dialog(MSGS, T, time_window_sec=1200)
    assert 2 in ids_of(sel)                  # узкое окно расширяется явно
    assert 4 not in ids_of(sel)              # общий window_sec=60 всё ещё режет


def test_default_no_env():
    os.environ.pop("EXPORT_CONTEXT_WINDOW", None)
    sel, _ = build_dialog(MSGS, T)
    ids = ids_of(sel)
    assert 1 in ids and 2 in ids and 4 not in ids   # дефолт 120с: 15мин=900с вне окна
