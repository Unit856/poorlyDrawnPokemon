"""Creating submissions, scope 7 and 9.

Approval status is decided here; *freezing* is not. uniqueId, options,
correct_letter and credit_name are written at first approval by the export path
(scope 10.2, slice 6). An auto-approved submission therefore exists briefly as
"approved but not yet frozen", which is the state slice 6 picks up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.images import ImageRejected, store_drawing
from app.models import (
    Pokemon,
    SessionOutcome,
    Submission,
    SubmissionStatus,
    User,
    get_or_create_settings,
    utcnow,
)


def initial_status(db: Session) -> SubmissionStatus:
    """Scope 9. Approval defaults OFF, so drawings go straight to approved."""
    return (
        SubmissionStatus.PENDING
        if get_or_create_settings(db).require_approval
        else SubmissionStatus.APPROVED
    )


def create_submission(
    db: Session,
    pokemon: Pokemon,
    user: User,
    data: bytes,
    *,
    chosen: bool = False,
) -> Submission:
    """Write the PNG and record the submission.

    The file is written before the row is inserted. If the insert then fails the
    file is orphaned on disk, which is harmless -- nothing links to it and the
    index is not reused. The reverse order would be worse: a row pointing at a
    file that does not exist is a broken public URL.
    """
    stored = store_drawing(db, pokemon, user, data)

    status = initial_status(db)
    submission = Submission(
        pokemon_id=pokemon.id,
        user_id=user.id,
        index=stored.index,
        file_path=stored.filename,
        status=status,
        chosen=chosen,
    )
    try:
        with db.begin_nested():
            db.add(submission)
            db.flush()
    except IntegrityError as exc:
        # The (pokemon, artist, index) unique constraint fired: two submissions
        # raced for the same index. The PNG for the loser is already written and
        # is simply abandoned.
        raise ImageRejected("that drawing index was just taken; please submit again") from exc

    if status is SubmissionStatus.APPROVED:
        # Auto-approve *is* first approval (scope 9), so it must freeze. Without
        # this the row is approved but has no uniqueId, and the export -- which is
        # a pure read of frozen columns -- would never see it.
        from app.moderation import freeze

        freeze(db, submission)
        db.flush()
    return submission


def resolve_draw_session(db: Session, session, outcome: SessionOutcome) -> None:
    session.resolved_at = utcnow()
    session.outcome = outcome
    db.add(session)
    db.flush()


@dataclass(frozen=True)
class SubmissionView:
    """Detached, template-safe row (same reasoning as auth.UserView)."""

    id: int
    pokemon_name: str
    pokemon_slug: str
    artist: str
    artist_slug: str
    status: str
    index: int
    file_path: str
    image_path: str
    created_at: datetime
    unique_id: int | None
    credit_name: str | None

    @classmethod
    def of(cls, submission: Submission, pokemon: Pokemon, user: User) -> "SubmissionView":
        return cls(
            id=submission.id,
            pokemon_name=pokemon.display_name,
            pokemon_slug=pokemon.slug,
            artist=user.display_name,
            artist_slug=user.slug,
            status=SubmissionStatus(submission.status).value,
            index=submission.index,
            file_path=submission.file_path,
            image_path=f"/images/{submission.file_path}",
            created_at=submission.created_at,
            unique_id=submission.unique_id,
            credit_name=submission.credit_name,
        )


def _rows(db: Session, stmt) -> list[SubmissionView]:
    results = db.execute(
        stmt.join(Pokemon, Submission.pokemon_id == Pokemon.id).join(
            User, Submission.user_id == User.id
        ).add_columns(Pokemon, User)
    ).all()
    return [SubmissionView.of(sub, pokemon, user) for sub, pokemon, user in results]


def gallery_for(db: Session, user: User) -> list[SubmissionView]:
    """Scope 7.5: the player's own read-only gallery.

    Deleted submissions do not appear. Rejected ones do -- the artist should be
    able to see that their drawing was turned down.
    """
    stmt = (
        select(Submission)
        .where(
            Submission.user_id == user.id,
            Submission.status != SubmissionStatus.DELETED,
        )
        .order_by(Submission.created_at.desc(), Submission.id.desc())
    )
    return _rows(db, stmt)


def review_queue(db: Session, *, status: str | None = None) -> list[SubmissionView]:
    """Scope 9: the Admin review queue.

    Defaults to pending only -- that is the queue. Other statuses are reachable
    by filter so an Admin can unapprove or unreject after the fact.
    """
    stmt = select(Submission)
    if status:
        stmt = stmt.where(Submission.status == SubmissionStatus(status))
    else:
        stmt = stmt.where(Submission.status == SubmissionStatus.PENDING)
    stmt = stmt.order_by(Submission.created_at.desc(), Submission.id.desc())
    return _rows(db, stmt)


def status_counts(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(Submission.status, func.count()).group_by(Submission.status)
    ).all()
    counts = {s.value: 0 for s in SubmissionStatus}
    for status, count in rows:
        counts[SubmissionStatus(status).value] = count
    return counts


def submission_count(db: Session, user: User) -> int:
    return len(
        db.scalars(
            select(Submission.id).where(
                Submission.user_id == user.id,
                Submission.status != SubmissionStatus.DELETED,
            )
        ).all()
    )
