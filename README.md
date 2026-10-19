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
| Картинки и прочие вложения | CDN (`get_from_cdn`) | файлы в `data/attachments/…`, метаданные+sha256 в `attachments` |
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
cp .env.example .env        # заполнить DISCORD_TOKEN и CHANNEL_ID
python main.py              # Ctrl+C / SIGTERM — корректное завершение
```

Прогресс — в консоли (`HISTORY/POLL/PROFILE/MEDIA/STATS`), данные — в
`data/profiling.db` (SQLite) и `data/attachments/`.

## Схема БД (основное)

- `user_profiles` — глобальный профиль (bio, badges, connections, flags, raw_json)
- `guild_member_profiles` — серверный профиль (nick, joined_at, timeout, permissions)
- `user_role_tags` — теги/роли пользователя
- `messages` — сообщения + метрики (word_count, hashtag_count, mentions, reactions…)
- `attachments` — вложения (url, sha256, stored_path, downloaded)
- `channel_states` — курсор last_message_id для инкрементального сбора (resume-safe)

Повторный запуск продолжает сбор с сохранённого курсора; дедупликация по
snowflake ID сообщений и вложений.
