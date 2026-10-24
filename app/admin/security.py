"""Security-слой админки: CSRF-защита форм и rate-limit страницы входа.

CSRF — double-submit cookie
---------------------------
На каждый запрос выдаётся токен в http-only cookie ``admin_csrf`` и
одновременно кладётся в ``g.csrf_token`` для шаблонов. Все POST-запросы
обязаны содержать поле ``csrf_token``, равное cookie — сверка через
``secrets.compare_digest`` (против timing-атак). GET/HEAD/OPTIONS не
проверяются (не должны менять состояние; «не трогать» по стандарту RFC 9110).

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
import secrets
import time
from collections import defaultdict
from typing import Callable

from flask import abort, g, request

CSRF_COOKIE = "admin_csrf"
CSRF_FIELD = "csrf_token"


# --------------------------------------------------------------------------- #
# CSRF                                                                         #
# --------------------------------------------------------------------------- #

def csrf_protect(app) -> None:
    """Регистрирует before_request-хук CSRF-защиты и токен для шаблонов."""

    @app.before_request
    def _csrf_check():
        # Токен на каждый запрос (для форм): cookie + g для {{ csrf_token() }}.
        token = request.cookies.get(CSRF_COOKIE)
        if not token or len(token) < 16:
            token = secrets.token_hex(32)
        g.csrf_token = token

        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        if request.endpoint == "static":
            return None

        submitted = ""
        if request.mimetype in ("application/x-www-form-urlencoded", "multipart/form-data"):
            submitted = request.form.get(CSRF_FIELD, "")
        elif request.is_json:
            try:
                submitted = (request.get_json(silent=True) or {}).get(CSRF_FIELD, "")
            except Exception:  # noqa: BLE001
                submitted = ""
        else:
            submitted = request.headers.get("X-CSRF-Token", "")

        cookie = request.cookies.get(CSRF_COOKIE, "")
        if not cookie or not submitted or \
                not secrets.compare_digest(str(submitted), str(cookie)):
            abort(400, description="CSRF-токен отсутствует или не совпадает. "
                                   "Перезагрузите страницу и повторите действие.")
        return None

    @app.after_request
    def _csrf_cookie(resp):
        # Cookie всегда синхронна с токеном страницы (SameSite=Lax — формы
        # с внешних сайтов не получат ни cookie, ни возможности его подделать).
        resp.set_cookie(
            CSRF_COOKIE,
            g.get("csrf_token", ""),
            httponly=True,
            samesite="Lax",
            secure=bool(os.getenv("ADMIN_COOKIE_SECURE", "").strip()),
        )
        return resp

    @app.context_processor
    def _csrf_inject() -> dict[str, Callable[[], str]]:
        # Использование в шаблонах: <input type="hidden" name="csrf_token"
        #                            value="{{ csrf_token() }}">
        return {"csrf_token": lambda: getattr(g, "csrf_token", "")}


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
