"""Security-слой админки: rate-limit страницы входа.

CSRF-защита выключена (осознанный отказ, 2026-10-24)
-----------------------------------------------------
Проект запускается только локально (bind на 127.0.0.1), доступ к админке
есть лишь у владельца машины. Double-submit cookie создавал проблемы:
падение record.html с 500 при отсутствии context_processor и ложные 400
«CSRF-токен отсутствует или не совпадает» при работе с несколькими
вкладками/рестартах процесса. Функция ``csrf_protect`` сохранена как
no-op: шаблоны могут использовать ``{{ csrf_token() }}`` (вернёт пустую
строку), а скрытые поля ``name='csrf_token'`` просто игнорируются на
бэкенде. Если проект когда-нибудь выйдет за пределы localhost —
восстановите реализацию из истории git.

Rate-limit входа
----------------
Неудачные попытки входа считаются на IP в памяти процесса: после
``ADMIN_LOGIN_MAX_ATTEMPTS`` (по умолчанию 5) подряд идущих ошибок вход
блокируется на ``ADMIN_LOGIN_LOCKOUT_SEC`` (по умолчанию 300 сек) с
экспоненциальным удлинением блокировки. Успешный вход счётчик сбрасывает.
Хранилище in-memory: при рестарте процессы обнуляются — это осознанный
компромисс для локальной админки (per-process limit).
"""
from __future__ import annotations

import os
import time
from collections import defaultdict
from typing import Callable

from flask import request

CSRF_COOKIE = "admin_csrf"   # legacy: cookie больше не ставится и не проверяется
CSRF_FIELD = "csrf_token"    # legacy: поле в формах игнорируется


# --------------------------------------------------------------------------- #
# CSRF (выключен)                                                              #
# --------------------------------------------------------------------------- #

def csrf_protect(app) -> None:
    """No-op. Проверка CSRF отключена: проект работает только локально.

    Оставлен context_processor, чтобы {{ csrf_token() }} в шаблонах не падал
    с UndefinedError (вернёт пустую строку). Скрытые поля csrf_token в формах
     игнорируются — проверка на бэкенде не выполняется.
    """

    @app.context_processor
    def _csrf_inject() -> dict[str, Callable[[], str]]:
        return {"csrf_token": lambda: ""}


# --------------------------------------------------------------------------- #
# Rate limit входа                                                             #
# --------------------------------------------------------------------------- #

class LoginRateLimiter:
    """Счётчик неудачных попыток входа по IP с экспоненциальным lockout."""

    def __init__(self, max_attempts: int = 5, base_lockout_sec: int = 300,
                 max_lockout_sec: int = 3600) -> None:
        self.max_attempts = max(1, max_attempts)
        self.base_lockout_sec = max(1, base_lockout_sec)
        self.max_lockout_sec = max(self.base_lockout_sec, max_lockout_sec)
        # ip -> (число неудач подряд, unix-ts разблокировки)
        self._fail: dict[str, tuple[int, float]] = {}

    @staticmethod
    def _client_ip() -> str:
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.remote_addr or "?"

    def locked_for(self, ip: str | None = None) -> float:
        """Сколько секунд осталось до разблокировки (0 — не заблокирован)."""
        ip = ip or self._client_ip()
        entry = self._fail.get(ip)
        if not entry:
            return 0.0
        remain = entry[1] - time.time()
        if remain <= 0:
            self._fail.pop(ip, None)
            return 0.0
        return remain

    def register_failure(self, ip: str | None = None) -> float:
        """Зафиксировать неудачу; вернуть сколько секунд теперь заблокирован."""
        ip = ip or self._client_ip()
        n, until = self._fail.get(ip, (0, 0.0))
        n += 1
        lock = 0.0
        if n >= self.max_attempts:
            over = n - self.max_attempts
            lock = min(self.base_lockout_sec * (2 ** over), self.max_lockout_sec)
            until = time.time() + lock
        self._fail[ip] = (n, until)
        return lock

    def reset(self, ip: str | None = None) -> None:
        """Успешный вход / новый логин-гет — снимаем счётчик."""
        self._fail.pop(ip or self._client_ip(), None)


def make_login_limiter() -> LoginRateLimiter:
    return LoginRateLimiter(
        max_attempts=int(os.getenv("ADMIN_LOGIN_MAX_ATTEMPTS", "5") or 5),
        base_lockout_sec=int(os.getenv("ADMIN_LOGIN_LOCKOUT_SEC", "300") or 300),
    )
