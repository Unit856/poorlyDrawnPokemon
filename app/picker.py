"""Weighted picker, scope 6.

Goal: cover the whole dex. Repeats are allowed, but untouched answer units come
first. Every answer unit gets one drawing before any gets a second.

The selection rule is a pure function over a count map so that weighting can be
simulated over the real catalog without writing anything (`app.cli
simulate-picker`), and tested without a database.

No reservation is taken when a Pokemon is assigned (decision A1). Two players
starting a session at the same moment can be handed the same Pokemon and both
drawings are kept -- with roughly a thousand rows in the min tier and 5-8
players, that is a duplicate rather than a fault.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models import COUNTING_STATUSES, Pokemon, Submission


class EmptyPool(RuntimeError):
    """No enabled catalog rows. The catalog has not been seeded, or all rows are disabled."""


def min_tier(counts: Mapping[int, int]) -> list[int]:
    """Ids whose drawing_count equals the lowest count in the pool."""
    if not counts:
        return []
    lowest = min(counts.values())
    return [key for key, value in counts.items() if value == lowest]


def choose(counts: Mapping[int, int], rng: random.Random | None = None) -> int:
    """Restrict to the min tier, then pick uniformly at random (scope 6)."""
    tier = min_tier(counts)
    if not tier:
        raise EmptyPool("no enabled catalog rows to pick from")
    # Sorted so a seeded rng gives reproducible results regardless of dict order.
    tier.sort()
    return (rng or random).choice(tier)


def drawing_counts(db: Session) -> dict[int, int]:
    """drawing_count per enabled catalog row.

    Pending, approved and rejected all count; deleted does not (scope 6). The
    outer join means a row with no submissions at all still appears, with zero.
    """
    rows = db.execute(
        select(Pokemon.id, func.count(Submission.id))
        .outerjoin(
            Submission,
            and_(
                Submission.pokemon_id == Pokemon.id,
                Submission.status.in_([s.value for s in COUNTING_STATUSES]),
            ),
        )
        .where(Pokemon.enabled.is_(True))
        .group_by(Pokemon.id)
    ).all()
    return {row[0]: row[1] for row in rows}


def pick(db: Session, rng: random.Random | None = None) -> Pokemon:
    pokemon_id = choose(drawing_counts(db), rng)
    pokemon = db.get(Pokemon, pokemon_id)
    if pokemon is None:  # pragma: no cover - referential integrity guarantees this
        raise EmptyPool("picked a catalog row that vanished mid-request")
    return pokemon


def coverage(counts: Mapping[int, int]) -> dict[int, int]:
    """Histogram of drawing_count -> number of answer units, for verification."""
    histogram: dict[int, int] = {}
    for value in counts.values():
        histogram[value] = histogram.get(value, 0) + 1
    return dict(sorted(histogram.items()))


def simulate(counts: Mapping[int, int], draws: int, rng: random.Random | None = None) -> dict[int, int]:
    """Run `draws` picks against a copy of the counts, writing nothing.

    Used by `app.cli simulate-picker` to confirm the weighting spreads across the
    whole dex before any drawing code exists (scope 17, step 3).
    """
    working = dict(counts)
    rng = rng or random.Random()
    for _ in range(draws):
        working[choose(working, rng)] += 1
    return working


def sequence(counts: Mapping[int, int], draws: int, rng: random.Random | None = None) -> Sequence[int]:
    """Like `simulate`, but returns the ids picked, in order."""
    working = dict(counts)
    rng = rng or random.Random()
    picked: list[int] = []
    for _ in range(draws):
        choice = choose(working, rng)
        working[choice] += 1
        picked.append(choice)
    return picked
