"""Draw flow, scope 7.

Slice 3 renders the hint panel and nothing else: the canvas, submit and PNG
export arrive in slice 4. That split is deliberate (scope 17, step 3) so the
weighting can be verified before any drawing code exists.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse, RedirectResponse

from app import config
from app.auth import DbSession, RequireUser
from app.draw import (
    TIMER_CHOICES,
    assign,
    current_session,
    default_timer,
    hints,
    normalise_timer,
    skip,
)
from app.images import ImageRejected, resolve_public_url
from app.models import (
    Pokemon,
    SessionOutcome,
    Submission,
    SubmissionStatus,
    get_or_create_settings,
)
from app.picker import EmptyPool
from app.ratelimit import client_key, submit_limiter
from app.submissions import create_submission, resolve_draw_session
from app.templating import render

router = APIRouter()


#: Scope 7.2: a small preset palette plus a custom picker. Black, white and
#: common Pokemon-ish primaries.
PALETTE = (
    "#1f2430", "#ffffff", "#e5484d", "#f76808", "#ffc53d", "#46a758",
    "#00a2c7", "#3b5bdb", "#8e4ec6", "#a5642a", "#e93d82", "#8b8d98",
)


def _render_draw(request: Request, db, session):
    pokemon = db.get(Pokemon, session.pokemon_id)
    return render(
        request,
        "draw.html",
        hints=hints(pokemon),
        timer_seconds=session.timer_seconds,
        session_id=session.id,
        canvas_size=config.CANVAS_SIZE,
        fill_tolerance=config.FILL_TOLERANCE,
        undo_depth=config.UNDO_DEPTH,
        palette=PALETTE,
    )


@router.post("/draw/start")
def start(
    request: Request,
    db: DbSession,
    user: RequireUser,
    timer: Annotated[str, Form()] = "",
):
    """Begin a session from the lobby.

    If an assignment is already open this does *not* re-roll it -- the chosen
    timer applies to the next new assignment instead (scope 7.1).
    """
    try:
        assign(db, user, timer_seconds=normalise_timer(timer))
    except EmptyPool:
        return render(request, "draw_empty.html")
    return RedirectResponse("/draw", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/draw")
def draw(request: Request, db: DbSession, user: RequireUser):
    session = current_session(db, user)
    if session is None:
        try:
            session = assign(db, user, timer_seconds=default_timer(db))
        except EmptyPool:
            return render(request, "draw_empty.html")
    return _render_draw(request, db, session)


@router.post("/draw/skip")
def skip_current(request: Request, db: DbSession, user: RequireUser):
    """Skip creates no submission and does not change drawing_count (scope 6)."""
    try:
        session = skip(db, user)
    except EmptyPool:
        return render(request, "draw_empty.html")
    if session is None:
        return RedirectResponse("/draw", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/draw", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/draw/submit")
async def submit(
    request: Request,
    db: DbSession,
    user: RequireUser,
    image: Annotated[UploadFile, File()],
    strokes: Annotated[int, Form()] = 0,
):
    """Scope 7.3 / 8.1. Writes the PNG, records the submission, resolves the session."""
    session = current_session(db, user)
    if session is None:
        # Nothing assigned -- most likely a stale tab posting after a skip.
        return JSONResponse(
            {"error": "No Pokémon is currently assigned. Start a new drawing."},
            status_code=status.HTTP_409_CONFLICT,
        )

    if strokes < 1:
        # Scope 7.3: zero committed strokes is empty and creates no row.
        return JSONResponse(
            {"error": "Draw something first."}, status_code=status.HTTP_400_BAD_REQUEST
        )

    key = client_key(request, str(user.id))
    if not submit_limiter.hit(key):
        return JSONResponse(
            {"error": "You are submitting too quickly. Wait a moment and try again."},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    pokemon = db.get(Pokemon, session.pokemon_id)
    data = await image.read()
    try:
        submission = create_submission(db, pokemon, user, data)
    except ImageRejected as exc:
        return JSONResponse({"error": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST)

    resolve_draw_session(db, session, SessionOutcome.SUBMITTED)
    return JSONResponse({"ok": True, "redirect": f"/draw/done/{submission.id}"})


@router.post("/draw/timeout")
def timed_out(request: Request, db: DbSession, user: RequireUser):
    """Timer hit zero on an untouched canvas.

    Scope 7.3: discard it and record a skip. No submission row, no PNG written,
    drawing_count unchanged. A canvas *with* strokes auto-submits instead, which
    the browser does by posting to /draw/submit.
    """
    session = current_session(db, user)
    if session is not None:
        resolve_draw_session(db, session, SessionOutcome.TIMED_OUT_EMPTY)
    try:
        assign(db, user, timer_seconds=session.timer_seconds if session else default_timer(db))
    except EmptyPool:
        return JSONResponse({"redirect": "/draw"})
    return JSONResponse({"redirect": "/draw"})


@router.get("/draw/done/{submission_id}")
def done(request: Request, db: DbSession, user: RequireUser, submission_id: int):
    submission = db.get(Submission, submission_id)
    if submission is None or submission.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    pokemon = db.get(Pokemon, submission.pokemon_id)
    settings = get_or_create_settings(db)
    base = settings.public_base_url
    return render(
        request,
        "draw_done.html",
        pokemon=pokemon,
        submission=submission,
        image_path=f"/images/{submission.file_path}",
        public_url=resolve_public_url(base, submission.file_path) if base else None,
        pending=SubmissionStatus(submission.status) is SubmissionStatus.PENDING,
    )


@router.get("/draw/timers")
def timers(user: RequireUser):
    return {"choices": [t if t is not None else "off" for t in TIMER_CHOICES]}
