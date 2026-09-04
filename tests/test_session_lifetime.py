"""Regression tests that use *production* session lifetime.

The other suites keep one session open for the whole test, which hides a class of
bug: error pages render after the request's session has been rolled back and
closed, so any ORM instance still held by a template is detached and expired.
That turned a 403 into a 500 in the container while the ordinary tests passed.

These tests close the session per request, exactly like `app.auth.get_db`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.auth import get_db
from app.db import Base, make_engine
from app.models import Role
from app.ratelimit import login_limiter
from app.users import create_user


@pytest.fixture()
def prod_client():
    import app.models  # noqa: F401

    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    setup = factory()
    create_user(setup, username="drake", password="correct-horse", role=Role.ADMIN)
    create_user(setup, username="alex", password="battery-staple", role=Role.PLAYER)
    setup.commit()
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
            db.close()  # the step that detaches ORM instances

    from app.main import app

    app.dependency_overrides[get_db] = override_db
    login_limiter.clear()
    with TestClient(app, follow_redirects=False) as client:
        yield client
    app.dependency_overrides.clear()
    engine.dispose()


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password})


def test_player_hitting_an_admin_page_gets_403_not_500(prod_client):
    login(prod_client, "alex", "battery-staple")
    response = prod_client.get("/admin/users")
    assert response.status_code == 403, response.text


def test_the_403_page_still_renders_the_nav_for_the_signed_in_user(prod_client):
    login(prod_client, "alex", "battery-staple")
    response = prod_client.get("/admin/users")
    # The layout must survive rendering after the session closed.
    assert "alex" in response.text
    assert "admin only" in response.text


def test_404_page_renders_for_a_signed_in_user(prod_client):
    login(prod_client, "alex", "battery-staple")
    response = prod_client.get("/no-such-page")
    assert response.status_code == 404
    assert "Back to the lobby" in response.text


def test_404_page_renders_for_an_anonymous_visitor(prod_client):
    response = prod_client.get("/no-such-page")
    assert response.status_code == 404


def test_ordinary_pages_still_work_with_per_request_sessions(prod_client):
    login(prod_client, "drake", "correct-horse")
    assert prod_client.get("/").status_code == 200
    assert prod_client.get("/profile").status_code == 200
    assert prod_client.get("/admin/users").status_code == 200


def test_profile_update_persists_across_requests(prod_client):
    login(prod_client, "alex", "battery-staple")
    prod_client.post("/profile", data={"display_name": "Alexandra"})
    assert "Alexandra" in prod_client.get("/profile").text


def test_json_clients_get_json_errors(prod_client):
    login(prod_client, "alex", "battery-staple")
    response = prod_client.get("/admin/users", headers={"accept": "application/json"})
    assert response.status_code == 403
    assert response.json()["detail"] == "admin only"
