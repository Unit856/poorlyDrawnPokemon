"""Scope 6: the weighted picker and its counting rules."""

from __future__ import annotations

import random

import pytest

from app.models import Pokemon, Submission, SubmissionStatus
from app.picker import EmptyPool, choose, coverage, drawing_counts, min_tier, pick, sequence
from app.users import create_user


def make_catalog(session, n=5):
    rows = []
    for i in range(1, n + 1):
        row = Pokemon(
            form_key=f"mon{i}",
            display_name=f"Mon {i}",
            slug=f"mon{i}",
            national_dex=i,
            generation=1,
            types=["Normal"],
            species_category="Test Pokémon",
            dex_entries=["Text."],
            enabled=True,
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def add_submission(session, pokemon, user, status=SubmissionStatus.APPROVED, index=1):
    sub = Submission(
        pokemon_id=pokemon.id,
        user_id=user.id,
        index=index,
        file_path=f"{pokemon.slug}-{user.slug}-{index}.png",
        status=status,
    )
    session.add(sub)
    session.flush()
    return sub


@pytest.fixture()
def user(session):
    u = create_user(session, username="alex", password="x" * 12)
    session.flush()
    return u


# --- pure selection ---------------------------------------------------------

def test_min_tier_selects_only_the_lowest_counts():
    assert sorted(min_tier({1: 0, 2: 0, 3: 1})) == [1, 2]


def test_min_tier_of_a_level_pool_is_everything():
    assert sorted(min_tier({1: 2, 2: 2, 3: 2})) == [1, 2, 3]


def test_choose_never_returns_a_row_above_the_min_tier():
    counts = {1: 0, 2: 5, 3: 5}
    rng = random.Random(0)
    assert all(choose(counts, rng) == 1 for _ in range(50))


def test_choose_on_an_empty_pool_raises():
    with pytest.raises(EmptyPool):
        choose({})


def test_every_unit_gets_one_before_any_gets_two():
    """The scope 6 guarantee, and acceptance criterion 5."""
    counts = {i: 0 for i in range(1, 51)}
    picked = sequence(counts, 50, random.Random(7))
    assert len(set(picked)) == 50, "a repeat occurred before full coverage"


def test_after_full_coverage_the_picker_starts_handing_out_seconds():
    counts = {i: 0 for i in range(1, 11)}
    picked = sequence(counts, 20, random.Random(7))
    first_pass, second_pass = picked[:10], picked[10:]
    assert set(first_pass) == set(second_pass) == set(counts)


def test_the_spread_never_exceeds_one_within_a_run():
    counts = {i: 0 for i in range(1, 31)}
    rng = random.Random(3)
    working = dict(counts)
    for _ in range(95):
        working[choose(working, rng)] += 1
        assert max(working.values()) - min(working.values()) <= 1


def test_choose_is_reproducible_for_a_seeded_rng():
    counts = {i: 0 for i in range(1, 21)}
    a = sequence(counts, 20, random.Random(42))
    b = sequence(counts, 20, random.Random(42))
    assert a == b


def test_coverage_histogram():
    assert coverage({1: 0, 2: 0, 3: 1, 4: 2}) == {0: 2, 1: 1, 2: 1}


# --- counting rules against the database ------------------------------------

def test_untouched_rows_count_zero(session, user):
    rows = make_catalog(session, 3)
    assert drawing_counts(session) == {r.id: 0 for r in rows}


@pytest.mark.parametrize(
    "status, counts",
    [
        (SubmissionStatus.PENDING, 1),
        (SubmissionStatus.APPROVED, 1),
        (SubmissionStatus.REJECTED, 1),
        (SubmissionStatus.DELETED, 0),
    ],
)
def test_status_counting_rules(session, user, status, counts):
    """Pending, approved and rejected all count; deleted releases the row."""
    rows = make_catalog(session, 2)
    add_submission(session, rows[0], user, status=status)
    assert drawing_counts(session)[rows[0].id] == counts


def test_rejected_still_counts_so_rejection_is_not_a_reroll_loophole(session, user):
    rows = make_catalog(session, 2)
    add_submission(session, rows[0], user, status=SubmissionStatus.REJECTED)
    # rows[0] is now in a higher tier, so the picker must hand out rows[1].
    assert pick(session, random.Random(0)).id == rows[1].id


def test_deleting_makes_an_answer_unit_eligible_again(session, user):
    rows = make_catalog(session, 2)
    sub = add_submission(session, rows[0], user, status=SubmissionStatus.APPROVED)
    assert drawing_counts(session)[rows[0].id] == 1
    sub.status = SubmissionStatus.DELETED
    session.flush()
    assert drawing_counts(session)[rows[0].id] == 0


def test_disabled_rows_are_never_picked(session, user):
    rows = make_catalog(session, 3)
    for row in rows[1:]:
        row.enabled = False
    session.flush()
    counts = drawing_counts(session)
    assert set(counts) == {rows[0].id}
    assert all(pick(session, random.Random(i)).id == rows[0].id for i in range(10))


def test_pick_on_an_unseeded_catalog_raises(session):
    with pytest.raises(EmptyPool):
        pick(session)


def test_multiple_drawings_by_the_same_artist_both_count(session, user):
    rows = make_catalog(session, 2)
    add_submission(session, rows[0], user, index=1)
    add_submission(session, rows[0], user, index=2)
    assert drawing_counts(session)[rows[0].id] == 2
