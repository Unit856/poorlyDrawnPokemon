"""ASGI entrypoint.

The catalog is deliberately *not* seeded here -- scope 5.5 requires seeding to be
an explicit Admin action, never something that happens on app start.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__, config
from app.auth import NotLoggedIn
from app.db import init_db, session_scope
from app.models import Pokemon, User, get_or_create_settings
from app.routes import admin as admin_routes
from app.routes import auth as auth_routes
from app.routes import draw as draw_routes
from app.routes import images as image_routes
from app.routes import pages as page_routes
from app.templating import STATIC_DIR, render


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.ensure_dirs()
    init_db()
    yield


app = FastAPI(title="Who's That Pokemon", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.exception_handler(NotLoggedIn)
async def redirect_to_login(request: Request, _exc: NotLoggedIn):
    return RedirectResponse(
        f"/login?next={quote(request.url.path)}", status_code=status.HTTP_303_SEE_OTHER
    )


# Registered against Starlette's HTTPException, not FastAPI's subclass: an
# unmatched route raises the base class, which would otherwise skip this handler
# and fall through to the default JSON 404.
@app.exception_handler(StarletteHTTPException)
async def html_error(request: Request, exc: StarletteHTTPException):
    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    response = render(request, "error.html", code=exc.status_code, detail=exc.detail)
    response.status_code = exc.status_code
    return response


app.include_router(auth_routes.router)
app.include_router(page_routes.router)
app.include_router(draw_routes.router)
app.include_router(image_routes.router)
app.include_router(admin_routes.router)


@app.get("/healthz")
def healthz() -> JSONResponse:
    with session_scope() as session:
        settings = get_or_create_settings(session)
        enabled = (
            session.scalar(select(func.count()).select_from(Pokemon).where(Pokemon.enabled)) or 0
        )
        users = session.scalar(select(func.count()).select_from(User)) or 0
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "catalog_enabled_rows": enabled,
                "catalog_snapshot": settings.catalog_snapshot_label or None,
                "users": users,
                "public_base_url": settings.public_base_url or None,
            }
        )
