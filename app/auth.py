"""Sessions and access control, scope 12.

Username + password, an HTTP-only signed session cookie, and no public
registration. The threat model is a friend group behind HTTPS (scope 12), so
this is deliberately plain: no CSRF tokens, no refresh rotation, no token store.
SameSite=Lax on the cookie is what stops cross-site form posts.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app import config
from app.models import Role, User

log = logging.getLogger(__name__)

COOKIE_NAME = "wtp_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
_SALT = "wtp-session-v1"


def _load_or_create_secret() -> str:
    """Persist the signing key in the data volume.

    It must survive `compose up` (scope 14: state restores from the volume after
    a restart) and must not live in the image, so an env var wins if set and a
    generated key is written next to the database otherwise.
    """
    from_env = os.environ.get("WTP_SECRET_KEY")
    if from_env:
        return from_env

    config.ensure_dirs()
    key_file = config.DATA_DIR / "secret_key"
    if key_file.is_file():
        existing = key_file.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    generated = secrets.token_urlsafe(48)
    key_file.write_text(generated, encoding="utf-8")
    try:
        key_file.chmod(0o600)
    except OSError:  # pragma: no cover - Windows dev boxes
        pass
    log.info("generated a new session signing key at %s", key_file)
    return generated


_serializer: URLSafeTimedSerializer | None = None


def get_serializer() -> URLSafeTimedSerializer:
    global _serializer
    if _serializer is None:
        _serializer = URLSafeTimedSerializer(_load_or_create_secret(), salt=_SALT)
    return _serializer


def issue_session(user: User) -> str:
    return get_serializer().dumps({"uid": user.id, "slug": user.slug})


def read_session(token: str | None) -> int | None:
    if not token:
        return None
    try:
        payload = get_serializer().loads(token, max_age=SESSION_MAX_AGE)
    except SignatureExpired:
        return None
    except BadSignature:
        log.warning("rejected a session cookie with a bad signature")
        return None
    uid = payload.get("uid")
    return uid if isinstance(uid, int) else None


def set_session_cookie(response, token: str, *, secure: bool) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def cookie_should_be_secure(request: Request) -> bool:
    """Secure flag when the request arrived over HTTPS (scope 12).

    Behind Caddy/nginx the app itself speaks plain HTTP, so the forwarded proto
    header is what actually carries the answer in production.
    """
    # An *empty* value means "not configured", not "off". Compose writes
    # `WTP_SECURE_COOKIES: ${WTP_SECURE_COOKIES:-}` as an empty string rather
    # than leaving it unset, and treating that as an explicit false silently
    # stripped Secure from every cookie served over HTTPS.
    override = (os.environ.get("WTP_SECURE_COOKIES") or "").strip()
    if override:
        return override.lower() in {"1", "true", "yes"}
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded:
        return forwarded.split(",")[0].strip() == "https"
    return request.url.scheme == "https"


# --- dependencies -----------------------------------------------------------


def get_db():
    from app.db import get_session_factory

    db = get_session_factory()()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


DbSession = Annotated[Session, Depends(get_db)]


@dataclass(frozen=True)
class UserView:
    """Detached, template-safe snapshot of a user.

    Templates must never hold a live ORM instance. Error pages render *after*
    the request's session has been rolled back and closed, so a mapped object
    reaching the layout raises DetachedInstanceError and turns a tidy 403 into a
    500. A plain frozen record cannot.
    """

    id: int
    username: str
    display_name: str
    slug: str
    role: str
    is_admin: bool

    @classmethod
    def of(cls, user: User) -> "UserView":
        role = user.role.value if isinstance(user.role, Role) else str(user.role)
        return cls(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            slug=user.slug,
            role=role,
            is_admin=user.is_admin,
        )


def get_current_user(
    request: Request,
    db: DbSession,
    wtp_session: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> User | None:
    """Resolve the session cookie, and stash a template-safe copy on the request.

    The layout needs the user for its nav bar. Doing that here rather than in
    middleware keeps it on the same session as the route handler, which matters
    because middleware runs outside the dependency graph.
    """
    uid = read_session(wtp_session)
    user = db.get(User, uid) if uid is not None else None
    request.state.user = UserView.of(user) if user is not None else None
    return user


CurrentUser = Annotated[User | None, Depends(get_current_user)]


class NotLoggedIn(HTTPException):
    """Signals a redirect to the login page rather than a bare 401."""

    def __init__(self) -> None:
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")


def require_user(user: CurrentUser) -> User:
    if user is None:
        raise NotLoggedIn()
    return user


RequireUser = Annotated[User, Depends(require_user)]


def require_admin(user: RequireUser) -> User:
    if not user.is_admin:
        # Deliberately 403 rather than a redirect: a logged-in player hitting an
        # admin URL is a permissions answer, not an authentication one.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin only")
    return user


RequireAdmin = Annotated[User, Depends(require_admin)]


def is_admin_role(role: Role | str) -> bool:
    return Role(role) is Role.ADMIN
