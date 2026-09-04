"""Earned free picks: quota accounting, and that scope 6 coverage survives it."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.draw import assign, current_session, skip
from app.freechoice import balance, can_choose
from app.models import DrawSession, Pokemon, Submission, SubmissionStatus, get_or_create_settings
from app.users import create_user
from tests.test_picker import add_submission, make_catalog


@pytest.fixture()
def user(session):
    u = create_user(session, username="alex", password="x" * 12, display_name="Alex")
    session.flush()
    return u


@pytest.fixture()
def catalog(session):
    return make_catalog(session, 12)


def set_quota(session, value):
    get_or_create_settings(session).free_choice_quota = value
    session.flush()


def drawn(session, catalog, user, n, *, chosen=False, status=SubmissionStatus.APPROVED):
    """Add n submissions, allocating indexes the way the app does.

    Using the real allocator rather than a local counter, so repeated calls for
    the same (pokemon, artist) pair cannot collide on the unique constraint.
    """
    from app.images import next_index

    for i in range(n):
        pokemon = catalog[i % len(catalog)]
        sub = add_submission(
            session, pokemon, user, status=status, index=next_index(session, pokemon, user)
        )
        sub.chosen = chosen
    session.flush()


# --- accounting -------------------------------------------------------------

def test_no_picks_before_the_quota_is_met(session, catalog, user):
    set_quota(session, 5)
    drawn(session, catalog, user, 4)
    state = balance(session, user)
    assert state.available == 0
    assert state.toward_next == 4
    assert state.remaining == 1
    assert not can_choose(session, user)


def test_one_pick_earned_at_the_quota(session, catalog, user):
    set_quota(session, 5)
    drawn(session, catalog, user, 5)
    assert balance(session, user).available == 1
    assert can_choose(session, user)


def test_picks_accrue(session, catalog, user):
    set_quota(session, 3)
    drawn(session, catalog, user, 9)
    assert balance(session, user).available == 3


def test_spending_a_pick_reduces_the_balance(session, catalog, user):
    set_quota(session, 5)
    drawn(session, catalog, user, 5)
    assert balance(session, user).available == 1

    chosen = add_submission(session, catalog[0], user, index=99)
    chosen.chosen = True
    session.flush()
    assert balance(session, user).available == 0


def test_a_free_pick_never_pays_for_the_next_one(session, catalog, user):
    """The rule that stops the feature bootstrapping itself."""
    set_quota(session, 2)
    drawn(session, catalog, user, 2)          # earns 1
    drawn(session, catalog, user, 4, chosen=True)  # spends, and must not earn

    state = balance(session, user)
    assert state.counted == 2, "chosen drawings must not count toward the quota"
    assert state.earned == 1
    assert state.spent == 4
    assert state.available == 0


def test_balance_never_goes_negative(session, catalog, user):
    set_quota(session, 5)
    drawn(session, catalog, user, 3, chosen=True)
    assert balance(session, user).available == 0


def test_quota_zero_disables_the_feature(session, catalog, user):
    set_quota(session, 0)
    drawn(session, catalog, user, 50)
    state = balance(session, user)
    assert not state.enabled
    assert state.available == 0
    assert state.remaining == 0
    assert not can_choose(session, user)


def test_deleted_drawings_stop_counting(session, catalog, user):
    """Deletion releases the answer unit, so it should not still buy a pick."""
    set_quota(session, 3)
    drawn(session, catalog, user, 3)
    assert balance(session, user).available == 1

    session.scalars(select(Submission)).first().status = SubmissionStatus.DELETED
    session.flush()
    assert balance(session, user).available == 0


def test_rejected_drawings_still_count(session, catalog, user):
    """The artist did the work; rejection is a quality call, not a reversal."""
    set_quota(session, 3)
    drawn(session, catalog, user, 3, status=SubmissionStatus.REJECTED)
    assert balance(session, user).available == 1


def test_pending_drawings_count(session, catalog, user):
    set_quota(session, 2)
    drawn(session, catalog, user, 2, status=SubmissionStatus.PENDING)
    assert balance(session, user).available == 1


def test_balances_are_per_player(session, catalog, user):
    set_quota(session, 2)
    other = create_user(session, username="sam", password="x" * 12)
    session.flush()
    drawn(session, catalog, user, 4)
    assert balance(session, user).available == 2
    assert balance(session, other).available == 0


# --- assignment -------------------------------------------------------------

def test_choosing_bypasses_the_picker(session, catalog, user):
    target = catalog[7]
    created = assign(session, user, choice=target)
    assert created.pokemon_id == target.id
    assert created.chosen is True


def test_a_picker_session_is_not_flagged(session, catalog, user):
    assert assign(session, user).chosen is False


def test_the_chosen_flag_survives_a_skip_into_a_normal_draw(session, catalog, user):
    """Skipping a chosen session must not carry the flag onto the next one."""
    assign(session, user, choice=catalog[3])
    replacement = skip(session, user)
    assert replacement.chosen is False


def test_choosing_respects_one_open_session(session, catalog, user):
    first = assign(session, user)
    second = assign(session, user, choice=catalog[5])
    # scope 7.1: an existing assignment is resumed, never replaced.
    assert second.id == first.id
    assert second.chosen is False


# --- coverage is preserved --------------------------------------------------

def test_free_picks_do_not_break_the_pickers_coverage_guarantee(session, catalog, user):
    """A chosen repeat must not stop the picker covering untouched rows."""
    from app.picker import drawing_counts, min_tier

    set_quota(session, 2)
    # Two picker drawings on catalog[0], then a free pick on the same Pokemon.
    drawn(session, catalog, user, 2)
    repeat = add_submission(session, catalog[0], user, index=50)
    repeat.chosen = True
    session.flush()

    counts = drawing_counts(session)
    tier = min_tier(counts)
    # Everything untouched is still in the min tier and will be dealt first.
    untouched = [p.id for p in catalog[2:]]
    assert all(pid in tier for pid in untouched)
    assert catalog[0].id not in tier
