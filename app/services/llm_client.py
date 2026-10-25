"""Клиент бесплатного LLM API для автоматического обновления психопрофилей.

Выбор провайдера (по качеству анализа личности среди бесплатных на 2026 год):
OpenRouter — единый OpenAI-совместимый шлюз к моделям с суффиксом ``:free``
(бесплатные тарифы без кредитов). По умолчанию используется
``deepseek/deepseek-chat-v3.1:free`` — DeepSeek стабильно лучше всех
бесплатных моделей держит строгий JSON-формат и рассуждает по большим
транскриптам переписки (а нам нужны именно оба качества). При желании
модель меняется одной строкой в .env (LLM_MODEL), провайдер — тоже.

Настройки берутся из окружения/.env:

  LLM_API_KEY   — ключ OpenRouter (https://openrouter.ai/keys, бесплатно);
                  без ключа обновление невозможно (понятная ошибка в админке)
  LLM_BASE_URL  — по умолчанию https://openrouter.ai/api/v1 (можно заменить
                  на любой OpenAI-совместимый endpoint, напр. локальный ollama)
  LLM_MODEL     — по умолчанию deepseek/deepseek-chat-v3.1:free

Ответ модели парсится analysis_importer.parse_llm_answer (толерантно к
markdown-обёрткам и пояснениям вокруг JSON) и сохраняется как новая версия
профиля через import_analysis.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

import httpx

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-chat-v3.1:free"


class LlmError(RuntimeError):
    """Ошибка вызова LLM API (недоступность, лимит, некорректный ответ)."""


def get_settings() -> dict:
    """api_key / base_url / model из окружения (.env подгружается коллектором)."""
    try:  # окружение может быть не загружено (админка запускается отдельно)
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # noqa: BLE001
        pass
    return {
        "api_key": (os.getenv("LLM_API_KEY") or "").strip(),
        "base_url": (os.getenv("LLM_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/"),
        "model": (os.getenv("LLM_MODEL") or DEFAULT_MODEL).strip(),
    }


def extract_json(text: str) -> str:
    """Вынуть JSON-объект из ответа модели (снимает markdown-обёртки ```...```)."""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    if m:
        return m.group(1)
    s, e = t.find("{"), t.rfind("}")
    if s != -1 and e > s:
        return t[s:e + 1]
    return t


# обратная совместимость со старым приватным именем
_extract_json = extract_json


def chat(prompt: str, *, temperature: float = 0.3, max_tokens: int | None = None,
         timeout: float = 300.0, retries: int = 3) -> tuple[str, str]:
    """Один запрос chat/completions. Возвращает (ответ_текст, модель).

    Ретраи с экспоненциальным backoff на 429/5xx и сетевых сбоях.
    Лимит генерации настраивается через LLM_MAX_TOKENS (по умолчанию 4096).
    """
    if max_tokens is None:
        try:
            max_tokens = int(os.getenv("LLM_MAX_TOKENS") or 4096)
        except ValueError:
            max_tokens = 4096
    cfg = get_settings()
    if not cfg["api_key"]:
        raise LlmError(
            "Не задан LLM_API_KEY. Получите бесплатный ключ на "
            "https://openrouter.ai/keys и добавьте в .env (см. README).")
    url = f"{cfg['base_url']}/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
        # необязательные заголовки OpenRouter для статистики приложения
        "HTTP-Referer": "https://github.com/hale040/discord_prifiler",
        "X-Title": "discord_profiler_admin",
    }
    body = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            r = httpx.post(url, headers=headers, json=body, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                last_err = LlmError(f"HTTP {r.status_code}: {r.text[:300]}")
            elif r.status_code >= 400:
                raise LlmError(f"HTTP {r.status_code}: {r.text[:500]}")
            else:
                data = r.json()
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                text = msg.get("content") or ""
                if not text.strip() and msg.get("reasoning"):
                    # reasoning-модели иногда кладут текст в отдельное поле
                    text = str(msg["reasoning"])
                if not text.strip():
                    raise LlmError(f"Пустой ответ модели {data.get('model', cfg['model'])}.")
                # P1-fix: явный сигнал обрезки ответа по лимиту токенов —
                # иначе битый JSON молча уходит в parse_llm_answer
                if choice.get("finish_reason") == "length":
                    raise LlmError(
                        f"Ответ модели обрезан по max_tokens={max_tokens} "
                        "(finish_reason=length): профиль не сохранён, "
                        "уменьшите размер дела или увелижьте LLM_MAX_TOKENS.")
                return text, str(data.get("model") or cfg["model"])
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_err = e
        if attempt < retries - 1:
            time.sleep(min(2 ** attempt * 2, 30))
    raise LlmError(f"LLM API недоступен после {retries} попыток: {last_err}")


def analyze_prompt(prompt_md: str) -> tuple[dict, str, str]:
    """Прогнать промпт-«дело» через модель. Возвращает (answer_dict, model, prompt_hash)."""
    raw, model_used = chat(prompt_md)
    from app.services.analysis_importer import parse_llm_answer

    answer = parse_llm_answer(_extract_json(raw))
    ph = hashlib.sha256(prompt_md.encode("utf-8")).hexdigest()[:32]
    return answer, model_used, ph
