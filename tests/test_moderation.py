"""Scope 9: approval, rejection, deletion and the uniqueId lifecycle."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Submission, SubmissionStatus, get_or_create_settings
from app.moderation import (
    ModerationError,
    approve,
    delete,
    reject,
    unapprove,
    unreject,
)
from app.picker import drawing_counts
from app.submissions import gallery_for, review_queue, status_counts
from app.users import create_user
from tests.test_picker import add_submission, make_catalog


@pytest.fixture()
def user(session):
    u = create_user(session, username="alex", password="x" * 12, display_name="Alex")
    session.flush()
    return u


@pytest.fixture()
def catalog(session):
    return make_catalog(session, 4)


@pytest.fixture()
def pending(session, catalog, user):
    return add_submission(session, catalog[0], user, status=SubmissionStatus.PENDING)


# --- approval ---------------------------------------------------------------

def test_approving_assigns_a_unique_id_and_snapshots_the_credit(session, pending):
    approve(session, pending)
    assert SubmissionStatus(pending.status) is SubmissionStatus.APPROVED
    assert pending.unique_id == 1
    assert pending.credit_name == "Alex"
    assert pending.approved_at is not None


def test_unique_ids_increase_and_are_not_recycled(session, catalog, user):
    first = add_submission(session, catalog[0], user, status=SubmissionStatus.PENDING)
    second = add_submission(session, catalog[1], user, status=SubmissionStatus.PENDING)
    approve(session, first)
    approve(session, second)
    assert (first.unique_id, second.unique_id) == (1, 2)


def test_reapproving_an_already_approved_row_does_not_change_its_id(session, pending):
    approve(session, pending)
    original = pending.unique_id
    approve(session, pending)
    # Frozen fields are written once and never rewritten.
    assert pending.unique_id == original


def test_credit_is_snapshotted_not_read_live(session, pending, user):
    approve(session, pending)
    user.display_name = "Alexandra"
    session.flush()
    # A published row must not mutate when the artist renames themselves.
    assert pending.credit_name == "Alex"


def test_public_url_is_frozen_when_the_base_is_configured(session, pending):
    get_or_create_settings(session).public_base_url = "https://pokedraw.example.com"
    session.flush()
    approve(session, pending)
    assert pending.public_url.startswith("https://pokedraw.example.com/images/")
    assert pending.public_url.endswith(".png")


def test_approval_is_not_blocked_when_the_base_url_is_unset(session, pending):
    """Auto-approve fires on submit, possibly before an Admin has set anything."""
    approve(session, pending)
    assert pending.unique_id is not None
    assert pending.public_url is None  # backfilled by the export


def test_a_deleted_drawing_cannot_be_approved(session, pending):
    delete(session, pending)
    with pytest.raises(ModerationError, match="deleted"):
        approve(session, pending)


# --- unapproving (decision A5) ----------------------------------------------

def test_unapproving_retires_the_unique_id(session, pending):
    approve(session, pending)
    retired = pending.unique_id
    outcome = unapprove(session, pending)

    assert outcome.retired_unique_id == retired
    assert pending.unique_id is None
    assert SubmissionStatus(pending.status) is SubmissionStatus.PENDING


def test_unapproving_clears_the_whole_frozen_payload(session, pending):
    get_or_create_settings(session).public_base_url = "https://x.example.com"
    session.flush()
    approve(session, pending)
    unapprove(session, pending)
    assert pending.credit_name is None
    assert pending.public_url is None
    assert pending.approved_at is None


def test_reapproval_after_unapprove_mints_a_new_id(session, pending):
    """Decision A5: not a reversible toggle. The old question is gone."""
    approve(session, pending)
    first_id = pending.unique_id
    unapprove(session, pending)
    approve(session, pending)
    assert pending.unique_id != first_id
    assert pending.unique_id > first_id


def test_a_retired_id_is_never_given_to_another_row(session, catalog, user):
    first = add_submission(session, catalog[0], user, status=SubmissionStatus.PENDING)
    approve(session, first)
    retired = first.unique_id
    unapprove(session, first)

    second = add_submission(session, catalog[1], user, status=SubmissionStatus.PENDING)
    approve(session, second)
    assert second.unique_id != retired


def test_only_approved_rows_can_be_unapproved(session, pending):
    with pytest.raises(ModerationError, match="only an approved"):
        unapprove(session, pending)


# --- rejection --------------------------------------------------------------

def test_rejecting_still_counts_toward_the_picker(session, catalog, user):
    submission = add_submission(session, catalog[0], user, status=SubmissionStatus.PENDING)
    reject(session, submission)
    # Scope 9: rejection must not be a re-roll loophole.
    assert drawing_counts(session)[catalog[0].id] == 1


def test_rejecting_an_approved_row_retires_its_id(session, pending):
    approve(session, pending)
    retired = pending.unique_id
    outcome = reject(session, pending)
    assert outcome.retired_unique_id == retired
    assert pending.unique_id is None


def test_rejected_drawings_stay_in_the_artists_gallery(session, pending, user):
    reject(session, pending)
    assert [d.id for d in gallery_for(session, user)] == [pending.id]


def test_unrejecting_returns_to_pending_when_approval_is_on(session, pending):
    get_or_create_settings(session).require_approval = True
    session.flush()
    reject(session, pending)
    unreject(session, pending)
    assert SubmissionStatus(pending.status) is SubmissionStatus.PENDING
    assert pending.unique_id is None


def test_unrejecting_approves_directly_when_approval_is_off(session, pending):
    reject(session, pending)
    unreject(session, pending)
    assert SubmissionStatus(pending.status) is SubmissionStatus.APPROVED
    assert pending.unique_id is not None


def test_only_rejected_rows_can_be_unrejected(session, pending):
    with pytest.raises(ModerationError, match="only a rejected"):
        unreject(session, pending)


# --- deletion ---------------------------------------------------------------

def test_deleting_releases_the_answer_unit_back_to_the_picker(session, catalog, user):
    submission = add_submission(session, catalog[0], user)
    assert drawing_counts(session)[catalog[0].id] == 1
    delete(session, submission)
    # Unlike rejection, deletion frees the Pokemon to be drawn again.
    assert drawing_counts(session)[catalog[0].id] == 0


def test_deleting_an_approved_row_retires_its_id(session, pending):
    approve(session, pending)
    retired = pending.unique_id
    outcome = delete(session, pending)
    assert outcome.retired_unique_id == retired


def test_deleted_drawings_vanish_from_the_artists_gallery(session, pending, user):
    delete(session, pending)
    assert gallery_for(session, user) == []


def test_deleting_twice_is_harmless(session, pending):
    delete(session, pending)
    delete(session, pending)
    assert SubmissionStatus(pending.status) is SubmissionStatus.DELETED


def test_deletion_does_not_free_the_index_for_reuse(session, catalog, user):
    from app.images import next_index

    submission = add_submission(session, catalog[0], user, index=1)
    delete(session, submission)
    # Scope 8.1: never reused even if an earlier drawing is deleted.
    assert next_index(session, catalog[0], user) == 2


# --- queue views ------------------------------------------------------------

def test_the_queue_defaults_to_pending(session, catalog, user):
    add_submission(session, catalog[0], user, status=SubmissionStatus.PENDING)
    add_submission(session, catalog[1], user, status=SubmissionStatus.APPROVED)
    assert [d.status for d in review_queue(session)] == ["pending"]


def test_the_queue_can_be_filtered_by_status(session, catalog, user):
    add_submission(session, catalog[0], user, status=SubmissionStatus.APPROVED)
    assert [d.status for d in review_queue(session, status="approved")] == ["approved"]


def test_status_counts_cover_every_status(session, catalog, user):
    add_submission(session, catalog[0], user, status=SubmissionStatus.PENDING)
    add_submission(session, catalog[1], user, status=SubmissionStatus.APPROVED)
    counts = status_counts(session)
    assert counts["pending"] == 1 and counts["approved"] == 1
    assert counts["rejected"] == 0 and counts["deleted"] == 0


def test_gallery_is_newest_first(session, catalog, user):
    a = add_submission(session, catalog[0], user)
    b = add_submission(session, catalog[1], user)
    assert [d.id for d in gallery_for(session, user)][0] == b.id
    assert a.id in [d.id for d in gallery_for(session, user)]


def test_a_gallery_shows_only_its_own_artists_work(session, catalog, user):
    other = create_user(session, username="sam", password="x" * 12)
    session.flush()
    add_submission(session, catalog[0], user)
    add_submission(session, catalog[1], other)
    assert all(d.artist_slug == "alex" for d in gallery_for(session, user))
