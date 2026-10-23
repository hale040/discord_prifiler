"""Импорт ответа нейросети-аналитика в таблицы psych_profiles/psych_signals/user_interests.

Полный JSON ответа хранится в psych_profiles.result_json без разбивки;
ключевые метрики дублируются в колонки. Дисклеймер НЕ сохраняется —
вместо него фиксируются период анализа и число записей (period_from/to,
messages_analyzed). Сигналы (evidence) пишутся атомарно с дедупом по
нормализованной цитате, интересы накапливаются между прогонами.

CLI:
    python -m app.services.analysis_importer --user <user_id> --model deepseek \
        --json-file answer.json [--db postgresql+psycopg://user:pass@host:5682/db] \
        [--period-from 2026-09-28 --period-to 2026-10-20 --messages 1058]
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone

from sqlalchemy import JSON, bindparam, text
from sqlalchemy.engine import Engine

MAX_SIGNALS = 10          # не забиваем базу: evidence не больше N на профиль
MAX_QUOTE_CHARS = 200     # длина цитаты в сигналах


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Парсинг/валидация ответа LLM                                                #
# --------------------------------------------------------------------------- #

def parse_llm_answer(raw: str) -> dict:
    """Достаёт JSON из ответа модели (терпим к markdown-обёрткам ```json …```)."""
    s = raw.strip()
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", s, re.S)
    if m:
        s = m.group(1)
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("В ответе не найден JSON-объект")
    data = json.loads(s[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("Ответ должен быть JSON-объектом")
    return data


def _slug(value: str) -> str:
    v = unicodedata.normalize("NFKD", (value or "").lower())
    v = re.sub(r"[^a-zа-я0-9]+", "-", v).strip("-")
    return v[:64] or "signal"


def _norm_quote(value: str) -> str:
    v = re.sub(r"\s+", " ", (value or "")).strip().lower()
    v = re.sub(r"[^\w\sа-яё]", "", v, flags=re.U)
    return v[:128]


def _as_float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # big_five допускаем и 0..1, и 0..100 — нормируем к 0..1
    return min(max(f / 100.0 if f > 1.0 else f, 0.0), 1.0)


# --------------------------------------------------------------------------- #
# Валидация структуры ответа                                                   #
# --------------------------------------------------------------------------- #

_MBTI_RE = re.compile(r"^[EI][NS][TF][JP]$")
_ENN_RE = re.compile(r"^([1-9]|1[0-2])([-w])?$")
_SEVERITIES = {"low", "med", "medium", "high"}


def validate_answer(data: dict) -> tuple[list[str], list[str]]:
    """Проверить структуру ответа модели по схеме промпта.

    Возвращает (errors, warnings). errors — то, что ломает импорт или говорит
    о том, что ответ не по схеме; warnings — поля, которые импорт терпит,
    но их стоит проверить глазами.
    """
    errs: list[str] = []
    warns: list[str] = []
    if not isinstance(data, dict):
        return (["корень JSON должен быть объектом {...}"], [])

    def _num(obj: dict, key: str, path: str, *, required: bool) -> None:
        v = obj.get(key)
        if v is None:
            if required:
                errs.append(f"{path}: отсутствует обязательное числовое поле")
            return
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            errs.append(f"{path}: ожидалось число, получено {type(v).__name__}")
            return
        if not 0 <= float(v) <= 100:
            errs.append(f"{path}: значение {v} вне диапазона 0..100 "
                        "(допустимы шкалы 0..1 и 0..100)")

    # --- big_five -------------------------------------------------------------
    bf = data.get("big_five")
    if not isinstance(bf, dict):
        errs.append("big_five: ожидается объект с 5 ключами")
    else:
        for k in ("openness", "conscientiousness", "extraversion",
                  "agreeableness", "neuroticism"):
            _num(bf, k, f"big_five.{k}", required=True)

    # --- mbti / enneagram -------------------------------------------------------
    for block, rx, label in (("mbti", _MBTI_RE, "MBTI"),
                             ("enneagram", _ENN_RE, "эннеаграмма")):
        o = data.get(block)
        if not isinstance(o, dict):
            errs.append(f"{block}: ожидается объект {{type, confidence, ...}}")
            continue
        t = o.get("type")
        if not isinstance(t, str) or not t.strip():
            errs.append(f"{block}.type: ожидается непустая строка ({label})")
        elif not rx.match(t.strip().upper()):
            warns.append(f"{block}.type: «{t}» не похоже на канонический код "
                         f"{label} — импортируется как есть")
        _num(o, "confidence", f"{block}.confidence", required=False)
        alt = o.get("alt")
        if alt is not None and not isinstance(alt, list):
            warns.append(f"{block}.alt: ожидается список альтернатив")

    # --- interests --------------------------------------------------------------
    it = data.get("interests")
    if it is None:
        warns.append("interests: поле отсутствует (импорт пройдёт без интересов)")
    elif not isinstance(it, list):
        errs.append("interests: ожидается список [{topic, evidence}, ...]")
    else:
        for i, x in enumerate(it):
            if not isinstance(x, dict) or not str((x or {}).get("topic") or "").strip():
                errs.append(f"interests[{i}]: у элемента должен быть непустой topic")

    # --- risk_flags ---------------------------------------------------------------
    rf = data.get("risk_flags")
    if rf is not None:
        if not isinstance(rf, list):
            errs.append("risk_flags: ожидается список")
        else:
            for i, x in enumerate(rf):
                sev = str((x or {}).get("severity") or "").lower()
                if sev and sev not in _SEVERITIES:
                    warns.append(f"risk_flags[{i}].severity: «{sev}» — ожидались "
                                 "low/med/high")

    # --- evidence -----------------------------------------------------------------
    ev = data.get("evidence")
    if ev is None:
        warns.append("evidence: поле отсутствует (сигналы не будут сохранены)")
    elif not isinstance(ev, list):
        errs.append("evidence: ожидается список [{signal, quote}, ...]")
    else:
        nq = 0
        for i, x in enumerate(ev):
            if not isinstance(x, dict) or not str(x.get("quote") or "").strip():
                errs.append(f"evidence[{i}]: цитата quote обязательна и непуста")
                continue
            nq += 1
        if nq and len(ev) > MAX_SIGNALS:
            warns.append(f"evidence: {len(ev)} элементов — в базу попадут "
                         f"только первые {MAX_SIGNALS} (дедуп по цитате)")

    # --- confidence_overall ---------------------------------------------------------
    _num(data, "confidence_overall", "confidence_overall", required=False)

    if "disclaimer" not in data:
        warns.append("disclaimer: отсутствует (не влияет на импорт — он не сохраняется)")
    return errs, warns


# --------------------------------------------------------------------------- #
# Импорт                                                                       #
# --------------------------------------------------------------------------- #

def import_analysis(
    engine: Engine,
    user_id: int,
    answer: dict | str,
    *,
    model: str | None = None,
    prompt_hash: str | None = None,
    source_lines: int | None = None,
    period_from: datetime | str | None = None,
    period_to: datetime | str | None = None,
    messages_analyzed: int | None = None,
) -> int:
    """Сохранить ответ LLM как новую версию профиля. Возвращает profile_id."""
    if isinstance(answer, str):
        answer = parse_llm_answer(answer)
    bf = answer.get("big_five") or {}
    mbti = answer.get("mbti") or {}
    enn = answer.get("enneagram") or {}
    risks = answer.get("risk_flags") or []
    sev_rank = {"high": 3, "med": 2, "medium": 3, "low": 1}
    severity_max = None
    best = 0
    for r in risks:
        sev = str((r or {}).get("severity") or "").lower()
        if sev_rank.get(sev, 0) > best:
            best, severity_max = sev_rank[sev], ("high" if sev in ("high", "medium") else sev)

    def _dt(v):
        if v is None or isinstance(v, datetime):
            return v.replace(tzinfo=timezone.utc) if isinstance(v, datetime) and v.tzinfo is None else v
        try:
            return datetime.fromisoformat(str(v))
        except ValueError:
            return None

    with engine.begin() as conn:
        # старые версии -> неактивные
        conn.execute(
            text("UPDATE psych_profiles SET is_current = FALSE WHERE user_id = :u AND is_current = TRUE"),
            {"u": user_id},
        )
        insert_sql = (
            text(
                    """INSERT INTO psych_profiles
                   (user_id, model, prompt_hash, source_lines, period_from, period_to,
                    messages_analyzed, big_five_openness, big_five_conscientiousness,
                    big_five_extraversion, big_five_agreeableness, big_five_neuroticism,
                    mbti_type, mbti_confidence, enneagram_type, enneagram_confidence,
                    confidence_overall, severity_max, result_json, is_current, created_at)
                   VALUES (:user_id,:model,:ph,:sl,:pf,:pt,:ma,:bo,:bc,:be,:ba,:bn,
                           :mt,:mc,:et,:ec,:co,:sm,:rj,TRUE,:ca)
                    RETURNING id"""
            )
            # rj — dict для нативной колонки JSONB: без явного типа
            # DBAPI-драйвер не умеет адаптировать dict ("can't adapt type 'dict'")
            .bindparams(bindparam("rj", type_=JSON(none_as_null=True)))
        )
        res = conn.execute(insert_sql, {
                "user_id": user_id, "model": model, "ph": prompt_hash, "sl": source_lines,
                "pf": _dt(period_from), "pt": _dt(period_to), "ma": messages_analyzed,
                "bo": _as_float(bf.get("openness")), "bc": _as_float(bf.get("conscientiousness")),
                "be": _as_float(bf.get("extraversion")), "ba": _as_float(bf.get("agreeableness")),
                "bn": _as_float(bf.get("neuroticism")),
                "mt": (mbti.get("type") or "")[:8] or None, "mc": _as_float(mbti.get("confidence")),
                "et": (enn.get("type") or "")[:8] or None, "ec": _as_float(enn.get("confidence")),
                "co": _as_float(answer.get("confidence_overall")),
                "sm": severity_max,
                # дисклеймер в хранилище не нужен — вырезаем из JSON
                # result_json — нативный JSONB: psycopg принимает dict как есть
                "rj": {k: v for k, v in answer.items() if k != "disclaimer"},
                "ca": _utcnow(),
            },
        )
        # psycopg не поддерживает lastrowid; у text()-запроса нет и
        # inserted_primary_key — PK читаем из RETURNING id (scalar()).
        profile_id = res.scalar()

        # ---- сигналы: атомарно, с дедупом, лимит MAX_SIGNALS ----
        n_sig = 0
        for ev in (answer.get("evidence") or [])[:MAX_SIGNALS]:
            quote = str((ev or {}).get("quote") or "")[:MAX_QUOTE_CHARS].strip()
            if not quote:
                continue
            sig = _slug(str(ev.get("signal") or ""))
            qn = _norm_quote(quote)
            mid = conn.execute(
                text("SELECT id FROM messages WHERE author_id = :u AND content LIKE :q LIMIT 1"),
                {"u": user_id, "q": f"%{quote[:60]}%"},
            ).scalar()
            ins = conn.execute(
                text(
                    """INSERT INTO psych_signals
                       (profile_id, user_id, signal_key, quote_norm, quote, message_id,
                        context, severity, created_at)
                       VALUES (:p,:u,:sk,:qn,:q,:m,:c,:s,:t)
                       ON CONFLICT (user_id, signal_key, quote_norm) DO NOTHING"""
                ),
                {"p": profile_id, "u": user_id, "sk": sig, "qn": qn, "q": quote,
                 "m": mid, "c": str(ev.get("context") or "")[:MAX_QUOTE_CHARS],
                 "s": None, "t": _utcnow()},
            )
            n_sig += ins.rowcount or 0

        # ---- интересы: merge (накапливаются между срезами диалога) ----
        for it in (answer.get("interests") or []):
            topic = str((it or {}).get("topic") or "").strip()
            if not topic:
                continue
            tn = _norm_quote(topic) or _slug(topic)
            exists = conn.execute(
                text("SELECT id, evidence_count FROM user_interests WHERE user_id = :u AND topic_norm = :n"),
                {"u": user_id, "n": tn},
            ).first()
            if exists:
                conn.execute(
                    text("UPDATE user_interests SET evidence_count = evidence_count + 1, "
                         "evidence = :e, last_seen_at = :t WHERE id = :id"),
                    {"e": str(it.get("evidence") or "")[:MAX_QUOTE_CHARS], "t": _utcnow(), "id": exists[0]},
                )
            else:
                conn.execute(
                    text("INSERT INTO user_interests (user_id, topic, topic_norm, evidence, "
                         "evidence_count, first_seen_at, last_seen_at) "
                         "VALUES (:u,:tp,:n,:e,1,:t,:t)"),
                    {"u": user_id, "tp": topic, "n": tn,
                     "e": str(it.get("evidence") or "")[:MAX_QUOTE_CHARS], "t": _utcnow()},
                )
    return profile_id


def get_active_profile(engine: Engine, user_id: int) -> dict | None:
    """Активный профиль + топ-сигналы + накопленные интересы — для экспорта."""
    return _get_active_profile(engine.connect(), user_id)


def _get_active_profile(conn, user_id: int) -> dict | None:
    row = conn.execute(
        text("SELECT * FROM psych_profiles WHERE user_id = :u AND is_current = TRUE "
             "ORDER BY created_at DESC LIMIT 1"),
        {"u": user_id},
    ).mappings().first()
    if not row:
        return None
    p = dict(row)
    p["signals_top"] = [
        {"signal": r["signal_key"], "quotes": [r["quote"]]}
        for r in conn.execute(
            text("SELECT signal_key, quote FROM psych_signals WHERE user_id = :u "
                 "ORDER BY created_at DESC LIMIT :n"),
            {"u": user_id, "n": MAX_SIGNALS},
        ).mappings()
    ]
    p["interests"] = [
        {"topic": r["topic"], "evidence_count": r["evidence_count"]}
        for r in conn.execute(
            text("SELECT topic, evidence_count FROM user_interests WHERE user_id = :u "
                 "ORDER BY evidence_count DESC, last_seen_at DESC LIMIT 12"),
            {"u": user_id},
        ).mappings()
    ]
    for key in ("period_from", "period_to", "created_at"):
        v = p.get(key)
        if isinstance(v, datetime):
            p[key] = v.strftime("%Y-%m-%d")
    return p


if __name__ == "__main__":  # CLI
    import argparse
    import os

    from app.models.db import DEFAULT_DATABASE_URL, create_db_engine

    ap = argparse.ArgumentParser(description="Импорт ответа LLM-аналитика в БД")
    ap.add_argument("--db", default=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    ap.add_argument("--user", type=int, required=True, help="user_id")
    ap.add_argument("--model", default=None, help="id модели (deepseek/qwen/claude…)")
    ap.add_argument("--json-file", required=True, help="файл с JSON-ответом нейросети")
    ap.add_argument("--period-from", default=None)
    ap.add_argument("--period-to", default=None)
    ap.add_argument("--messages", type=int, default=None)
    args = ap.parse_args()

    eng = create_db_engine(args.db)
    with open(args.json_file, encoding="utf-8") as fh:
        pid = import_analysis(
            eng, args.user, fh.read(), model=args.model,
            period_from=args.period_from, period_to=args.period_to,
            messages_analyzed=args.messages,
        )
    print(f"OK: импортировано, profile_id={pid}")
