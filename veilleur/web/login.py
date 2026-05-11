"""Login / logout routes for the cookie-based web UI session."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from veilleur.web.auth import (
    COOKIE_NAME,
    REMEMBER_ME_MAX_AGE_SECONDS,
    is_safe_next,
    verify_password,
)
from veilleur.web.csrf import require_csrf_token


def _csrf(request: Request) -> str:
    return getattr(request.state, "csrf_token", "")

#: Per-IP login throttle: at most this many failed attempts inside the
#: rolling window below before further attempts are rejected with 429.
#: Successful logins do not consume budget.
_LOGIN_FAIL_LIMIT = 5
_LOGIN_FAIL_WINDOW_SECONDS = 60.0
#: Constant delay applied to every failed attempt. Slows down brute-force
#: scans without making the legitimate "I fat-fingered my password" path
#: actively annoying.
_FAILED_LOGIN_DELAY_SECONDS = 1.0

_login_failures_lock = asyncio.Lock()
_login_failures: dict[str, deque[float]] = {}


def _client_ip(request: Request) -> str:
    """Best-effort client IP for throttle bookkeeping.

    ``ProxyHeadersMiddleware`` populates ``request.client.host`` from
    trusted ``X-Forwarded-For`` headers, so behind a configured proxy the
    real client IP is what we see here. Bare or unknown clients fall back
    to a literal sentinel so they share one bucket rather than disabling
    throttling entirely.
    """
    if request.client is not None and request.client.host:
        return request.client.host
    return "unknown"


def _purge_old(history: deque[float], now: float) -> None:
    cutoff = now - _LOGIN_FAIL_WINDOW_SECONDS
    while history and history[0] < cutoff:
        history.popleft()


async def _too_many_failures(ip: str) -> bool:
    now = time.monotonic()
    async with _login_failures_lock:
        history = _login_failures.get(ip)
        if history is None:
            return False
        _purge_old(history, now)
        if not history:
            _login_failures.pop(ip, None)
            return False
        return len(history) >= _LOGIN_FAIL_LIMIT


async def _record_failure(ip: str) -> None:
    now = time.monotonic()
    async with _login_failures_lock:
        history = _login_failures.setdefault(ip, deque())
        _purge_old(history, now)
        history.append(now)


async def _clear_failures(ip: str) -> None:
    async with _login_failures_lock:
        _login_failures.pop(ip, None)

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _static_url(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return path


_templates.env.globals["static_url"] = _static_url  # type: ignore[assignment]


login_router = APIRouter(tags=["web-ui-auth"])


def _resolve_next(raw: str | None) -> str:
    if raw and is_safe_next(raw):
        return raw
    return "/"


def _cookie_secure(request: Request) -> bool:
    """Mark the cookie ``Secure`` whenever the request looks like HTTPS.

    Honours ``X-Forwarded-Proto`` via Starlette's URL parsing, which the
    ``ProxyHeadersMiddleware`` already populates from trusted proxies.
    """
    return request.url.scheme == "https"


@login_router.get("/login", name="login", response_class=HTMLResponse)
async def login_form(
    request: Request,
    next: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    return _templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "request": request,
            "next": _resolve_next(next),
            "error": error,
            "csrf_token": _csrf(request),
        },
    )


@login_router.post(
    "/login",
    name="login_submit",
    dependencies=[Depends(require_csrf_token)],
)
async def login_submit(
    request: Request,
    password: str = Form(...),
    remember_me: bool = Form(default=False),
    next: str = Form(default="/"),
) -> Response:
    target = _resolve_next(next)
    ip = _client_ip(request)
    if await _too_many_failures(ip):
        return _templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "request": request,
                "next": target,
                "error": "Too many failed attempts. Try again in a minute.",
                "csrf_token": _csrf(request),
            },
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(int(_LOGIN_FAIL_WINDOW_SECONDS))},
        )
    if not verify_password(password):
        await _record_failure(ip)
        # Constant delay on failure so the loop is bounded even when an
        # attacker stays just under the per-window cap.
        await asyncio.sleep(_FAILED_LOGIN_DELAY_SECONDS)
        return _templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "request": request,
                "next": target,
                "error": "Invalid password.",
                "csrf_token": _csrf(request),
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    await _clear_failures(ip)
    response = RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=COOKIE_NAME,
        value=password,
        max_age=REMEMBER_ME_MAX_AGE_SECONDS if remember_me else None,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
        path="/",
    )
    return response


@login_router.post(
    "/logout",
    name="logout",
    dependencies=[Depends(require_csrf_token)],
)
async def logout(request: Request) -> Response:
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return response
