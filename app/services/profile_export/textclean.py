"""app.services.profile_export.textclean — часть модуля экспорта «дел» для LLM.

Автономный экспорт из PostgreSQL (SQLAlchemy + stdlib), без Discord.
Сгенерирован разделением export_profiles.py; публичный CLI — export_profiles.py.
"""
from __future__ import annotations

import re
from .stats import URL_RE


PII_PATTERNS = [
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[email]"),
    # телефон ДО IP; leading-digit вариант требует пробела до и после, чтобы
    # не съесть ISO-дату в [2026-10-19 13:01] (там скобка сразу после цифр)
    (re.compile(r"\+\d[\d \-.()]{6,}\d|(?<= )\d[\d \-.()]{8,}\d(?= |$)"),
     lambda m: "[phone]" if sum(c.isdigit() for c in m.group()) >= 7 else m.group()),
    (re.compile(r"(?<![\d.])\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![\d.])"), "[ip]"),
    (re.compile(r"(?:^|\s)(?:[A-Z]{2}\d{10,14}|[a-z]{2}\d{9,12})(?:\s|$)"), "[iban]"),
]


def redact(text: str) -> str:
    for pat, repl in PII_PATTERNS:
        text = pat.sub(repl, text)
    return text


def sanitize(value, redact_pii: bool):
    """Рекурсивная чистка строковых полей профиля."""
    if isinstance(value, str):
        return redact(value) if redact_pii else value
    if isinstance(value, list):
        return [sanitize(v, redact_pii) for v in value]
    if isinstance(value, dict):
        return {k: sanitize(v, redact_pii) for k, v in value.items()}
    return value


def strip_urls_deep(obj):
    """Рекурсивно удаляет из структуры значения-ссылки (http/https) — в промпт
    LLM не должны попадать URL картинок/аватаров (мёртвые токены CDN и лишние
    токены модели). Полные досье с url остаются в JSON-выгрузке.

    Поле "transcript" — многострочный текст ленты: его НЕ трогаем (URL из
    строк чата вырезаются отдельно функцией strip_urls_text; раньше deep-strip
    удалял весь транскрипт целиком, если в любой строке была ссылка, —
    нейросеть получала «(нет сообщений)»)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "transcript" and isinstance(v, str):
                out[k] = v
                continue
            nv = strip_urls_deep(v)
            if isinstance(nv, str) and URL_RE.search(nv):
                continue
            out[k] = nv
        return out
    if isinstance(obj, list):
        return [strip_urls_deep(x) for x in obj]
    return obj


def strip_urls_text(text: str) -> str:
    """Убирает голые http(s)-ссылки из текста (картинки/CDN-URL не нужны LLM;
    сам факт вложения остаётся виден по флагу [img])."""
    return re.sub(r"https?://\S+", "", text or "")


# анти-инъекционная нормализация (AI prompt injection defense):
# 1) закрытые токены моделей — вырезаются целиком, чтобы текст сообщения
#    не мог «закрыть» системный блок и начать свой;
_INJECT_TOKENS_RE = re.compile(
    r"</?\s*(?:system|assistant|user|instructions?|prompt)\b[^>]*>",
    re.IGNORECASE)
# 2) markdown-заголовки внутри пользовательских строк понижаются до «▍» —
#    лента не может создавать ##-секции, конкурирующие с настоящей структурой
#    промпта;
# 3) управляемые символы (включая zero-width и direction-override, которыми
#    маскируют скрытые команды) заменяются на пробел.
_MD_HEAD_RE = re.compile(r"^\s*#{1,6}\s+", re.MULTILINE)
_CTRL_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f"
    # zero-width, bidi embed/override/isolate (маскировка скрытых команд),
    # soft hyphen, mongolian VS, hangul filler, BOM
    r"\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f"
    r"\u00ad\u180e\u3164\ufeff]")


def anti_inject(text: str) -> str:
    """Безусловная (не зависит от redact_pii) нормализация любого текста,
    попадающего в промпт: user content, анкеты, комментарии, имена."""
    if not text:
        return text or ""
    t = _INJECT_TOKENS_RE.sub("", text)
    t = _MD_HEAD_RE.sub("▍", t)
    t = _CTRL_RE.sub(" ", t)
    return t
