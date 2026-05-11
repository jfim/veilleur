"""Login / logout routes for the cookie-based web UI session."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from veilleur.web.auth import (
    COOKIE_NAME,
    REMEMBER_ME_MAX_AGE_SECONDS,
    is_safe_next,
    verify_password,
)

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
        },
    )


@login_router.post("/login", name="login_submit")
async def login_submit(
    request: Request,
    password: str = Form(...),
    remember_me: bool = Form(default=False),
    next: str = Form(default="/"),
) -> Response:
    target = _resolve_next(next)
    if not verify_password(password):
        return _templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "request": request,
                "next": target,
                "error": "Invalid password.",
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
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


@login_router.post("/logout", name="logout")
async def logout(request: Request) -> Response:
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return response
