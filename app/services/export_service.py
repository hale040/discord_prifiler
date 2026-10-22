"""Экспорт диалогов в компактном виде для LLM-анализа личности.

Формат спроектирован под экономию токенов:

  * одна строка на сообщение;
  * авторы получают короткие метки U1, U2... (первым — целевой пользователь);
    имя/ник юзера упоминается один раз в шапке;
  * связи ответов — суффикс ``>N`` (ответ на реплику номер N) и/или
    ``@Uk`` (на кого отвечает, если родительского сообщения нет в выборке);
  * служебные пометки: ``(edit)`` — правилось, ``(del)`` — удалено,
    ``[img]``/``[emb]``/``[stk]`` вместо URL вложений;
  * упоминания <@id> вырезаются из текста (роль уже видна из ``>N/@Uk``).

Запуск из корня проекта::

    python -m app.services.export_service --user fluffy_rat_69
    python -m app.services.export_service --user-id 123456789 --since 2024-01-01
    python -m app.services.export_service --target-only -o dialog.txt

Параметры БД берутся из окружения/.env (DATABASE_URL), как у коллектора.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime

# запуск и как «python -m app.services.export_service», и как «python app/services/export_service.py»
if __package__ in (None, ""):
    sys.path.insert(0, os.getcwd())

from sqlalchemy import select  # noqa: E402

from app.config import Settings, _parse_since  # noqa: E402
from app.models.db import init_db  # noqa: E402
from app.models.entities import GuildMemberProfile, Message, UserProfile  # noqa: E402

USER_MENTION_RE = re.compile(r"<@!?(\d+)>")


def _try_load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # noqa: BLE001
        pass


class DialogExporter:
    def __init__(self, db_url: str) -> None:
        # Settings — slots-датакласс без default_factory при __new__,
        # поэтому поля можно задать напрямую; экспортёру нужен только database_url.
        self.settings = Settings.__new__(Settings)
        self.settings.database_url = db_url
        self.sf = init_db(self.settings)

    # ------------------------------------------------------------- выборка
    def fetch_rows(
        self,
        target_user_id: int | None,
        channel_id: int | None,
        since: datetime | None,
        until: datetime | None,
        limit: int,
        target_only: bool,
    ) -> tuple[list[Message], dict[int, str]]:
        """Сообщения для экспорта + имена пользователей.

        По умолчанию берём ВСЕХ участников канала (диалог с ответами
        собеседников). target_only=True — только реплики целевого пользователя
        (менее информативно, но ещё компактнее).
        """
        with self.sf() as s:
            stmt = select(Message)
            if channel_id is not None:
                stmt = stmt.where(Message.channel_id == channel_id)
            if since is not None:
                stmt = stmt.where(Message.timestamp >= since)
            if until is not None:
                stmt = stmt.where(Message.timestamp <= until)
            if target_only and target_user_id is not None:
                stmt = stmt.where(Message.author_id == target_user_id)
            stmt = stmt.order_by(Message.timestamp.asc(), Message.id.asc()).limit(limit)
            rows = list(s.scalars(stmt))
            names = {
                u.user_id: (u.global_name or u.username or f"id{u.user_id}")
                for u in s.scalars(select(UserProfile))
            }
            # серверные ники имеют приоритет для читаемости
            for gp in s.scalars(select(GuildMemberProfile)):
                if gp.nick:
                    names[gp.user_id] = gp.nick
            return rows, names

    # ------------------------------------------------------------ сборка
    @staticmethod
    def _clean_content(text: str) -> str:
        """Максимально компактный текст реплики без потери смысла."""
        t = (text or "").replace("\r\n", "\n").strip()
        # вырезаем ping-упоминания: <@123> -> '' (роль видна из >N/@Uk)
        t = USER_MENTION_RE.sub("", t)
        # склеиваем переносы строк в один пробел — экономим строки вывода
        t = re.sub(r"\s*\n\s*", " ", t)
        t = re.sub(r"\s{2,}", " ", t).strip()
        return t

    def build(
        self,
        rows: list[Message],
        names: dict[int, str],
        target_user_id: int | None,
        max_len: int,
    ) -> tuple[list[str], dict[str, int]]:
        lines: list[str] = []
        stats = {"messages": len(rows), "replies_linked": 0, "edited": 0, "deleted": 0}

        id_to_num: dict[int, int] = {}
        author_ids: list[int] = []
        num = 0
        prev_day: str | None = None

        for m in rows:
            num += 1
            id_to_num[m.id] = num
            if m.author_id not in author_ids:
                author_ids.append(m.author_id)
            day = m.timestamp.strftime("%Y-%m-%d") if m.timestamp else "?"
            if day != prev_day:
                lines.append(f"# {day}")
                prev_day = day

            label = f"U{author_ids.index(m.author_id) + 1}"
            text = self._clean_content(m.clean_content or m.content)
            if not text:
                text = "[msg]"  # пустая реплика — только вложения/стикер
            if max_len and len(text) > max_len:
                text = text[:max_len] + "…"

            meta = [f"{label} {m.timestamp.strftime('%H:%M') if m.timestamp else '?'}:"]
            flags = ""
            reply = ""
            if m.reference_id and m.reference_id in id_to_num and m.reference_id != m.id:
                reply = f">{id_to_num[m.reference_id]}"
                stats["replies_linked"] += 1
            elif m.reply_to_author_id:
                if m.reply_to_author_id not in author_ids:
                    author_ids.append(m.reply_to_author_id)
                rlab = f"U{author_ids.index(m.reply_to_author_id) + 1}"
                reply = f"@{rlab}"
            if reply:
                flags += reply
            if m.edited_at:
                flags += "e"
                stats["edited"] += 1
            if m.deleted_at:
                flags += "d"
                stats["deleted"] += 1
            atts = ""
            if m.has_attachment:
                atts += "[img]"
            if m.embed_count:
                atts += "[emb]"
            if m.stickers_json and m.stickers_json not in ("[]", "null"):
                atts += "[stk]"
            line = " ".join(x for x in (meta[0], flags, text, atts) if x)
            lines.append(line)
        return lines, stats

    # --------------------------------------------------------------- экспорт
    def export(
        self,
        *,
        target_username: str | None,
        target_user_id: int | None,
        channel_id: int | None,
        since: datetime | None,
        until: datetime | None,
        limit: int,
        max_len: int,
        target_only: bool,
    ) -> tuple[str, dict]:
        # резолв целевого пользователя
        with self.sf() as s:
            if target_user_id is None and target_username:
                up = s.scalars(
                    select(UserProfile).where(UserProfile.username == target_username)
                ).first()
                if up is None:
                    raise SystemExit(
                        f"Пользователь '{target_username}' не найден в user_profiles."
                    )
                target_user_id = up.user_id

        rows, names = self.fetch_rows(
            target_user_id, channel_id, since, until, limit, target_only
        )
        if not rows:
            raise SystemExit("Нет сообщений для экспорта (проверьте фильтры).")

        lines, stats = self.build(rows, names, target_user_id, max_len)

        # карта меток авторов (имена — один раз в шапке)
        author_ids: list[int] = []
        for m in rows:
            if m.author_id not in author_ids:
                author_ids.append(m.author_id)
        # целевой пользователь получает U1
        if target_user_id in author_ids:
            author_ids.remove(target_user_id)
            author_ids.insert(0, target_user_id)
        # добавляем «внешних» адресатов из reply-ссылок, не представленных в выборке
        extra: set[int] = set()
        for m in rows:
            if (
                m.reply_to_author_id
                and m.reply_to_author_id not in author_ids
                and m.reference_id not in {r.id for r in rows}
            ):
                extra.add(m.reply_to_author_id)
        author_ids.extend(sorted(extra))

        header: list[str] = []
        tgt_name = names.get(target_user_id or 0, "?")
        first_ts = min((m.timestamp for m in rows if m.timestamp), default=None)
        last_ts = max((m.timestamp for m in rows if m.timestamp), default=None)
        span = (
            f"{first_ts:%Y-%m-%d}..{last_ts:%Y-%m-%d}" if first_ts and last_ts else "?"
        )
        header.append(f"# target={tgt_name}({target_user_id}) span={span} msgs={len(rows)}")
        legend = ", ".join(
            f"U{i + 1}={names.get(uid, f'id{uid}')}" for i, uid in enumerate(author_ids)
        )
        header.append(f"# {legend}")
        header.append("# формат: 'U№ ЧЧ:ММ [>N|@Uk][ed] текст [img]', где >N — ответ на реплику N, e/d — edit/delete")
        return "\n".join(header + [""] + lines) + "\n", stats


def main(argv: list[str] | None = None) -> int:
    _try_load_dotenv()
    p = argparse.ArgumentParser(description="Компактный экспорт диалогов для LLM.")
    p.add_argument("--db", default=os.getenv("DATABASE_URL", ""),
                   help="PostgreSQL URL (по умолчанию из env DATABASE_URL)")
    p.add_argument("--user", help="username целевого пользователя (из user_profiles)")
    p.add_argument("--user-id", type=int, help="snowflake id целевого пользователя")
    p.add_argument("--channel", type=int, help="фильтр по каналу")
    p.add_argument("--since", help="дата начала (UTC): 2024-01-01 или ISO8601")
    p.add_argument("--until", help="дата конца (UTC)")
    p.add_argument("--limit", type=int, default=5000, help="максимум сообщений")
    p.add_argument(
        "--max-len", type=int, default=200,
        help="обрезка длинных реплик (0 — не обрезать)",
    )
    p.add_argument(
        "--target-only", action="store_true",
        help="выводить только реплики целевого пользователя (по умолчанию — весь диалог)",
    )
    p.add_argument("-o", "--out", help="файл результата (по умолчанию stdout)")
    a = p.parse_args(argv)

    exp = DialogExporter(a.db)
    text, stats = exp.export(
        target_username=a.user,
        target_user_id=a.user_id,
        channel_id=a.channel,
        since=_parse_since(a.since or ""),
        until=_parse_since(a.until or ""),
        limit=a.limit,
        max_len=a.max_len,
        target_only=a.target_only,
    )
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Экспортировано: {stats} -> {a.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
