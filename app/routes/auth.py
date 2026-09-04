"""Login and logout, scope 12."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from app.auth import (
    CurrentUser,
    DbSession,
    clear_session_cookie,
    cookie_should_be_secure,
    issue_session,
    set_session_cookie,
)
from app.models import User
from app.ratelimit import client_key, login_limiter
from app.security import needs_rehash, verify_password
from app.templating import render

router = APIRouter()


@router.get("/login")
def login_form(request: Request, db: DbSession, user: CurrentUser, next: str = "/"):
    if user is not None:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    # No public registration (scope 12), so a fresh install has no way in until
    # the admin runs the CLI. Say so rather than showing an unusable form.
    no_users = (db.scalar(select(func.count()).select_from(User)) or 0) == 0
    return render(request, "login.html", next=next, no_users=no_users, error=None)


@router.post("/login")
def login(
    request: Request,
    db: DbSession,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
):
    username = username.strip()
    key = client_key(request, username.lower())

    def fail(message: str, code: int = status.HTTP_401_UNAUTHORIZED):
        response = render(request, "login.html", next=next, no_users=False, error=message)
        response.status_code = code
        return response

    if not login_limiter.hit(key):
        return fail(
            f"Too many attempts. Try again in {login_limiter.retry_after(key)} seconds.",
            status.HTTP_429_TOO_MANY_REQUESTS,
        )

    user = db.scalar(select(User).where(User.username == username))
    # Same message either way: a login form should not confirm which usernames
    # exist, even to friends.
    if user is None or not verify_password(user.password_hash, password):
        return fail("Incorrect username or password.")

    if needs_rehash(user.password_hash):
        from app.security import hash_password

        user.password_hash = hash_password(password)
        db.add(user)

    login_limiter.reset(key)

    # Only ever redirect within this site.
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(response, issue_session(user), secure=cookie_should_be_secure(request))
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    clear_session_cookie(response)
    return response
