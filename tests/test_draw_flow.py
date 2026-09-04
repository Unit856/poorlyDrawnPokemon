"""Scope 7.1 session resume, 7.4 timers, and the scope 5.3 hint panel."""

from __future__ import annotations

import random

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.auth import get_db
from app.draw import assign, current_session, hints, normalise_timer, skip
from app.models import DrawSession, Pokemon, SessionOutcome, Submission
from app.ratelimit import login_limiter
from app.users import create_user
from tests.test_picker import make_catalog


@pytest.fixture()
def player(session):
    user = create_user(session, username="alex", password="battery-staple")
    session.commit()
    return user


@pytest.fixture()
def catalog(session):
    rows = make_catalog(session, 6)
    session.commit()
    return rows


@pytest.fixture()
def client(session, player):
    # Depends on `player` explicitly: without it the login below runs before the
    # account exists, every request 303s to /login, and assertions that merely
    # check for the *absence* of something pass vacuously.
    from app.main import app

    def override_db():
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    app.dependency_overrides[get_db] = override_db
    login_limiter.clear()
    with TestClient(app, follow_redirects=False) as c:
        logged_in = c.post("/login", data={"username": "alex", "password": "battery-staple"})
        assert logged_in.status_code == 303, "fixture login failed; tests would pass vacuously"
        yield c
    app.dependency_overrides.clear()


# --- timer normalisation ----------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("60", 60), ("90", 90), ("120", 120), ("180", 180),
        ("", None), ("off", None), (None, None),
        ("45", None), ("999", None), ("banana", None), ("-60", None),
    ],
)
def test_timer_choices_are_restricted_to_the_scope_list(raw, expected):
    assert normalise_timer(raw) == expected


# --- assignment and resume --------------------------------------------------

def test_assign_creates_one_open_session(session, player, catalog):
    created = assign(session, player, timer_seconds=90)
    assert created.resolved_at is None
    assert created.timer_seconds == 90
    assert current_session(session, player).id == created.id


def test_assigning_again_resumes_rather_than_rerolling(session, player, catalog):
    first = assign(session, player)
    second = assign(session, player)
    # Scope 7.1: hitting Draw again must not hand out a different Pokemon.
    assert second.id == first.id
    assert second.pokemon_id == first.pokemon_id


def test_only_one_open_session_exists_per_player(session, player, catalog):
    for _ in range(5):
        assign(session, player)
    open_count = session.scalar(
        select(func.count())
        .select_from(DrawSession)
        .where(DrawSession.user_id == player.id, DrawSession.resolved_at.is_(None))
    )
    assert open_count == 1


def test_two_players_hold_independent_sessions(session, player, catalog):
    other = create_user(session, username="sam", password="x" * 12)
    session.flush()
    assign(session, player)
    assign(session, other)
    assert current_session(session, player).id != current_session(session, other).id


# --- skip -------------------------------------------------------------------

def test_skip_resolves_the_old_session_and_opens_a_new_one(session, player, catalog):
    first = assign(session, player)
    second = skip(session, player, rng=random.Random(1))

    session.refresh(first)
    assert first.resolved_at is not None
    assert SessionOutcome(first.outcome) is SessionOutcome.SKIPPED
    assert second.id != first.id
    assert second.resolved_at is None


def test_skip_creates_no_submission(session, player, catalog):
    assign(session, player)
    for _ in range(5):
        skip(session, player, rng=random.Random(2))
    # Acceptance criterion 6.
    assert session.scalar(select(func.count()).select_from(Submission)) == 0


def test_skip_does_not_change_drawing_count(session, player, catalog):
    from app.picker import drawing_counts

    before = drawing_counts(session)
    assign(session, player)
    for _ in range(10):
        skip(session, player, rng=random.Random(3))
    assert drawing_counts(session) == before


def test_a_skipped_pokemon_can_return_with_no_cooldown(session, player, catalog):
    """Scope 6: no cooldown in v1."""
    assign(session, player)
    seen = set()
    for i in range(40):
        seen.add(skip(session, player, rng=random.Random(i)).pokemon_id)
    # With 6 rows and 40 skips, repeats are unavoidable and permitted.
    assert len(seen) > 1


