"""Scope 12 auth, and the scope 3 permission split."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import COOKIE_NAME, get_db
from app.models import Role, User
from app.ratelimit import login_limiter
from app.users import create_user


@pytest.fixture()
def client(session):
    from app.main import app

    def override_db():
        # Mirrors the real get_db, including the commit-on-success. Without it,
        # writes made by a handler stay pending and a later refresh() silently
        # discards them, which would make these tests lie.
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    app.dependency_overrides[get_db] = override_db
    login_limiter.clear()
    with TestClient(app, follow_redirects=False) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def admin(session):
    user = create_user(session, username="drake", password="correct-horse", role=Role.ADMIN)
    session.commit()
    return user


@pytest.fixture()
def player(session):
    user = create_user(session, username="alex", password="battery-staple", role=Role.PLAYER)
    session.commit()
    return user


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password})


# --- session basics ---------------------------------------------------------

def test_login_sets_an_httponly_session_cookie(client, admin):
    response = login(client, "drake", "correct-horse")
    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    assert client.cookies.get(COOKIE_NAME)


def test_bad_password_is_rejected(client, admin):
    response = login(client, "drake", "wrong")
    assert response.status_code == 401
    assert COOKIE_NAME not in response.cookies


def test_unknown_user_gives_the_same_message_as_a_bad_password(client, admin):
    unknown = login(client, "nobody", "whatever")
    bad_password = login(client, "drake", "wrong")
    # A login form should not confirm which usernames exist.
    assert unknown.status_code == bad_password.status_code == 401
    assert "Incorrect username or password." in unknown.text
    assert "Incorrect username or password." in bad_password.text


def test_logout_clears_the_cookie(client, admin):
    login(client, "drake", "correct-horse")
    response = client.post("/logout")
    assert response.status_code == 303
    assert 'wtp_session=""' in response.headers["set-cookie"] or "Max-Age=0" in response.headers["set-cookie"]


def test_a_tampered_cookie_is_not_accepted(client, admin):
    login(client, "drake", "correct-horse")
    client.cookies.set(COOKIE_NAME, "forged.session.value")
    response = client.get("/")
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


# --- access control ---------------------------------------------------------

def test_anonymous_lobby_redirects_to_login_preserving_destination(client):
    response = client.get("/profile")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/profile"


def test_player_cannot_reach_admin_pages(client, player):
    login(client, "alex", "battery-staple")
    response = client.get("/admin/users")
    # 403, not a redirect: they are authenticated, just not permitted (scope 3).
    assert response.status_code == 403


def test_admin_can_reach_admin_pages(client, admin):
    login(client, "drake", "correct-horse")
    assert client.get("/admin/users").status_code == 200


def test_anonymous_admin_page_redirects_rather_than_403(client):
    response = client.get("/admin/users")
    assert response.status_code == 303


def test_open_redirect_is_refused(client, admin):
    response = client.post(
        "/login",
        data={"username": "drake", "password": "correct-horse", "next": "//evil.example.com"},
    )
    assert response.headers["location"] == "/"


# --- rate limiting ----------------------------------------------------------

def test_login_is_rate_limited(client, admin):
    for _ in range(10):
        login(client, "drake", "wrong")
    response = login(client, "drake", "correct-horse")
    assert response.status_code == 429
    assert "Too many attempts" in response.text


def test_successful_login_clears_the_penalty(client, admin):
    for _ in range(3):
        login(client, "drake", "wrong")
    assert login(client, "drake", "correct-horse").status_code == 303
    for _ in range(9):
        login(client, "drake", "wrong")
    # Budget was reset by the success, so we are still under the limit.
    assert login(client, "drake", "correct-horse").status_code == 303


# --- no public registration -------------------------------------------------

def test_there_is_no_signup_route(client):
    for path in ("/signup", "/register"):
        assert client.get(path).status_code == 404


def test_login_page_explains_bootstrap_when_no_users_exist(client):
    assert "create-admin" in client.get("/login").text


# --- admin user management --------------------------------------------------

def test_admin_creates_a_player(client, session, admin):
    login(client, "drake", "correct-horse")
    response = client.post(
        "/admin/users/create",
        data={"username": "sam", "password": "long-enough-pw", "role": "player"},
    )
    assert response.status_code == 200
    created = session.scalar(select(User).where(User.username == "sam"))
    assert created is not None and created.slug == "sam"


def test_creating_a_colliding_slug_is_refused_in_the_ui(client, session, admin):
    login(client, "drake", "correct-horse")
    client.post("/admin/users/create", data={"username": "Sam", "password": "long-enough-pw"})
    response = client.post(
        "/admin/users/create", data={"username": "SAM", "password": "long-enough-pw"}
    )
    assert response.status_code == 400
    assert "never auto-suffixed" in response.text


def test_short_passwords_are_refused(client, admin):
    login(client, "drake", "correct-horse")
    response = client.post("/admin/users/create", data={"username": "sam", "password": "short"})
    assert response.status_code == 400


def test_admin_resets_a_password_and_the_old_one_stops_working(client, session, admin, player):
    login(client, "drake", "correct-horse")
    response = client.post(f"/admin/users/{player.id}/password", data={"password": "new-password"})
    assert response.status_code == 200
    client.post("/logout")

    assert login(client, "alex", "battery-staple").status_code == 401
    assert login(client, "alex", "new-password").status_code == 303


def test_blank_reset_generates_and_shows_a_temporary_password(client, admin, player):
    login(client, "drake", "correct-horse")
    response = client.post(f"/admin/users/{player.id}/password", data={"password": ""})
    assert "Temporary password:" in response.text


def test_player_cannot_reset_another_users_password(client, admin, player):
    login(client, "alex", "battery-staple")
    assert client.post(f"/admin/users/{admin.id}/password", data={"password": "x" * 12}).status_code == 403


def test_admin_cannot_demote_themselves(client, admin):
    login(client, "drake", "correct-horse")
    response = client.post(f"/admin/users/{admin.id}/role", data={"role": "player"})
    assert response.status_code == 400
    assert "your own Admin role" in response.text


def test_last_admin_cannot_be_demoted(client, session, admin):
    second = create_user(session, username="second", password="x" * 12, role=Role.ADMIN)
    session.commit()
    login(client, "drake", "correct-horse")
    # Demoting the *other* admin is fine while one remains.
    assert client.post(f"/admin/users/{second.id}/role", data={"role": "player"}).status_code == 200


# --- profile / credit -------------------------------------------------------

def test_player_sets_their_display_name(client, session, player):
    login(client, "alex", "battery-staple")
    response = client.post("/profile", data={"display_name": "Alexandra"})
    assert response.status_code == 303
    session.refresh(player)
    assert player.display_name == "Alexandra"


def test_changing_display_name_never_changes_the_artist_slug(client, session, player):
    login(client, "alex", "battery-staple")
    client.post("/profile", data={"display_name": "Alexandra"})
    session.refresh(player)
    # The slug is in every filename this account has already written.
    assert player.slug == "alex"


def test_blank_display_name_is_refused(client, player):
    login(client, "alex", "battery-staple")
    response = client.post("/profile", data={"display_name": "   "})
    assert "cannot be empty" in response.text


# --- Secure cookie flag (scope 12) ------------------------------------------

def test_empty_secure_cookies_env_means_unset_not_off(monkeypatch):
    """Regression: compose writes the var as an empty string.

    Treating "set but empty" as an explicit false stripped Secure from every
    cookie served over HTTPS behind the reverse proxy.
    """
    from starlette.datastructures import Headers
    from app.auth import cookie_should_be_secure

    class FakeRequest:
        def __init__(self, proto):
            self.headers = Headers({"x-forwarded-proto": proto})
            self.url = type("U", (), {"scheme": "http"})()

    monkeypatch.setenv("WTP_SECURE_COOKIES", "")
    assert cookie_should_be_secure(FakeRequest("https")) is True
    assert cookie_should_be_secure(FakeRequest("http")) is False


def test_explicit_override_still_wins(monkeypatch):
    from starlette.datastructures import Headers
    from app.auth import cookie_should_be_secure

    class FakeRequest:
        headers = Headers({"x-forwarded-proto": "http"})
        url = type("U", (), {"scheme": "http"})()

    monkeypatch.setenv("WTP_SECURE_COOKIES", "1")
    assert cookie_should_be_secure(FakeRequest()) is True
    monkeypatch.setenv("WTP_SECURE_COOKIES", "0")
    assert cookie_should_be_secure(FakeRequest()) is False
