"""Scope 3, 7.5 and 9 over HTTP, with production session lifetime."""

from __future__ import annotations

from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import config
from app.auth import get_db
from app.db import Base, make_engine
from app.models import Role, Submission, SubmissionStatus, get_or_create_settings
from app.ratelimit import login_limiter
from app.users import create_user
from tests.test_picker import add_submission, make_catalog


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "images")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "images").mkdir(parents=True, exist_ok=True)

    import app.models  # noqa: F401

    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    setup = factory()
    admin = create_user(setup, username="drake", password="x" * 12, role=Role.ADMIN)
    player = create_user(setup, username="alex", password="x" * 12, display_name="Alex")
    catalog = make_catalog(setup, 4)
    submission = add_submission(setup, catalog[0], player, status=SubmissionStatus.PENDING)
    setup.commit()
    ids = {"admin": admin.id, "player": player.id, "submission": submission.id}
    setup.close()

    def override_db():
        db = factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    from app.main import app

    app.dependency_overrides[get_db] = override_db
    login_limiter.clear()
    with TestClient(app, follow_redirects=False) as client:
        yield client, factory, ids
    app.dependency_overrides.clear()
    engine.dispose()


def login(client, username):
    return client.post("/login", data={"username": username, "password": "x" * 12})


def status_of(factory, submission_id):
    db = factory()
    try:
        return SubmissionStatus(db.get(Submission, submission_id).status)
    finally:
        db.close()


def unique_id_of(factory, submission_id):
    db = factory()
    try:
        return db.get(Submission, submission_id).unique_id
    finally:
        db.close()


# --- My Drawings (scope 7.5) ------------------------------------------------

def test_my_drawings_is_reachable(env):
    client, _, _ = env
    login(client, "alex")
    assert client.get("/drawings").status_code == 200


def test_my_drawings_offers_no_actions(env):
    """Scope 7.5: read-only. Delete is Admin-only (scope 3)."""
    client, _, _ = env
    login(client, "alex")
    body = client.get("/drawings").text
    assert "/admin/submissions" not in body
    assert "Delete" not in body


def test_my_drawings_requires_login(env):
    client, _, _ = env
    assert client.get("/drawings").status_code == 303


# --- permissions (scope 3) --------------------------------------------------

@pytest.mark.parametrize("action", ["approve", "reject", "delete", "unapprove", "unreject"])
def test_players_cannot_moderate_anything(env, action):
    client, factory, ids = env
    login(client, "alex")
    response = client.post(f"/admin/submissions/{ids['submission']}/{action}")
    assert response.status_code == 403
    assert status_of(factory, ids["submission"]) is SubmissionStatus.PENDING


def test_players_cannot_delete_even_their_own_drawing(env):
    """The loophole rejection exists to close must stay closed."""
    client, factory, ids = env
    login(client, "alex")
    client.post(f"/admin/submissions/{ids['submission']}/delete")
    assert status_of(factory, ids["submission"]) is not SubmissionStatus.DELETED


def test_players_cannot_open_the_queue(env):
    client, _, _ = env
    login(client, "alex")
    assert client.get("/admin/queue").status_code == 403


# --- admin queue ------------------------------------------------------------

def test_admin_sees_the_pending_queue(env):
    client, _, _ = env
    login(client, "drake")
    response = client.get("/admin/queue")
    assert response.status_code == 200
    assert "Mon 1" in response.text


def test_admin_approves(env):
    client, factory, ids = env
    login(client, "drake")
    response = client.post(f"/admin/submissions/{ids['submission']}/approve")
    assert response.status_code == 303
    assert status_of(factory, ids["submission"]) is SubmissionStatus.APPROVED
    assert unique_id_of(factory, ids["submission"]) == 1


def test_admin_rejects_then_unrejects(env):
    client, factory, ids = env
    login(client, "drake")
    client.post(f"/admin/submissions/{ids['submission']}/reject")
    assert status_of(factory, ids["submission"]) is SubmissionStatus.REJECTED
    client.post(f"/admin/submissions/{ids['submission']}/unreject")
    # Approval is off by default, so unreject goes straight to approved.
    assert status_of(factory, ids["submission"]) is SubmissionStatus.APPROVED


def test_admin_deletes(env):
    client, factory, ids = env
    login(client, "drake")
    client.post(f"/admin/submissions/{ids['submission']}/delete")
    assert status_of(factory, ids["submission"]) is SubmissionStatus.DELETED


def test_unapprove_then_reapprove_mints_a_new_id_over_http(env):
    client, factory, ids = env
    login(client, "drake")
    client.post(f"/admin/submissions/{ids['submission']}/approve")
    first = unique_id_of(factory, ids["submission"])
    client.post(f"/admin/submissions/{ids['submission']}/unapprove")
    assert unique_id_of(factory, ids["submission"]) is None
    client.post(f"/admin/submissions/{ids['submission']}/approve")
    assert unique_id_of(factory, ids["submission"]) > first


def test_the_retirement_is_explained_to_the_admin(env):
    client, factory, ids = env
    login(client, "drake")
    client.post(f"/admin/submissions/{ids['submission']}/approve")
    response = client.post(f"/admin/submissions/{ids['submission']}/unapprove")
    notice = unquote(response.headers["location"])
    assert "retired permanently" in notice
    assert "re-approving mints a new question" in notice


def test_an_unknown_action_is_404(env):
    client, _, ids = env
    login(client, "drake")
    assert client.post(f"/admin/submissions/{ids['submission']}/frobnicate").status_code == 404


def test_an_invalid_transition_reports_an_error(env):
    client, _, ids = env
    login(client, "drake")
    response = client.post(f"/admin/submissions/{ids['submission']}/unapprove")
    assert response.status_code == 400
    assert "only an approved" in response.text


def test_a_missing_submission_is_404(env):
    client, _, _ = env
    login(client, "drake")
    assert client.post("/admin/submissions/99999/approve").status_code == 404


# --- approval toggle (scope 9) ----------------------------------------------

def test_admin_can_turn_approval_on(env):
    client, factory, _ = env
    login(client, "drake")
    client.post("/admin/settings", data={"require_approval": "1", "public_base_url": ""})
    db = factory()
    try:
        assert get_or_create_settings(db).require_approval is True
    finally:
        db.close()


def test_omitting_the_checkbox_turns_approval_off(env):
    client, factory, _ = env
    login(client, "drake")
    client.post("/admin/settings", data={"require_approval": "1"})
    client.post("/admin/settings", data={"public_base_url": ""})
    db = factory()
    try:
        assert get_or_create_settings(db).require_approval is False
    finally:
        db.close()


def test_players_cannot_change_settings(env):
    client, _, _ = env
    login(client, "alex")
    assert client.post("/admin/settings", data={"require_approval": "1"}).status_code == 403
