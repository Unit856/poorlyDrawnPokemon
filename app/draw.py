"""Draw sessions, scope 7.1 and 7.4.

The assignment lives server-side against the player, not in the browser tab. A
reload, a new tab, or a fresh login all resume the *same* Pokemon -- reloading is
not a re-roll, and Skip is the only way to change the assignment.

Elapsed time is deliberately never stored. The timer restarts at full duration on
resume (scope 7.1), which falls out of not persisting it rather than needing code.
"""

from __future__ import annotations

import random

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import DrawSession, Pokemon, SessionOutcome, User, get_or_create_settings, utcnow
from app.picker import pick

#: Scope 7.4. None means "Off".
TIMER_CHOICES: tuple[int | None, ...] = (None, 60, 90, 120, 180)


def normalise_timer(value: int | str | None) -> int | None:
    """Coerce a submitted timer choice to one of the allowed values."""
    if value in (None, "", "off", "Off"):
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds in TIMER_CHOICES else None


def current_session(db: Session, user: User) -> DrawSession | None:
    """The player's open assignment, if any."""
    return db.scalar(
        select(DrawSession)
        .where(DrawSession.user_id == user.id, DrawSession.resolved_at.is_(None))
        .order_by(DrawSession.id.desc())
    )


def assign(
    db: Session,
    user: User,
    *,
    timer_seconds: int | None = None,
    rng: random.Random | None = None,
) -> DrawSession:
    """Open a session, or return the existing one untouched.

    Returning the existing session rather than re-rolling is the whole point of
    scope 7.1: hitting Draw again must not hand out a different Pokemon.
    """
    existing = current_session(db, user)
    if existing is not None:
        return existing

    pokemon = pick(db, rng)
    session = DrawSession(
        user_id=user.id,
        pokemon_id=pokemon.id,
        timer_seconds=timer_seconds,
    )
    try:
        # Savepoint, so losing this race does not poison the whole transaction.
        with db.begin_nested():
            db.add(session)
            db.flush()
    except IntegrityError:
        # The partial unique index caught a concurrent insert -- two rapid
        # clicks on Draw is the realistic way in. Whoever won created the
        # assignment we should be resuming anyway.
        raced = current_session(db, user)
        if raced is None:  # pragma: no cover - constraint fired for another reason
            raise
        return raced
    return session


def resolve(db: Session, session: DrawSession, outcome: SessionOutcome) -> DrawSession:
    session.resolved_at = utcnow()
    session.outcome = outcome
    db.add(session)
    db.flush()
    return session


def skip(db: Session, user: User, *, rng: random.Random | None = None) -> DrawSession | None:
    """Resolve the open session as skipped and immediately roll a new pick.

    Skip creates no Submission and does not change drawing_count, so a skipped
    Pokemon can be handed out again straight away -- there is no cooldown in v1
    (scope 6).
    """
    existing = current_session(db, user)
    if existing is None:
        return None

    timer = existing.timer_seconds
    resolve(db, existing, SessionOutcome.SKIPPED)
    return assign(db, user, timer_seconds=timer, rng=rng)


def default_timer(db: Session) -> int | None:
    return normalise_timer(get_or_create_settings(db).default_timer_seconds)


def hints(pokemon: Pokemon) -> dict:
    """The scope 5.3 hint panel. Never any artwork, sprite, cry or silhouette."""
    return {
        "name": pokemon.display_name,
        "species": pokemon.species_category,
        "generation": pokemon.generation,
        "types": list(pokemon.types or []),
        "dex_entries": list(pokemon.dex_entries or [])[:3],
        "slug": pokemon.slug,
    }
