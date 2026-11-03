"""Конвертация анкет chat_members.questionnaire из «сырого текста» в JSON.

diag_stats считает questionnaires_filled по правилу «не пусто и НЕ LIKE '{%'»,
а questionnaire.fetch_questionnaires читает поле как структурированные данные.
Анкеты, сохранённые старыми путями (raw-текст поста или ручное редактирование),
падают в questionnaires_nonjson и не попадают в промпт.

Формат JSON: {"v": 1, "text": "<исходный текст>", "answers": []}
Идемпотентно: строки, уже начинающиеся с '{', пропускаются.

Запуск:
    python scripts/fix_nonjson_questionnaires.py            # dry-run (что изменится)
    python scripts/fix_nonjson_questionnaires.py --apply    # применить
"""
from __future__ import annotations

import json
import os
import sys


def main() -> int:
    apply_changes = "--apply" in sys.argv[1:]
    url = os.environ.get("DATABASE_URL")
    if not url:
        try:
            from dotenv import load_dotenv
            load_dotenv()
            url = os.environ.get("DATABASE_URL")
        except ImportError:
            pass
    if not url:
        print("[FATAL] DATABASE_URL не задан (.env или окружение)", file=sys.stderr)
        return 2

    from sqlalchemy import create_engine, text

    sync_url = url.replace("postgresql+asyncpg://", "postgresql://") \
                  .replace("postgres://", "postgresql://")
    engine = create_engine(sync_url, future=True)

    sel = text(
        "SELECT id, author_id, channel_id, questionnaire FROM chat_members "
        "WHERE questionnaire IS NOT NULL "
        "  AND btrim(questionnaire) <> '' "
        "  AND btrim(questionnaire) NOT LIKE '{%'"
    )
    upd = text("UPDATE chat_members SET questionnaire = CAST(:js AS TEXT) "
               "WHERE id = :id")

    changed = 0
    with engine.connect() as conn:
        rows = conn.execute(sel).mappings().all()
        for r in rows:
            js = json.dumps({"v": 1, "text": r["questionnaire"], "answers": []},
                            ensure_ascii=False)
            print(f"id={r['id']} author={r['author_id']} "
                  f"preview={r['questionnaire'][:60]!r}")
            if apply_changes:
                conn.execute(upd, {"js": js, "id": r["id"]})
            changed += 1
        if apply_changes and changed:
            conn.commit()

    mode = "APPLIED" if apply_changes else "DRY-RUN"
    print(f"# {mode}: nonjson анкет найдено: {changed}")
    if changed and not apply_changes:
        print("# перезапустите с --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
