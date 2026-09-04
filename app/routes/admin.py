"""Admin user management, scope 3 and 12.

Accounts are created here or by the CLI; there is no self-serve signup. Password
reset is an Admin action that sets a new password directly -- no email flow, no
reset tokens (scope 12).
"""

from __future__ import annotations

import secrets
from typing import Annotated

from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi import status as status_lib  # `status` is a route parameter below
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from app import config, preflight
from app.auth import DbSession, RequireAdmin, UserView
from app.export import ExportBlocked, approved_submissions, export, pack_index
from app.draw import TIMER_CHOICES, normalise_timer
from app.models import Role, Submission, SubmissionStatus, User, get_or_create_settings
from app.moderation import ACTIONS, ModerationError
from app.picker import coverage, drawing_counts, min_tier
from app.slugs import artist_slug
from app.submissions import review_queue, status_counts
from app.templating import render
from app.users import SlugTaken, UsernameTaken, create_user, set_password

router = APIRouter(prefix="/admin")

MIN_PASSWORD_LENGTH = 8


def _users_page(request, db, *, error=None, notice=None, status_code=200):
    users = [UserView.of(u) for u in db.scalars(select(User).order_by(User.username)).all()]
    response = render(request, "admin_users.html", users=users, error=error, notice=notice)
    response.status_code = status_code
    return response


@router.get("/users")
def list_users(request: Request, db: DbSession, admin: RequireAdmin):
    return _users_page(request, db)


@router.post("/users/create")
def create(
    request: Request,
    db: DbSession,
    admin: RequireAdmin,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    display_name: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = Role.PLAYER.value,
):
    if len(password) < MIN_PASSWORD_LENGTH:
        return _users_page(
            request,
            db,
            error=f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
            status_code=status_lib.HTTP_400_BAD_REQUEST,
        )
    try:
        user = create_user(
            db,
            username=username,
            password=password,
            display_name=display_name or None,
            role=Role(role),
        )
    except (UsernameTaken, SlugTaken) as exc:
        # Slug collisions are surfaced verbatim: the fix is a human choosing a
        # different username, never an auto-suffix (scope 8.1).
        return _users_page(request, db, error=str(exc), status_code=status_lib.HTTP_400_BAD_REQUEST)

    return _users_page(
        request, db, notice=f"Created {user.username} with artist slug {user.slug}."
    )


@router.post("/users/{user_id}/password")
def reset_password(
    request: Request,
    db: DbSession,
    admin: RequireAdmin,
    user_id: int,
    password: Annotated[str, Form()] = "",
):
    user = db.get(User, user_id)
    if user is None:
        return _users_page(request, db, error="No such user.", status_code=status_lib.HTTP_404_NOT_FOUND)

    generated = None
    if not password:
        generated = secrets.token_urlsafe(9)
        password = generated
    elif len(password) < MIN_PASSWORD_LENGTH:
        return _users_page(
            request,
            db,
            error=f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
            status_code=status_lib.HTTP_400_BAD_REQUEST,
        )

    set_password(db, user, password)
    notice = (
        f"Password reset for {user.username}. Temporary password: {generated}"
        if generated
        else f"Password reset for {user.username}."
    )
    return _users_page(request, db, notice=notice)


@router.post("/users/{user_id}/role")
def set_role(
    request: Request,
    db: DbSession,
    admin: RequireAdmin,
    user_id: int,
    role: Annotated[str, Form()],
):
    user = db.get(User, user_id)
    if user is None:
        return _users_page(request, db, error="No such user.", status_code=status_lib.HTTP_404_NOT_FOUND)

    new_role = Role(role)
    if user.id == admin.id and new_role is not Role.ADMIN:
        # Locking every admin out of a self-hosted box with no signup and no
        # password-reset flow would need CLI surgery to undo.
        return _users_page(
            request,
            db,
            error="You cannot remove your own Admin role.",
            status_code=status_lib.HTTP_400_BAD_REQUEST,
        )

    remaining_admins = [
        u for u in db.scalars(select(User).where(User.role == Role.ADMIN)).all() if u.id != user.id
    ]
    if new_role is not Role.ADMIN and not remaining_admins:
        return _users_page(
            request,
            db,
            error="At least one Admin must remain.",
            status_code=status_lib.HTTP_400_BAD_REQUEST,
        )

    user.role = new_role
    db.add(user)
    return _users_page(request, db, notice=f"{user.username} is now {new_role.value}.")


@router.get("/settings")
def settings_form(request: Request, db: DbSession, admin: RequireAdmin, saved: bool = False):
    return _settings_page(request, db, saved=saved)


def _settings_page(request, db, *, saved=False, error=None, status_code=200):
    settings = get_or_create_settings(db)
    response = render(
        request,
        "admin_settings.html",
        settings=settings,
        timer_choices=TIMER_CHOICES,
        saved=saved,
        error=error,
    )
    response.status_code = status_code
    return response


@router.post("/settings")
def update_settings(
    request: Request,
    db: DbSession,
    admin: RequireAdmin,
    public_base_url: Annotated[str, Form()] = "",
    default_timer_seconds: Annotated[str, Form()] = "",
    require_approval: Annotated[str, Form()] = "",
):
    settings = get_or_create_settings(db)

    base = public_base_url.strip().rstrip("/")
    if base and not base.startswith(("http://", "https://")):
        return _settings_page(
            request,
            db,
            error="Public base URL must start with http:// or https://",
            status_code=status_lib.HTTP_400_BAD_REQUEST,
        )

    settings.public_base_url = base
    settings.default_timer_seconds = normalise_timer(default_timer_seconds)
    # Scope 9. Toggling this only affects *new* submissions: anything already
    # pending stays pending until an Admin works the queue.
    settings.require_approval = require_approval.lower() in {"1", "on", "true", "yes"}
    db.add(settings)
    return RedirectResponse("/admin/settings?saved=1", status_code=status_lib.HTTP_303_SEE_OTHER)


