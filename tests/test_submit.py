"""Scope 7.3, 7.4, 8.2 and 12: submitting, timing out, and serving."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import config
from app.auth import get_db
from app.draw import assign, current_session
from app.models import DrawSession, SessionOutcome, Submission, SubmissionStatus, get_or_create_settings
from app.ratelimit import login_limiter, submit_limiter
from app.users import create_user
from tests.test_images import blank_png, png_bytes
from tests.test_picker import make_catalog


@pytest.fixture(autouse=True)
def isolated_images(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "images")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "images").mkdir(parents=True, exist_ok=True)
    yield


@pytest.fixture()
def player(session):
    user = create_user(session, username="alex", password="battery-staple")
    session.commit()
    return user


@pytest.fixture()
def catalog(session):
    rows = make_catalog(session, 4)
    session.commit()
    return rows


@pytest.fixture()
def client(session, player, catalog):
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
    submit_limiter.clear()
    with TestClient(app, follow_redirects=False) as c:
        assert c.post(
            "/login", data={"username": "alex", "password": "battery-staple"}
        ).status_code == 303
        yield c
    app.dependency_overrides.clear()


def post_drawing(client, data=None, strokes=3):
    return client.post(
        "/draw/submit",
        files={"image": ("drawing.png", data or png_bytes(), "image/png")},
        data={"strokes": str(strokes)},
    )


# --- submitting -------------------------------------------------------------

def test_submit_creates_a_submission_and_writes_the_png(client, session, player):
    client.get("/draw")
    response = post_drawing(client)
    assert response.status_code == 200, response.text

    submission = session.scalar(select(Submission))
    assert submission is not None
    assert submission.index == 1
    assert (config.IMAGES_DIR / submission.file_path).is_file()


def test_submit_resolves_the_draw_session(client, session, player):
    client.get("/draw")
    post_drawing(client)
    resolved = session.scalar(select(DrawSession).where(DrawSession.resolved_at.is_not(None)))
    assert SessionOutcome(resolved.outcome) is SessionOutcome.SUBMITTED
    assert current_session(session, player) is None


def test_submitted_file_matches_the_filename_contract(client, session, player):
    client.get("/draw")
    post_drawing(client)
    submission = session.scalar(select(Submission))
    assert submission.file_path.endswith(".png")
    assert submission.file_path.count("-") >= 2


def test_zero_strokes_is_refused_with_the_scope_message(client, session):
    client.get("/draw")
    response = post_drawing(client, strokes=0)
    assert response.status_code == 400
    assert response.json()["error"] == "Draw something first."
    # Scope 7.3: no submission row is created.
    assert session.scalar(select(func.count()).select_from(Submission)) == 0


def test_a_blank_canvas_is_refused_even_if_strokes_are_claimed(client, session):
    """Backstop: never publish an empty image to a permanent URL."""
    client.get("/draw")
    response = post_drawing(client, data=blank_png(), strokes=5)
    assert response.status_code == 400
    assert "empty" in response.json()["error"]
    assert session.scalar(select(func.count()).select_from(Submission)) == 0


def test_a_wrong_size_canvas_is_refused(client, session):
    client.get("/draw")
    response = post_drawing(client, data=png_bytes(size=(256, 256)))
    assert response.status_code == 400
    assert "800x800" in response.json()["error"]


def test_submitting_with_no_open_session_is_a_conflict(client, session, player):
    response = post_drawing(client)
    assert response.status_code == 409


def test_submit_requires_login(session, catalog):
    from app.main import app

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, follow_redirects=False) as anon:
        assert anon.post("/draw/submit", files={"image": ("d.png", png_bytes(), "image/png")}).status_code == 303
    app.dependency_overrides.clear()


# --- rate limit (scope 12) --------------------------------------------------

def test_submissions_are_rate_limited(client, session, player):
    client.get("/draw")
    assert post_drawing(client).status_code == 200
    client.post("/draw/start", data={"timer": ""})
    second = post_drawing(client)
    assert second.status_code == 429
    assert "too quickly" in second.json()["error"]


def test_the_rate_limit_does_not_block_a_slower_second_drawing(client, session, player):
    client.get("/draw")
    assert post_drawing(client).status_code == 200
    submit_limiter.clear()  # stand in for waiting out the window
    client.post("/draw/start", data={"timer": ""})
    assert post_drawing(client).status_code == 200
    assert session.scalar(select(func.count()).select_from(Submission)) == 2


# --- approval status (scope 9) ----------------------------------------------

def test_auto_approve_is_the_default(client, session, player):
    client.get("/draw")
    post_drawing(client)
    submission = session.scalar(select(Submission))
    assert SubmissionStatus(submission.status) is SubmissionStatus.APPROVED
    assert submission.approved_at is not None


def test_with_approval_on_submissions_are_pending(client, session, player):
    get_or_create_settings(session).require_approval = True
    session.commit()
    client.get("/draw")
    post_drawing(client)
    submission = session.scalar(select(Submission))
    assert SubmissionStatus(submission.status) is SubmissionStatus.PENDING
    assert submission.approved_at is None


def test_auto_approve_freezes_immediately(client, session, player):
    """Auto-approve *is* first approval, so it must freeze (scope 9, 10.2).

    Replaces an earlier test that asserted freezing was deferred. That deferral
    was a bug: an approved row with no uniqueId is invisible to the export, which
    is a pure read of frozen columns, so every drawing submitted with approval
    off would have silently never reached a pack.
    """
    client.get("/draw")
    post_drawing(client)
    submission = session.scalar(select(Submission))
    assert SubmissionStatus(submission.status) is SubmissionStatus.APPROVED
    assert submission.unique_id is not None
    assert submission.options_json is not None and len(submission.options_json) == 4
    assert submission.correct_letter in "ABCD"
    assert submission.credit_name == player.display_name


def test_pending_submissions_are_not_frozen(client, session, player):
    """Nothing is frozen until approval actually happens."""
    get_or_create_settings(session).require_approval = True
    session.commit()
    client.get("/draw")
    post_drawing(client)
    submission = session.scalar(select(Submission))
    assert submission.unique_id is None
    assert submission.options_json is None
    assert submission.credit_name is None


# --- timeout (scope 7.3 / 7.4) ----------------------------------------------

def test_empty_timeout_records_a_skip_and_creates_nothing(client, session, player):
    client.get("/draw")
    response = client.post("/draw/timeout")
    assert response.status_code == 200

    resolved = session.scalar(select(DrawSession).where(DrawSession.resolved_at.is_not(None)))
    assert SessionOutcome(resolved.outcome) is SessionOutcome.TIMED_OUT_EMPTY
    assert session.scalar(select(func.count()).select_from(Submission)) == 0


def test_timeout_immediately_assigns_a_new_pokemon(client, session, player):
    client.get("/draw")
    client.post("/draw/timeout")
    assert current_session(session, player) is not None


# --- confirmation page ------------------------------------------------------

def test_done_page_shows_the_drawing(client, session, player):
    client.get("/draw")
    redirect = post_drawing(client).json()["redirect"]
    page = client.get(redirect)
    assert page.status_code == 200
    assert "submitted" in page.text.lower()


def test_done_page_is_not_readable_by_another_player(client, session, player):
    client.get("/draw")
    redirect = post_drawing(client).json()["redirect"]

    create_user(session, username="sam", password="x" * 12)
    session.commit()
    client.post("/logout")
    client.post("/login", data={"username": "sam", "password": "x" * 12})
    assert client.get(redirect).status_code == 404


# --- serving (scope 8.2) ----------------------------------------------------

def test_served_image_has_the_contract_headers(client, session, player):
    client.get("/draw")
    post_drawing(client)
    submission = session.scalar(select(Submission))

    response = client.get(f"/images/{submission.file_path}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["access-control-allow-origin"] == "*"


def test_images_are_public_and_need_no_login(client, session, player):
    client.get("/draw")
    post_drawing(client)
    submission = session.scalar(select(Submission))
    client.post("/logout")
    # Steam clients fetch these with no session at all.
    assert client.get(f"/images/{submission.file_path}").status_code == 200


def test_missing_image_is_404(client):
    assert client.get("/images/bulbasaur-nobody-1.png").status_code == 404


def test_there_is_no_directory_listing(client):
    assert client.get("/images/").status_code == 404
    assert client.get("/images").status_code in (404, 307)


@pytest.mark.parametrize(
    "path",
    [
        "/images/../secret_key",
        "/images/..%2Fsecret_key",
        "/images/vulpix-alex-1.jpg",
        "/images/%2e%2e%2fsecret_key",
    ],
)
def test_traversal_and_non_png_requests_are_refused(client, path):
    assert client.get(path).status_code == 404
