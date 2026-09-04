"""Approval, rejection and deletion, scope 9.

The uniqueId lifecycle lives here because approval is what creates and destroys
it. Two rules drive everything below:

* A uniqueId is assigned at *first* approval and is **never reused by any row**.
  Retirement simply clears it; the counter in Settings only ever increases, so a
  retired number can never be handed out again.
* Unapproving is not a reversible toggle from the pack's point of view. Approving
  the same drawing again mints a *new* uniqueId and the old question is gone
  (decision A5). Players who saw the retired question meet the drawing again
  under a new number.

Counting rules (scope 6) fall out of the status alone: pending, approved and
rejected all count toward drawing_count; only deleted releases the answer unit
back to the picker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.export import FALLBACK_CREDIT, NotEnoughCatalog, build_options, enabled_names
from app.images import resolve_public_url
from app.models import (
    Pokemon,
    Submission,
    SubmissionStatus,
    User,
    allocate_unique_id,
    get_or_create_settings,
    utcnow,
)

log = logging.getLogger(__name__)


class ModerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Outcome:
    submission_id: int
    status: SubmissionStatus
    retired_unique_id: int | None = None
    assigned_unique_id: int | None = None


def _retire(db: Session, submission: Submission) -> int | None:
    """Drop the question out of the pack permanently.

    Clears the whole frozen payload, not just the id: if this drawing is ever
    approved again it must be frozen afresh, never resurrected (scope 9).
    """
    retired = submission.unique_id
    submission.unique_id = None
    submission.options_json = None
    submission.correct_letter = None
    submission.credit_name = None
    submission.public_url = None
    submission.approved_at = None
    db.add(submission)
    return retired


def freeze(db: Session, submission: Submission, *, rng=None) -> None:
    """Write the once-only fields at first approval (scope 10.2).

    Everything here is written exactly once and never rewritten. Two fields are
    allowed to come out empty, and the export backfills them:

    * `public_url`, when a drawing is auto-approved before an Admin has ever set
      the public base URL. Approval must not be blocked on a setting that
      auto-approve reaches first.
    * `options_json`, if the catalog somehow holds fewer than four enabled rows.
    """
    if submission.unique_id is not None:
        return  # already frozen; never rewritten

    submission.unique_id = allocate_unique_id(db)

    # Snapshotted so a later display-name change cannot mutate a published row.
    artist = db.get(User, submission.user_id)
    submission.credit_name = artist.display_name if artist else FALLBACK_CREDIT

    pokemon = db.get(Pokemon, submission.pokemon_id)
    names = enabled_names(db)
    if pokemon.display_name not in names:
        names = names + [pokemon.display_name]
    try:
        options, letter = build_options(names, pokemon.display_name, rng)
        submission.options_json = options
        submission.correct_letter = letter
    except NotEnoughCatalog as exc:
        # Degenerate catalog. Approval still succeeds; the export will complete
        # the freeze or refuse with a clear message.
        log.warning("could not freeze options for submission %s: %s", submission.id, exc)

    base = get_or_create_settings(db).public_base_url
    if base:
        submission.public_url = resolve_public_url(base, submission.file_path)

    submission.approved_at = utcnow()
    db.add(submission)


def approve(db: Session, submission: Submission) -> Outcome:
    status = SubmissionStatus(submission.status)
    if status is SubmissionStatus.DELETED:
        raise ModerationError("a deleted drawing cannot be approved")

    already = submission.unique_id
    submission.status = SubmissionStatus.APPROVED
    freeze(db, submission)
    db.flush()
    return Outcome(
        submission.id,
        SubmissionStatus.APPROVED,
        assigned_unique_id=None if already else submission.unique_id,
    )


def unapprove(db: Session, submission: Submission) -> Outcome:
    if SubmissionStatus(submission.status) is not SubmissionStatus.APPROVED:
        raise ModerationError("only an approved drawing can be unapproved")

    retired = _retire(db, submission)
    submission.status = SubmissionStatus.PENDING
    db.flush()
    return Outcome(submission.id, SubmissionStatus.PENDING, retired_unique_id=retired)


def reject(db: Session, submission: Submission) -> Outcome:
    status = SubmissionStatus(submission.status)
    if status is SubmissionStatus.DELETED:
        raise ModerationError("a deleted drawing cannot be rejected")

    retired = _retire(db, submission) if status is SubmissionStatus.APPROVED else None
    submission.status = SubmissionStatus.REJECTED
    db.flush()
    # Still counts toward drawing_count: rejection must not be a re-roll loophole.
    return Outcome(submission.id, SubmissionStatus.REJECTED, retired_unique_id=retired)


def unreject(db: Session, submission: Submission) -> Outcome:
    if SubmissionStatus(submission.status) is not SubmissionStatus.REJECTED:
        raise ModerationError("only a rejected drawing can be unrejected")

    # Back to pending, or straight to approved when approval is switched off.
    if get_or_create_settings(db).require_approval:
        submission.status = SubmissionStatus.PENDING
        db.flush()
        return Outcome(submission.id, SubmissionStatus.PENDING)
    return approve(db, submission)


def delete(db: Session, submission: Submission) -> Outcome:
    """Admin-only (scope 3).

    Releases the answer unit back to the picker, unlike rejection. The file may
    stay on disk and the index is still never reused.
    """
    if SubmissionStatus(submission.status) is SubmissionStatus.DELETED:
        return Outcome(submission.id, SubmissionStatus.DELETED)

    retired = _retire(db, submission)
    submission.status = SubmissionStatus.DELETED
    submission.deleted_at = utcnow()
    db.flush()
    return Outcome(submission.id, SubmissionStatus.DELETED, retired_unique_id=retired)


ACTIONS = {
    "approve": approve,
    "unapprove": unapprove,
    "reject": reject,
    "unreject": unreject,
    "delete": delete,
}
