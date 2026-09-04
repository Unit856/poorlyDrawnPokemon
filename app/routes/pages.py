"""Lobby and profile, scope 3 and 7."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from app.auth import DbSession, RequireUser
from app.draw import TIMER_CHOICES, current_session, default_timer
from app.freechoice import balance
from app.models import Pokemon, Submission, SubmissionStatus, get_or_create_settings
from app.submissions import gallery_for
from app.templating import render

router = APIRouter()

MAX_DISPLAY_NAME = 32


@router.get("/")
def lobby(request: Request, db: DbSession, user: RequireUser):
    """Draw / My Drawings / Admin tools (scope 7 step 1)."""
    my_drawings = (
        db.scalar(
            select(func.count())
            .select_from(Submission)
            .where(Submission.user_id == user.id, Submission.status != SubmissionStatus.DELETED)
        )
        or 0
    )
    catalog_rows = (
        db.scalar(select(func.count()).select_from(Pokemon).where(Pokemon.enabled)) or 0
    )
    settings = get_or_create_settings(db)

    open_session = current_session(db, user)
    open_view = None
    if open_session is not None:
        pokemon = db.get(Pokemon, open_session.pokemon_id)
        if pokemon is not None:
            open_view = {"name": pokemon.display_name, "id": open_session.id}

    return render(
        request,
        "lobby.html",
        my_drawings=my_drawings,
        catalog_rows=catalog_rows,
        settings=settings,
        open_session=open_view,
        timer_choices=TIMER_CHOICES,
        default_timer=default_timer(db),
        balance=balance(db, user),
    )


@router.get("/drawings")
def my_drawings(request: Request, db: DbSession, user: RequireUser):
    """Scope 7.5: read-only. No actions, deliberately -- delete is Admin-only."""
    return render(request, "my_drawings.html", drawings=gallery_for(db, user))


@router.get("/profile")
def profile_form(request: Request, user: RequireUser, saved: bool = False):
    return render(request, "profile.html", saved=saved, error=None)


@router.post("/profile")
def update_profile(
    request: Request,
    db: DbSession,
    user: RequireUser,
    display_name: Annotated[str, Form()],
):
    """Scope 3: a player sets the display name used as their credit.

    Changing it does not touch anything already published. `credit` is
    snapshotted onto a submission at approval and `slug` was frozen at account
    creation, so past questions and past filenames both keep the old name
    (scope 10.2, 8.1).
    """
    display_name = display_name.strip()
    if not display_name:
        return render(request, "profile.html", saved=False, error="Display name cannot be empty.")
    if len(display_name) > MAX_DISPLAY_NAME:
        return render(
            request,
            "profile.html",
            saved=False,
            error=f"Display name must be {MAX_DISPLAY_NAME} characters or fewer.",
        )

    user.display_name = display_name
    db.add(user)
    return RedirectResponse("/profile?saved=1", status_code=status.HTTP_303_SEE_OTHER)
