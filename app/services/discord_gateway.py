"""Discord-клиент от имени ПОЛЬЗОВАТЕЛЬСКОГО аккаунта (userbot).

Используется библиотека ``discord.py-self`` (форк discord.py для self-bot /
user-token подключений). В отличие от официальной библиотеки она:

  * подключается по user-token'у (Client, а не AutoShardedBot);
  * отправляет корректные client_type/properties, чтобы Discord видел
    обычный клиент (web/desktop/mobile), а не бота;
  * умеет делать пользовательские эндпоинты: /users/{id}/profile
    (полный профиль: bio, pronouns, badges, connections, mutual guilds).

Anti-detection / human-like поведение реализовано здесь и в CollectionService:
  * случайные паузы между запросами (REQUEST_DELAY_MIN/MAX);
  * история запрашивается small-pages через history(after=cursor) — ровно так,
    как это делает UI-клиент при скролле канала вверх;
  * никаких команд, реакций, отправки сообщений — только чтение.
"""
from __future__ import annotations

import logging
from typing import Any

import discord

log = logging.getLogger("discord_gateway")


class UserbotClient(discord.Client):
    """Тонкая обёртка над discord.py-self Client с user-oriented настройками."""

    def __init__(self, token: str, *, gateway_callback, ready_callback) -> None:
        # discord.py-self сам формирует корректные gateway properties по
        # user-token'у (client_type web/desktop/mobile), поэтому трафик
        # выглядит как обычный клиент Discord, а не как бот-либария.
        # Кэш сообщений включён точечно (max_messages=200): без него live
        # MESSAGE_EDIT/MESSAGE_DELETE приходят с author=None и пустым
        # content — редактирования было бы не зафиксировать.
        super().__init__(
            member_cache_flags=discord.MemberCacheFlags.none(),
            chunk_guilds_at_startup=False,
            guild_subscriptions=True,  # нужны live MESSAGE_CREATE для гильдий
            max_messages=200,
            status=discord.Status.online,
        )
        self._token = token
        self._gateway_callback = gateway_callback
        self._ready_callback = ready_callback
        self._ready_fired = False

    # ------------------------------------------------------------- lifecycle
    async def start(self, token: str | None = None) -> None:
        """Подключение. Токен можно передать и в конструктор, и сюда.

        Совместимость версий библиотек:
          * discord.py-self / discord.py 2.x: Client.start(token) — токен
            АРГУМЕНТОМ (в 2.x ещё и bot=...);
          * некоторые сборки/форки: Client.start() без аргументов.
        Поэтому сигнатура допускает вызов и start(), и start(token), а внутри
        выбирается вариант по интроспекции родительского метода — это исключает
        TypeError "start() takes 1 positional argument but 2 were given".
        """
        import inspect

        tok = token or self._token
        try:
            n_params = len(inspect.signature(
                discord.Client.start).parameters)
        except (TypeError, ValueError):      # builtin/C-обёртка без сигнатуры
            n_params = 1
        if n_params >= 2:                    # start(self, token, ...)
            await super().start(tok)
        else:                                # start(self) — токен где-то ещё
            await super().start()

    # ---------------------------------------------------------------- events
    async def on_message(self, message: discord.Message) -> None:
        """Live-событие MESSAGE_CREATE — отдаём готовый объект контроллеру.

        Основной источник данных — периодический history-опрос; это событие
        лишь ускоряет реакцию (не ждём следующего интервала).
        """
        try:
            await self._gateway_callback("MESSAGE_CREATE", {"message": message})
        except Exception:  # noqa: BLE001
            log.exception("Ошибка live-обработки сообщения %s", getattr(message, "id", "?"))

    async def on_message_edit(self, before: Any, after: Any) -> None:
        """Live MESSAGE_UPDATE — фиксируем факт и содержимое редактирования."""
        try:
            await self._gateway_callback("MESSAGE_EDIT", {"message": after, "before": before})
        except Exception:  # noqa: BLE001
            log.exception("Ошибка live-обработки редактирования %s", getattr(after, "id", "?"))

    async def on_message_delete(self, message: Any) -> None:
        """Live MESSAGE_DELETE — помечаем сообщение удалённым (не удаляем данные)."""
        try:
            await self._gateway_callback("MESSAGE_DELETE", {"message": message})
        except Exception:  # noqa: BLE001
            log.exception("Ошибка live-обработки удаления %s", getattr(message, "id", "?"))

    async def on_raw_reaction_add(self, payload: Any) -> None:
        """REACTION_ADD без кэша сообщения (raw-событие надёжнее cached)."""
        try:
            await self._gateway_callback(
                "REACTION_ADD",
                {
                    "message_id": getattr(payload, "message_id", None),
                    "channel_id": getattr(payload, "channel_id", None),
                    "guild_id": getattr(payload, "guild_id", None),
                    "user_id": getattr(getattr(payload, "user", None), "id", None)
                    or getattr(payload, "user_id", None),
                    "emoji": str(getattr(payload, "emoji", "")),
                },
            )
        except Exception:  # noqa: BLE001
            log.exception("Ошибка live-обработки реакции add")

    async def on_raw_reaction_remove(self, payload: Any) -> None:
        """REACTION_REMOVE — снятие реакции тоже сигнал (пересмотр оценки)."""
        try:
            await self._gateway_callback(
                "REACTION_REMOVE",
                {
                    "message_id": getattr(payload, "message_id", None),
                    "channel_id": getattr(payload, "channel_id", None),
                    "guild_id": getattr(payload, "guild_id", None),
                    "user_id": getattr(getattr(payload, "user", None), "id", None)
                    or getattr(payload, "user_id", None),
                    "emoji": str(getattr(payload, "emoji", "")),
                },
            )
        except Exception:  # noqa: BLE001
            log.exception("Ошибка live-обработки реакции remove")

    async def on_presence_update(self, before: Any, after: Any) -> None:
        """PRESENCE_UPDATE: смена статуса/активности участника гильдии."""
        try:
            user = after or before
            status = getattr(after, "status", None) or getattr(before, "status", None)
            activities = getattr(after, "activities", None) or []
            act_name = act_kind = status_text = None
            # Нормализация через единый маппер: раньше сюда шёл сырой
            # 'ActivityType.playing' (enum.name), и в presence_snapshots
            # activity_kind никогда не был равен 'playing' — из-за этого
            # diag-эвристика users_with_games давала ложные 0.
            from app.services import mappers
            for a in activities:
                kind = mappers.activity_kind_str(a)
                if kind == "custom":
                    status_text = getattr(a, "state", None)
                elif act_name is None:
                    act_name = getattr(a, "name", None)
                    act_kind = kind
            await self._gateway_callback(
                "PRESENCE_UPDATE",
                {
                    "user_id": getattr(user, "id", None),
                    "status": getattr(status, "name", None) or (str(status) if status else None),
                    "status_text": status_text,
                    "activity_name": act_name,
                    "activity_kind": act_kind,
                },
            )
        except Exception:  # noqa: BLE001
            log.exception("Ошибка live-обработки presence")

    # ---------------------------------------------------------------- events
    async def on_ready(self) -> None:
        # on_ready вызывается при каждой переподключении gateway — иначе
        # контроллер повторно запускал бы полный backfill. Готовимся один раз.
        if getattr(self, "_ready_fired", False):
            return
        self._ready_fired = True
        me = self.user
        log.info(
            "Вход выполнен: %s (id=%s) — режим пользователя, бот-флаг отсутствует",
            getattr(me, "name", "?"),
            getattr(me, "id", "?"),
        )
        if me is not None and getattr(me, "bot", False):
            log.warning("Токен помечен как bot-токен! Для user-режима нужен user-token.")
        await self._ready_callback(self)

    # ------------------------------------------------------------ permissions
    async def safe_fetch_channel(self, channel_id: int) -> discord.abc.Messageable | None:
        try:
            return await self.fetch_channel(channel_id)
        except discord.Forbidden:
            log.error("Нет доступа к каналу %s (403)", channel_id)
        except discord.NotFound:
            log.error("Канал %s не найден (404)", channel_id)
        except discord.HTTPException as exc:
            log.error("fetch_channel(%s) failed: %s", channel_id, exc)
        return None

    # ------------------------------------------------------------------- DM
    async def resolve_dm_channel(self, dm_channel_id: int | None, user_id: int):
        """Найти существующий ЛС-канал пользователя. НИЧЕГО НЕ СОЗДАЁТ.

        Порядок (по докам: GET /users/@me/channels — единственный способ
        узнать о канале, которого нет в кэше):
          1) get_channel(id) — канал открыт и лежит в локальном кэше;
          2) fetch_channel(id) — прямой запрос канала по id;
          3) fetch_private_channels() — список всех ЛС-каналов аккаунта,
             поиск по получателям (в py-self у DM два recipients: я и
             собеседник; совпадение по id = диалог с нужным человеком).

        Возвращает (channel, source) либо (None, причина_ошибки_или_None).
        """
        # 1) кэш клиента (без сетевого запроса)
        if dm_channel_id:
            ch = self.get_channel(int(dm_channel_id))
            if ch is not None:
                return ch, "cache"
        # 2) прямой fetch по id
        if dm_channel_id:
            try:
                ch = await self.fetch_channel(int(dm_channel_id))
                return ch, "fetch_channel"
            except discord.Forbidden:
                pass  # нет доступа к каналу по id — пробуем список ЛС
            except discord.NotFound:
                pass
            except discord.HTTPException as exc:
                log.debug("resolve_dm: fetch_channel(%s) http error: %s",
                          dm_channel_id, exc)
        # 3) GET /users/@me/channels — поиск среди существующих диалогов
        fetch_err = None
        try:
            for ch in await self.fetch_private_channels():
                recips = {r.id for r in getattr(ch, "recipients", [])}
                if user_id in recips or (
                        getattr(ch, "recipient", None) is not None
                        and ch.recipient.id == user_id):
                    return ch, "private_channels"
        except discord.Forbidden as exc:
            fetch_err = f"Forbidden({exc})"
        except discord.HTTPException as exc:
            fetch_err = f"{type(exc).__name__}({exc})"
            log.debug("resolve_dm: fetch_private_channels failed: %s", exc)
        return None, fetch_err

    async def fetch_full_profile(self, user_id: int):
        """Полный профиль пользователя (bio, badges, connections, mutual guilds).

        Эндпоинт ``/users/{id}/profile`` доступен только пользовательским
        токеном — боты его не видят. Возвращает ``discord.UserProfile`` либо None.
        """
        try:
            return await self.fetch_user_profile(
                user_id,
                with_mutual_guilds=True,
                with_mutual_friends=True,
                with_mutual_friends_count=True,
            )
        except TypeError:
            # подстраховка, если версия либы не поддерживает какие-то kwarg
            try:
                return await self.fetch_user_profile(user_id)
            except discord.DiscordException as exc:
                log.debug("fetch_user_profile(%s) fallback failed: %s", user_id, exc)
                return None
        except discord.Forbidden:
            log.debug("Профиль %s недоступен (заблокирован/нет доступа)", user_id)
            return None
        except discord.HTTPException as exc:
            log.debug("fetch_user_profile(%s) http error: %s", user_id, exc)
            return None

    async def fetch_guild_channels(self, guild_id: int):
        """Список каналов сервера (text/voice/forum).

        Нужен админке для «обновить чаты»: без этого шага в chats попадают
        только каналы, из которых уже были собраны сообщения.
        Возвращает [(channel_id, name)]; пустой список при недоступности.
        """
        try:
            guild = self.get_guild(guild_id) or await self.fetch_guild(guild_id)
            return [(int(ch.id), getattr(ch, "name", "") or f"#{ch.id}")
                    for ch in guild.channels]
        except Exception as exc:  # noqa: BLE001 — приватный канал/нет доступа
            log.debug("fetch_guild_channels(%s) failed: %s", guild_id, exc)
            return []

    async def fetch_member_safe(self, guild_id: int, user_id: int) -> discord.Member | None:
        try:
            guild = self.get_guild(guild_id) or await self.fetch_guild(guild_id)
            return await guild.fetch_member(user_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("fetch_member(%s,%s) failed: %s", guild_id, user_id, exc)
            return None