def test_skip_carries_the_timer_choice_to_the_new_assignment(session, player, catalog):
    assign(session, player, timer_seconds=120)
    assert skip(session, player, rng=random.Random(0)).timer_seconds == 120


def test_skip_with_no_open_session_is_a_no_op(session, player, catalog):
    assert skip(session, player) is None


# --- hint panel -------------------------------------------------------------

def test_hints_expose_only_the_scope_5_3_fields(session, catalog):
    panel = hints(catalog[0])
    assert set(panel) == {"name", "species", "generation", "types", "dex_entries", "slug"}


def test_hints_cap_dex_entries_at_three(session, catalog):
    catalog[0].dex_entries = ["a", "b", "c", "d", "e"]
    session.flush()
    assert len(hints(catalog[0])["dex_entries"]) == 3


# --- routes -----------------------------------------------------------------

def test_draw_page_shows_the_assigned_pokemon(client, session, player, catalog):
    response = client.get("/draw")
    assert response.status_code == 200
    assigned = session.get(Pokemon, current_session(session, player).pokemon_id)
    assert assigned.display_name in response.text


def test_draw_page_never_shows_an_image(client, catalog):
    """Scope 5.3: no artwork, sprites, cries or silhouettes, ever.

    Checks for image *markup and URLs*, not for the word "sprite" -- the page
    copy says out loud that it shows none, and that prose is not a violation.
    """
    body = client.get("/draw").text.lower()
    for marker in ("<img", "<picture", "<svg", "<audio", "background-image", "pokeapi.co", ".png", ".jpg", ".gif"):
        assert marker not in body, f"draw page leaked {marker!r}"


def test_reloading_the_draw_page_keeps_the_same_pokemon(client, catalog):
    first = client.get("/draw").text
    for _ in range(4):
        assert client.get("/draw").text == first


def test_skip_route_changes_the_assignment_eventually(client, session, player, catalog):
    before = current_session(session, player)
    original = client.get("/draw") and current_session(session, player).pokemon_id
    changed = False
    for _ in range(30):
        assert client.post("/draw/skip").status_code == 303
        if current_session(session, player).pokemon_id != original:
            changed = True
            break
    assert changed


def test_draw_start_does_not_reroll_an_open_session(client, session, player, catalog):
    client.get("/draw")
    original = current_session(session, player).pokemon_id
    client.post("/draw/start", data={"timer": "60"})
    assert current_session(session, player).pokemon_id == original


def test_unseeded_catalog_gives_a_helpful_page_not_a_crash(client, session, player):
    response = client.get("/draw")
    assert response.status_code == 200
    assert "Nothing to draw" in response.text
    assert "app.cli seed" in response.text


def test_draw_requires_login(session, catalog):
    from app.main import app

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, follow_redirects=False) as anon:
        assert anon.get("/draw").status_code == 303
    app.dependency_overrides.clear()


def test_timer_renders_when_set(client, session, player, catalog):
    client.post("/draw/start", data={"timer": "90"})
    assert 'data-seconds="90"' in client.get("/draw").text


def test_no_timer_renders_as_off(client, catalog):
    client.post("/draw/start", data={"timer": ""})
    assert "No timer" in client.get("/draw").text


def test_a_second_open_session_is_impossible(session, player, catalog):
    """The partial unique index is the guarantee resume depends on."""
    from sqlalchemy.exc import IntegrityError

    assign(session, player)
    session.add(DrawSession(user_id=player.id, pokemon_id=catalog[0].id))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_losing_the_assign_race_resumes_instead_of_erroring(session, player, catalog):
    """Two rapid clicks on Draw must not surface a 500.

    Simulates the loser of the race: a row already exists, and the caller's
    check-then-insert has gone stale.
    """
    winner = assign(session, player)

    # Force the stale path: insert directly, bypassing assign's own guard.
    import app.draw as draw_module

    real_current = draw_module.current_session
    calls = {"n": 0}

    def stale_then_true(db, user):
        calls["n"] += 1
        # First call (assign's guard) pretends nothing is open; the retry after
        # IntegrityError sees reality.
        return None if calls["n"] == 1 else real_current(db, user)

    draw_module.current_session = stale_then_true
    try:
        resumed = draw_module.assign(session, player)
    finally:
        draw_module.current_session = real_current

    assert resumed.id == winner.id
