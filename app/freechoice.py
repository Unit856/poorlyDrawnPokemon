"""Earned free picks.

The weighted picker exists so the whole dex gets covered (scope 6), and letting
anyone draw whatever they like would quietly defeat that: favourites accumulate
drawings while the long tail stays empty.

So choice is *earned*. Every `free_choice_quota` picker-assigned drawings buys
one free pick of any enabled Pokemon. Most drawings still come from the picker,
so coverage keeps advancing, and choosing becomes a reward rather than an escape
hatch.

Accounting is derived, never stored as a running balance:

    earned    = picker-assigned submissions // quota
    spent     = submissions made with a free pick
    available = earned - spent

Deriving it means there is no counter to drift, double-spend or repair. Only
picker-assigned drawings count toward `earned`, so a free pick can never pay for
the next one.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Submission, SubmissionStatus, User, get_or_create_settings


@dataclass(frozen=True)
class Balance:
    quota: int
    earned: int
    spent: int
    counted: int

    @property
    def enabled(self) -> bool:
        return self.quota > 0

    @property
    def available(self) -> int:
        return max(0, self.earned - self.spent)

    @property
    def toward_next(self) -> int:
        """Picker-assigned drawings completed since the last full quota."""
        return self.counted % self.quota if self.quota else 0

    @property
    def remaining(self) -> int:
        """How many more picker-assigned drawings buy the next pick."""
        if not self.quota:
            return 0
        return self.quota - self.toward_next


def _count(db: Session, user: User, *, chosen: bool) -> int:
    # Deleted drawings do not count. An Admin deleting a drawing releases the
    # answer unit back to the picker (scope 9), so it should not still be paying
    # for a free pick either. Rejected ones do count: the artist did the work,
    # and rejection is a quality call, not a reversal of effort.
    return (
        db.scalar(
            select(func.count())
            .select_from(Submission)
            .where(
                Submission.user_id == user.id,
                Submission.chosen.is_(chosen),
                Submission.status != SubmissionStatus.DELETED,
            )
        )
        or 0
    )


def balance(db: Session, user: User) -> Balance:
    quota = max(0, get_or_create_settings(db).free_choice_quota or 0)
    counted = _count(db, user, chosen=False)
    spent = _count(db, user, chosen=True)
    earned = counted // quota if quota else 0
    return Balance(quota=quota, earned=earned, spent=spent, counted=counted)


def can_choose(db: Session, user: User) -> bool:
    state = balance(db, user)
    return state.enabled and state.available > 0
