# Discord Chat Profiler (userbot)

Сервис-процесс, подключающийся к Discord **от имени пользователя** (user-token)
для периодического сбора данных из целевого чата: все сообщения, картинки/вложения,
полные профили участников, роли/теги — всё для последующего профайлинга и
определения психотипов. **Пока только сбор данных.**

> ⚠️ **Legal/ToS warning:** использование user-token'а (self-bot) нарушает
> Terms of Service Discord и может привести к блокировке аккаунта. Используйте
> только на собственном аккаунте и с пониманием рисков. Также соблюдайте
> законодательство о защите персональных данных вашей юрисдикции.

## Стек

- Python 3.12
- [discord.py-self](https://pypi.org/project/discord.py-self/) — форк discord.py
  для подключения по пользовательскому токену (импортируется как `discord`)
- SQLAlchemy 2.x + SQLite (WAL)
- Архитектура MVC

## Структура (MVC)

```
main.py                        # точка входа процесса (сигналы, graceful shutdown)
export_profiles.py             # автономный экспорт «дел» для LLM-профайлинга (без Discord)
app/
├── config.py                  # настройки из ENV (.env.example)
├── models/                    # MODEL
│   ├── entities.py            #   SQLAlchemy-модели: профили, роли/теги, сообщения, вложения, курсоры
│   ├── db.py                  #   движок SQLite (WAL), create_all
│   └── repository.py          #   репозиторий: upsert/dedup/state/stats
├── views/                     # VIEW
│   └── console_view.py        #   консольный вывод прогресса + настройка логов
├── controllers/               # CONTROLLER
│   └── collector_controller.py#   оркестрация: события клиента -> сервисы -> view
├── services/
│   ├── discord_gateway.py     #   UserbotClient (discord.py-self): логин, profile, member
│   ├── collection_service.py  #   поллер канала, backfill истории, профилирование, загрузка картинок
│   └── mappers.py             #   discord-объекты -> dict для БД (+ метрики для психотипирования)
└── utils/helpers.py           #   human_delay, JSON-сериализация, snowflake
```

## Что собирается

| Данные | Источник | Где хранится |
|---|---|---|
| Все сообщения (текст, правки, pinned, replies, stickers, реакции, embeds) | `channel.history()` пагинацией | `messages` (+ raw_json) |
| Картинки и прочие вложения | CDN (`get_from_cdn`) — скачивание **только при явном `DOWNLOAD_ATTACHMENTS=1`** в .env (по умолчанию выключено) | файлы в `data/attachments/…`, метаданные+sha256 в `attachments` |
| Профили пользователей: username, bio, pronouns-поля, avatar/banner, accent color, дата регистрации, flags | `/users/{id}/profile` (доступен только user-токену) | `user_profiles` |
| Бейджи, connections (соцсети), mutual guilds/friends | профиль | `badges_json`, `connections_json`, `raw_json` |
| Серверные данные: ник, joined_at, boost, timeout, права | `guild.fetch_member` | `guild_member_profiles` |
| Роли/теги пользователя | Member.roles | `user_role_tags` |
| Метрики текста (word/char count, хэштеги, упоминания) | вычисляются при маппинге | `messages` |

## «Как пользователь, а не бот»

- Логин по **user-token** через `discord.Client` (discord.py-self) — gateway
  сообщает обычный client_type/properties, а не bot-библиотеку.
- Только чтение: никаких команд, реакций, отправки сообщений.
- История запрашивается маленькими страницами (`before=cursor`, 25–50 сообщений) —
  как скролл ленты в UI; новые сообщения добираются `after=cursor`.
- Между всеми запросами — случайные human-like паузы (`REQUEST_DELAY_MIN..MAX`,
  редкие «задумчивые» паузы до 3x).
- Периодический опрос целевого канала раз в `POLL_INTERVAL_SEC` (±джиттер).
- Live-событие MESSAGE_CREATE используется лишь чтобы не ждать следующего интервала.

## Запуск

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # заполнить DISCORD_TOKEN, CHANNEL_ID и (опционально) START_DATE
python main.py              # Ctrl+C / SIGTERM — корректное завершение
```

### Дата начала сбора (START_DATE)

В `.env` задаётся `START_DATE` — собирать сообщения только с этой даты (UTC):

```
START_DATE=2024-01-01            # только дата
START_DATE=2024-01-01 12:30      # дата + время
START_DATE=                      # пусто (или 0/all) — вся история
```

Работает в двух уровнях:
- **Экономия запросов:** backfill стартует сразу с snowflake-курсора,
  вычисленного из даты (`(ms - Discord epoch) << 22`), — история до этой
  даты у API вообще не запрашивается.
- **Фильтр на записи:** любое сообщение (backfill, poll, live-событие) со
  `created_at < START_DATE` отбрасывается и не попадает в БД.

Прогресс — в консоли (`HISTORY/POLL/PROFILE/MEDIA/STATS`), данные — в
`data/profiling.db` (SQLite) и `data/attachments/`.

## Экспорт для нейросетевого профайлинга

После накопления данных выгрузите подготовленные «дела» пользователей:

```bash
python export_profiles.py                 # все пользователи -> out/dataset.jsonl + out/prompts/*.md
python export_profiles.py fluff           # один пользователь (username/часть имени/id)
python export_profiles.py --per-user-json --min-messages 10 --redact-pii
# лимит строк чата задаётся в .env (EXPORT_MAX_LINES) или флагом:
python export_profiles.py fluff --max-lines 300   # 300 самых СВЕЖИХ строк диалога
```

Каждое «дело» содержит: профиль (bio, бейджи, connections, флаги), серверные атрибуты
(ник, роли/теги, дата вступления, таймауты), статистику коммуникации (активность по
часам/дням, доли вопросов/эмодзи/CAPS, bursts, лексические маркеры неуверенности/
абсолютизма/извинений), медиа-сигналы и хронологический транскрипт сообщений.
Лимит ленты измеряется КОЛИЧЕСТВОМ СТРОК ЧАТА (не символами — строки текста никогда
не обрезаются посередине): `EXPORT_MAX_LINES` в `.env` (или флаг `--max-lines N`)
оставляет только самые СВЕЖИЕ N строк — срез идёт от нового к старому, старые дни
отбрасываются. `0`/пусто = полная лента без среза. Факт среза честно отражён
в промпте и в `index.json` (`transcript_truncated`, `line_limit`). Для бесплатных
LLM с маленьким контекстом рекомендуется EXPORT_MAX_LINES≈300.
Для каждого пользователя генерируется готовый промпт `out/prompts/<id>.md` с
системной инструкцией и JSON-схемой ответа (Big Five, MBTI, эннеаграмма, стиль
общения, интересы, риски, цитаты-обоснования). Флаг `--redact-pii` вырезает
email/телефоны/IP из текста перед экспортом. Каталог `out/` в `.gitignore` —
это персональные данные.

## Схема БД (основное)

- `user_profiles` — глобальный профиль (bio, badges, connections, flags, raw_json)
- `guild_member_profiles` — серверный профиль (nick, joined_at, timeout, permissions)
- `user_role_tags` — теги/роли пользователя
- `messages` — сообщения + метрики (word_count, hashtag_count, mentions, reactions…),
  связи диалога: `reference_id` (на какое сообщение ответ) и `reply_to_author_id`
  (кому отвечает — автор родителя; достраивается фоном из reply-графа, работает
  даже если родительское сообщение удалено/вне периода сбора)
- `attachments` — вложения (url, sha256, stored_path, downloaded)
- `channel_states` — курсор last_message_id для инкрементального сбора (resume-safe)

Повторный запуск продолжает сбор с сохранённого курсора; дедупликация по
snowflake ID сообщений и вложений.

## Компактный экспорт диалогов для LLM

Связи «кто кому отвечает» строятся автоматически (каждая страница backfill и
цикл статистики вызывают `Repository.link_reply_references()`), а также
однократно для уже накопленных данных:

```bash
python -c "from app.config import Settings; from app.models.db import init_db; \
from app.models.repository import Repository; \
print(Repository(init_db(Settings())).link_reply_references())"
```

Выгрузка диалога в токено-экономичном формате (метки U1/U2…, имена — один раз
в шапке, ответы — `>N`, правка/удаление — флаги `e/d`, вложения — `[img]`):

```bash
# весь диалог канала вокруг целевого пользователя (с ответами собеседников)
python -m app.services.export_service --user fluffy_rat_69 -o dialog.txt

# фильтры: период, канал, лимит, обрезка длинных реплик
python -m app.services.export_service --user-id 123456789 --since 2024-01-01 --channel 123 --max-len 150

# только реплики целевого пользователя (максимально компактно, но без контекста)
python -m app.services.export_service --user fluffy_rat_69 --target-only
```

Пример вывода:

```
# target=Fluffy(111) span=2026-10-01..2026-10-01 msgs=6
# U1=Fluffy, U2=partner_x, U3=guest_y
# формат: 'U№ ЧЧ:ММ [>N|@Uk][ed] текст [img]', где >N — ответ на реплику N, e/d — edit/delete

# 2026-10-01
U2 12:00: Кофе будешь?
U1 12:01: >1 давай, и фотоотчет скинь
U1 12:03: @U3 а ты что молчишь?
U2 12:04: >2 не дождешься
U1 12:05: e Огонь горит уже так жарко… [img]
```

`>N` — ответ на реплику №N этого же экспорта; `@Uk` — адресат, если
родительского сообщения нет в выборке (удалено/вне периода). Пингующие
`<@id>` из текста вырезаны — роль участника полностью описывается связями.
