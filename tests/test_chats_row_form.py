"""Чаты админки: формат М.Д.ГГГГ, активная галка анкет, кнопка на всю строку."""
import re


def test_date_format_m_d_yyyy():
    from app.admin.app import _parse_date_userfmt as f
    # М.Д.ГГГГ — основной формат (день.месяц.год)
    v, e = f("11.3.2026"); assert e is None and v.month == 3 and v.day == 11
    v, e = f("3.11.2026"); assert e is None and v.month == 11 and v.day == 3
    v, e = f("03.11.2026"); assert e is None and v.month == 11  # с нулём ок
    # обратная совместимость старых форматов
    v, e = f("2026-11-03"); assert e is None
    # ошибки валидации
    _, e = f("2026.11.03"); assert e and "М.Д.ГГГГ" in e
    _, e = f("abc");        assert e and "М.Д.ГГГГ" in e
    _, e = f("9.9.2099");   assert e and "будущего" in e
    _, e = f("1.1.2014");   assert e and "ранняя" in e


def test_template_row_form_and_active_qa_checkbox():
    src = open("app/admin/templates/admin/chats.html", encoding="utf-8").read()
    assert "placeholder='М.Д.ГГГГ'" in src
    assert "ДД.ММ.ГГГГ" not in src
    # чекбокс анкет активен: ни disabled, ни {% if guild_id %} вокруг него
    qa = re.search(r"<input[^>]*name='collects_qa'[^>]*>", src).group(0)
    assert "disabled" not in qa and "guild_id" not in qa
    # одна кнопка на строку: form id=row-<ID>, чекбоксы привязаны form=...
    assert "id='row-{{ r.channel_id }}'" in src
    assert "form='row-{{ r.channel_id }}'" in src
    assert "chat_save_row" in src
    # мёртвый JS-гейт анкет удалён
    assert "Анкеты включаются только внутри сервера" not in src
    # подтверждение удаления
    assert "confirm('Удалить чат" in src


def test_route_registered():
    import inspect
    import app.admin.app as m
    src = inspect.getsource(m)
    assert '"/chats/row/<int:channel_id>"' in src
    assert "def chat_save_row" in src
