**Зерокодинг тест проект**

# Discord Chat Profiler (userbot)

Сервис-процесс, подключающийся к Discord **от имени пользователя** (user-token)
для периодического сбора данных из целевого чата: все сообщения, картинки/вложения,
полные профили участников, роли/теги, динамические поведенческие сигналы (правки,
реакции, присутствие), а также **история личных сообщений (ЛС) — выборочно, только
для пользователей, помеченных кнопкой в админке**. Всё складывается в PostgreSQL
для последующего профайлинга и определения психотипов (LLM-анализ в один клик).

> ⚠️ **Legal/ToS warning:** использование user-token'а (self-bot) нарушает
> Terms of Service Discord и может привести к блокировке аккаунта. Используйте
> только на собственном аккаунте и с пониманием рисков. Также соблюдайте
> законодательство о защите персональных данных вашей юрисдикции.

## Стек

- Python 3.12
- [discord.py-self](https://pypi.org/project/discord.py-self/) — форк discord.py
  для подключения по пользовательскому токену (импортируется как `discord`)
- SQLAlchemy 2.x + PostgreSQL (psycopg 3)
- Архитектура MVC

## Структура (MVC)

```
main.py                        # точка входа процесса (сигналы, graceful shutdown)
admin_app.py                   # CLI-обёртка админки (запуск; реализация — app/admin/)
export_profiles.py             # CLI-обёртка экспорта (реализация — app/services/profile_export/)
app/
├── config.py                  # настройки из ENV (.env.example)
├── models/                    # MODEL
│   ├── entities.py            #   SQLAlchemy-модели: профили, роли/теги, сообщения, вложения, курсоры
│   ├── db.py                  #   движок PostgreSQL (SQLAlchemy + psycopg), create_all
│   └── repository.py          #   репозиторий: upsert/dedup/state/stats
├── admin/                     # веб-админка (Flask), python -m app.admin
│   ├── app.py                 #   приложение: маршруты, CRUD, SQL-редактор, аудит,
│   │                          #   psych-импорт, переключатель ЛС-сбора (/dm/toggle/<id>)
│   ├── security.py            #   LoginRateLimiter (вход по ADMIN_PASSWORD); CSRF выключен (локальный запуск)
│   ├── bp/context.py          #   AdminContext: таблицы, VIEW_COLUMNS, общие хелперы
│   ├── __main__.py            #   точка входа python -m app.admin
│   └── templates/admin/       #   ВЕСЬ HTML отдельными файлами Jinja2:
│       ├── layout.html        #     каркас страницы (nav, стили, flash);
│       │                      #     таблицы: table-layout:fixed + перенос слов —
│       │                      #     без горизонтального скролла
│       ├── login.html         #     вход по ADMIN_PASSWORD
│       ├── dashboard.html     #     сводка по таблицам
│       ├── browse.html        #     лента таблицы: поиск/фильтры/пагинация;
│       │                      #     действия вынесены в карточку — в списке
│       │                      #     только ссылка на запись и ★ избранное
│       ├── record.html        #     карточка записи: редактирование + ВСЕ действия
│       │                      #     (🧠 психо профиль, 💬 Собрать ЛС / стоп + статус
│       │                      #     синхронизации) + связанные данные
│       ├── sql.html           #     SQL-редактор (SELECT / подтверждение UPDATE-DELETE)
│       ├── audit.html         #     журнал аудита
│       ├── actions.html       #     VACUUM/ANALYZE
│       ├── profile_import.html#     импорт ответа LLM (CLI-формат)
│       ├── psych_manual.html  #     ручная вставка ответа модели с валидацией структуры
│       └── profile_view.html  #     читаемый вид психопрофиля
├── views/                     # VIEW
│   └── console_view.py        #   консольный вывод прогресса + настройка логов
├── controllers/               # CONTROLLER
│   └── collector_controller.py#   оркестрация: события клиента -> сервисы -> view
├── services/
│   ├── discord_gateway.py     #   UserbotClient (discord.py-self): логин, profile, member;
│   │                          #   live-события MESSAGE_EDIT/Delete, REACTION_*, PRESENCE_UPDATE
│   ├── collection_service.py  #   поллер канала, backfill истории, профилирование, загрузка
│   │                          #   картинок + dm-loop: выгрузка ЛС по кнопке в админке
│   ├── mappers.py             #   discord-объекты -> dict для БД (+ метрики для психотипирования)
│   ├── llm_client.py          #   клиент OpenRouter (бесплатные модели) для автосборки профиля
│   ├── analysis_importer.py   #   JSON-ответ LLM -> psych_profiles/signals/interests + validate_answer
│   └── profile_export/        #   экспорт «дел» и промптов (модуль за export_profiles.py)
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
| Правки сообщений (прежний текст, номер правки) | MESSAGE_EDIT + `msg.edits` | `message_revisions` |
| Реакции как события (кто, чем, add/remove) | `on_raw_reaction_add/remove` | `reaction_events` (+ снимок в `messages.reactions_json`) |
| История присутствия (статусы, активности, кастомные статусы) | PRESENCE_UPDATE | `presence_snapshots` |
| Диалогичность A→B (персистентные агрегаты, PG-триггеры) | считаются при записи сообщений | `interaction_aggregates` |
| **ЛС: полная история диалога — только по кнопке в карточке** | dm-loop, `channel.history()` ЛС-канала | `messages` (guild_id=NULL) + курсоры `dm_*` в `user_profiles` |

## Сбор личных сообщений (ЛС) по кнопке

Сообщения из ЛС собираются **не для всех подряд**, а только для тех пользователей,
у которых в **карточке профиля** админки нажата кнопка **«💬 Собрать ЛС»**
(флаг `dm_fetch = TRUE`, переключается POST-эндпоинтом `/dm/toggle/<user_id>`;
повторный клик — «💬 ЛС: стоп»).

- Фоновый цикл `dm-loop` (`CollectionService._dm_loop`) каждые `DM_POLL_INTERVAL_SEC`
  (по умолчанию 300 с) перечитывает список помеченных из БД: включили кнопку —
  на следующем тике диалог начал грузиться; выключили — цикл его пропускает.
- Первый проход выгружает **всю историю** ЛС от новых к старым маленькими
  страницами с human-like паузами и обработкой 429; курсор `dm_last_message_id`
  (самый старый загруженный id) делает процесс resume-safe после рестарта.
- После полной выгрузки проход становится дешёвым one-shot:
  `history(after=dm_first_message_id)` забирает только новые сообщения.
  Принудительная полная перемотка: `DM_FULL_RESCAN=1`.
- Сообщения пишутся в обычную таблицу `messages` (`guild_id = NULL`,
  `channel_id` = id DM-канала) — отдельных таблиц не нужно; дедупликация по
  snowflake ID общая со сбором чата.
- В карточке виден статус: сколько сообщений сохранено (`dm_messages_total`),
  когда синхронизировали (`dm_synced_at`), id канала диалога.

## «Как пользователь, а не бот»

- Логин по **user-token** через `discord.Client` (discord.py-self) — gateway
  сообщает обычный client_type/properties, а не bot-библиотеку.
- Только чтение: никаких команд, реакций, отправки сообщений.
- История запрашивается маленькими страницами (`before=cursor`, 25–50 сообщений) —
  как скролл ленты в UI; новые сообщения добираются `after=cursor`.
- Между всеми запросами — случайные human-like паузы (`REQUEST_DELAY_MIN..MAX`,
  редкие «задумчивые» паузы до 3x).
- Цикл мониторинга: **при запуске проверка новых сообщений выполняется сразу**
  (в т.ч. сразу после завершения backfill истории), далее схема «обход завершён
  → ждём `POLL_INTERVAL_SEC` (по умолчанию 300 = 5 мин, ±джиттер) → новый обход».
- Live-событие MESSAGE_CREATE используется лишь чтобы не ждать следующего интервала.
- Обрыв страницы лимитом API (429/retry_after) больше не теряет сообщения:
  усечённый обход помечается `_poll_incomplete`, и следующий проход выполняется
  немедленно, продолжая с последнего сохранённого id; live-событие впереди
  отстающего курсора (gap после простоя) тоже запускает немедленный догон.
- `messages.raw_json` по умолчанию НЕ пишется (`STORE_RAW_JSON=0`) — полный дамп
  каждого сообщения раздувал базу; при включении хранится в JSONB.

## База данных (PostgreSQL)

Проект работает **только с PostgreSQL** (SQLite больше не поддерживается).
Дефолтный URL из `.env.example`:

```
DATABASE_URL=postgresql+psycopg://profiler:profiler@localhost:5682/profiling
```

То есть: пользователь `profiler`, пароль `profiler`, база `profiling`,
хост `localhost`, **порт 5682**.

### Вариант 1 — Docker Compose (рекомендуется, PostgreSQL 18)

В репозитории лежит `docker-compose.yml` с PostgreSQL 18 (alpine), портом 5682,
паролями из дефолтного `DATABASE_URL` и tuned-параметрами
(`shared_buffers`, `work_mem`, `wal_level=minimal`, `synchronous_commit=off`,
`jit=off`, locale `C.UTF-8`). Роль, база и права создаются образом автоматически.

**Хранение данных:** БД лежит в каталоге `./data/db` рядом с проектом
(bind-mount, добавлен в `.gitignore`). Обратите внимание: начиная с официальных
образов postgres 18 данные хранятся в версионном подкаталоге
(`/var/lib/postgresql/18/docker`), поэтому монтируется именно
`/var/lib/postgresql`, а не `/var/lib/postgresql/data` — иначе контейнер падает
с ошибкой *"there appears to be PostgreSQL data in /var/lib/postgresql/data
(unused mount/volume)"* при апгрейде образа 17→18. Если у вас остался старый
named-volume `pgdata` от прошлой конфигурации — переносить его нельзя (в нём
формат PG≤17), просто удалите volume и начните новую базу: схема создаётся
заново при первом запуске `main.py`.

```bash
mkdir -p data/db            # каталог под данные (права: uid 70 для alpine-образа)
docker compose up -d        # поднять базу
docker compose ps           # статус (healthcheck: pg_isready)

# проверить подключение (порт на хосте — 5682)
PGPASSWORD=profiler psql -h localhost -p 5682 -U profiler -d profiling -c 'SELECT version();'

docker compose stop         # остановить (данные сохраняются в ./data/db)
docker compose down         # удалить контейнер, данные остаются в ./data/db
rm -rf data/db              # полная очистка БД (схема пересоздаётся при следующем запуске main.py)
```

### Вариант 2 — локальный сервер (Debian/Ubuntu, PostgreSQL 13+)

Сервер по умолчанию слушает стандартный порт **5432**, а проект ожидает
**5682**. Варианты: переназначить порт сервера — `sudo -u postgres psql -c
"ALTER SYSTEM SET port = 5682;" && sudo systemctl restart postgresql`
(тогда проверка ниже работает как есть) — либо оставить 5432 и указать его в
`DATABASE_URL` в `.env`.

```bash
# 1. Установить и запустить сервер (если ещё не стоит)
sudo apt install postgresql
sudo systemctl enable --now postgresql

# 2. Создать роль, базу и схему от имени системного пользователя postgres
sudo -u postgres psql <<'SQL'
CREATE ROLE profiler WITH LOGIN PASSWORD 'profiler';
CREATE DATABASE profiling OWNER profiler;
-- PG15+: public не наследует CREATE от владельца БД.
-- ВАЖНО: схема должна принадлежать роли profiler, иначе таблицы, созданные
-- приложением (create_all), останутся собственником postgres и INSERT/UPDATE
-- будут падать с "permission denied for table ...":
\connect profiling
DROP SCHEMA IF EXISTS public CASCADE;
CREATE SCHEMA public AUTHORIZATION profiler;
SQL

# 3. Проверить подключение под новой ролью (порт 5682)
PGPASSWORD=profiler psql -h localhost -p 5682 -U profiler -d profiling -c 'SELECT version();'
```

Схема таблиц **создаётся автоматически** при первом запуске `python main.py`
(`init_db()` → SQLAlchemy `create_all()`), никаких SQL-скриптов и миграций
запускать не нужно — база новая и пустая. Модуль версионируемых миграций
сохранён (`app/models/migrations.py`), но сейчас отключён (см. ниже).

Если используете другой хост/порт/пароль — просто поправьте `DATABASE_URL`
в `.env`. Формат: `postgresql+psycopg://USER:PASSWORD@HOST:PORT/DBNAME`.

### Что собирается (проверка после первого запуска)

```bash
PGPASSWORD=profiler psql -h localhost -p 5682 -U profiler -d profiling -c '\dt'
# messages, attachments, user_profiles, guild_member_profiles,
# user_role_tags, channel_states, …
```

## Запуск

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # заполнить DISCORD_TOKEN, CHANNEL_ID, DATABASE_URL и (опционально) START_DATE
python main.py              # Ctrl+C / SIGTERM — корректное завершение
```

При неверном/не-PostgreSQL `DATABASE_URL` приложение падает сразу с понятным
сообщением (fail-fast), а не молча создаёт sqlite-файл.

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
PostgreSQL (`DATABASE_URL`) и `data/attachments/`.

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
системной инструкцией и JSON-схемой ответа (Big Five, MBTI/эннеаграмма как слабые
гипотезы с confidence, стиль, интересы с доказательствами, risk_flags с severity,
data_gaps). Промпт оптимизирован под дешёвые/бесплатные LLM: «дело» подаётся в
компактном JSON (без snowflake-ID, URL аватаров, color/position и прочих мёртвых
байтов; даты — до дня; badges — только названия), транскрипт остаётся полным.
Модель для анализа задаётся в `.env` (`LLM_MODEL`, бесплатные варианты:
DeepSeek/Qwen через OpenRouter `:free`, Groq) — подсказка для скриптов автоанализа.
Флаг `--redact-pii` вырезает email/телефоны/IP из текста перед экспортом. Каталог
`out/` в `.gitignore` — это персональные данные.

## Психо профиль в один клик (админка)

Кнопка 🧠 «Обновить психо профиль» (для нового пользователя — «Построить»)
находится **в карточке пользователя** (`/record/user_profiles/<id>`) — все
действия над записью собраны внутри карточки; в списке профилей остались
только переход в карточку и ★ избранное. По нажатию сервер сам: собирает
«дело» по пользователю из PostgreSQL (тот же боевой промпт, что CLI-экспорт),
отправляет его в LLM API и импортирует ответ в таблицы `psych_profiles` /
`psych_signals` / `user_interests` с версионированием (старая запись становится
неактивной) и записью в `audit_log`.

Настройки в `.env`:
- `LLM_API_KEY` — ключ провайдера (без него кнопка покажет понятную ошибку);
- `LLM_BASE_URL` — по умолчанию `https://openrouter.ai/api/v1`;
- `LLM_MODEL` — по умолчанию `deepseek/deepseek-chat-v3.1:free` (лучшая из
  бесплатных моделей OpenRouter для такого анализа: большой контекст, аккуратный
  JSON). Бесплатный ключ: openrouter.ai → Sign in → Keys → Create Key.

Альтернативные бесплатные эндпоинты те же: Groq (`llama-3.3-70b-versatile`,
base_url `https://api.groq.com/openai/v1`). Ответ парсится толерантно
(markdown-обёртки ```json, лишние пояснения до/после JSON отрезаются).

## Схема БД (основное)

- `user_profiles` — глобальный профиль (bio, badges, connections, flags, raw_json)
- `guild_member_profiles` — серверный профиль (nick, joined_at, timeout, permissions)
- `user_role_tags` — теги/роли пользователя
- `messages` — сообщения + метрики (word_count, hashtag_count, mentions, reactions…),
  связи диалога: `reference_id` (на какое сообщение ответ) и `reply_to_author_id`
  (кому отвечает — автор родителя; достраивается фоном из reply-графа, работает
  даже если родительское сообщение удалено/вне периода сбора); сюда же пишутся
  сообщения ЛС (`guild_id = NULL`)
- `attachments` — вложения (url, sha256, stored_path, downloaded)
- `channel_states` — курсор last_message_id для инкрементального сбора (resume-safe)
- `message_revisions` — история правок сообщений (прежний текст, edit_number) [v10]
- `reaction_events` — событийная лента реакций add/remove (кто, чем, когда) [v10]
- `presence_snapshots` — история статусов/активностей пользователей [v10]
- `interaction_aggregates` — персистентные directed-метрики диалогичности A→B
  (обновляются PG-триггерами при записи сообщений) [v10]
- `user_profiles.dm_*` — флаги/курсоры ЛС-сбора: `dm_fetch`, `dm_channel_id`,
  `dm_last_message_id`, `dm_first_message_id`, `dm_synced_at`, `dm_messages_total` [v11]

Повторный запуск продолжает сбор с сохранённого курсора; дедупликация по
snowflake ID сообщений и вложений.

### Схема БД и миграции

База новая: схема создаётся целиком из SQLAlchemy-моделей при старте
(`app/models/db.py` → `init_db` → `create_all`) — см. раздел
«База данных (PostgreSQL)» выше. Старые SQLite-базы не поддерживаются —
данные при необходимости переносите вручную.

Модуль версионируемых миграций `app/models/migrations.py` сохранён для
будущего (доводка схемы живой базы по шагам, таблица `schema_version`),
но сейчас **отключён**: вызовы `migrate()`/`stamp_baseline()` из `init_db`
убраны, в админке кнопка запуска миграций снята. Когда база станет
«живой» и пересоздание схемы будет неприемлемым — подключите миграции
обратно в `init_db` и следуйте правилу: любое изменение модели требует
идемпотентного шага в конце списка `MIGRATIONS`.

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