@router.get("/queue")
def queue(
    request: Request,
    db: DbSession,
    admin: RequireAdmin,
    status: str = "",
    notice: str = "",
    error: str = "",
):
    return _queue_page(request, db, status=status, notice=notice or None, error=error or None)


def _queue_page(request, db, *, status="", notice=None, error=None, status_code=200):
    valid = {s.value for s in SubmissionStatus}
    status = status if status in valid else ""
    response = render(
        request,
        "admin_queue.html",
        drawings=review_queue(db, status=status or None),
        counts=status_counts(db),
        active_status=status or "pending",
        settings=get_or_create_settings(db),
        notice=notice,
        error=error,
    )
    response.status_code = status_code
    return response


@router.post("/submissions/{submission_id}/{action}")
def moderate(
    request: Request,
    db: DbSession,
    admin: RequireAdmin,
    submission_id: int,
    action: str,
    status: Annotated[str, Form()] = "",
):
    """Approve / unapprove / reject / unreject / delete (scope 9).

    Delete is here and nowhere else: players cannot delete their own drawings,
    because that would reopen the loophole rejection exists to close.
    """
    handler = ACTIONS.get(action)
    if handler is None:
        raise HTTPException(status_code=status_lib.HTTP_404_NOT_FOUND, detail="Not Found")

    submission = db.get(Submission, submission_id)
    if submission is None:
        return _queue_page(
            request, db, status=status, error="No such drawing.",
            status_code=status_lib.HTTP_404_NOT_FOUND,
        )

    try:
        outcome = handler(db, submission)
    except ModerationError as exc:
        return _queue_page(
            request, db, status=status, error=str(exc),
            status_code=status_lib.HTTP_400_BAD_REQUEST,
        )

    notice = f"Drawing #{submission_id} is now {outcome.status.value}."
    if outcome.retired_unique_id is not None:
        notice += (
            f" uniqueId {outcome.retired_unique_id} is retired permanently and will "
            "never be reused — re-approving mints a new question."
        )
    if outcome.assigned_unique_id is not None:
        notice += f" Assigned uniqueId {outcome.assigned_unique_id}."

    return RedirectResponse(
        f"/admin/queue?status={status}&notice={quote(notice)}",
        status_code=status_lib.HTTP_303_SEE_OTHER,
    )


@router.get("/export")
def export_preview(request: Request, db: DbSession, admin: RequireAdmin, error: str = ""):
    """Scope 10.1. Shows what the pack will contain before downloading it."""
    settings = get_or_create_settings(db)
    submissions = approved_submissions(db)
    packs: dict[int, int] = {}
    for submission in submissions:
        index = pack_index(submission.unique_id)
        packs[index] = packs.get(index, 0) + 1

    return render(
        request,
        "admin_export.html",
        count=len(submissions),
        packs=dict(sorted(packs.items())),
        warn_threshold=config.PACK_ROW_WARN_THRESHOLD,
        chunk_size=config.PACK_CHUNK_SIZE,
        over_threshold=len(submissions) > config.PACK_ROW_WARN_THRESHOLD,
        settings=settings,
        error=error or None,
    )


@router.post("/export")
def download_csv(request: Request, db: DbSession, admin: RequireAdmin):
    try:
        payload, report = export(db)
    except ExportBlocked as exc:
        return RedirectResponse(
            f"/admin/export?error={quote(str(exc))}", status_code=status_lib.HTTP_303_SEE_OTHER
        )

    if report.rows == 0:
        return RedirectResponse(
            "/admin/export?error=" + quote("There are no approved drawings to export yet."),
            status_code=status_lib.HTTP_303_SEE_OTHER,
        )

    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="WhosThatPokemon.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/preflight")
def preflight_page(request: Request, db: DbSession, admin: RequireAdmin, run: str = ""):
    """Scope 13. Network checks only run on request — they hit the public URL."""
    report = preflight.run(db, skip_network=True) if run != "1" else preflight.run(db)
    return render(
        request,
        "admin_preflight.html",
        report=report,
        ran_network=run == "1",
        settings=get_or_create_settings(db),
    )


@router.get("/coverage")
def coverage_report(request: Request, db: DbSession, admin: RequireAdmin):
    """Catalog coverage, the scope 17 step 3 verification surface.

    Shows how the picker has spread drawings across the dex so far.
    """
    counts = drawing_counts(db)
    return {
        "answer_units": len(counts),
        "histogram": {str(k): v for k, v in coverage(counts).items()},
        "lowest_count": min(counts.values()) if counts else None,
        "min_tier_size": len(min_tier(counts)),
    }


@router.get("/slug-preview")
def slug_preview(request: Request, db: DbSession, admin: RequireAdmin, username: str = ""):
    """Show what a username would slugify to before the account is created.

    Worth having because the slug is permanent and appears in every filename
    that account ever writes.
    """
    try:
        slug = artist_slug(username) if username else ""
        taken = bool(slug) and db.scalar(select(User).where(User.slug == slug)) is not None
    except ValueError:
        slug, taken = "", False
    return {"username": username, "slug": slug, "taken": taken}
