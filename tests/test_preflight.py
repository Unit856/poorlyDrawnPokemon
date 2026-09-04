"""Scope 8.3 / 13: deployment self-check."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app import config, preflight
from app.models import SubmissionStatus, get_or_create_settings
from app.preflight import FAIL, OK, WARN, Preflight, check_base_url, check_live_fetch
from app.users import create_user
from tests.test_picker import add_submission, make_catalog


def status_of(report: Preflight, name: str) -> str:
    return next(c.status for c in report.checks if c.name == name)


def names(report: Preflight) -> set[str]:
    return {c.name for c in report.checks}


# --- base URL sanity --------------------------------------------------------

def test_missing_base_url_fails():
    report = Preflight()
    assert check_base_url(report, "") is False
    assert status_of(report, "public base URL") == FAIL


@pytest.mark.parametrize(
    "base",
    [
        "http://localhost:8000",
        "https://127.0.0.1",
        "https://192.168.1.50",
        "http://10.0.0.4:8000",
        "https://pokedraw.local",
    ],
)
def test_lan_only_hosting_fails_loudly(base):
    """The quiet killer: previews fine locally, dead for every remote friend."""
    report = Preflight()
    check_base_url(report, base)
    assert status_of(report, "reachability") == FAIL


def test_a_public_https_hostname_passes():
    report = Preflight()
    check_base_url(report, "https://pokedraw.example.com")
    assert status_of(report, "scheme") == OK
    assert status_of(report, "hostname") == OK
    assert "reachability" not in names(report)


def test_plain_http_warns():
    report = Preflight()
    check_base_url(report, "http://pokedraw.example.com")
    assert status_of(report, "scheme") == WARN


def test_a_raw_public_ip_warns():
    report = Preflight()
    # A genuinely routable address. Note that RFC 5737 documentation ranges
    # (198.51.100.0/24 and friends) are classed as private and fail the
    # reachability check instead — which is correct, they are not routable.
    check_base_url(report, "https://8.8.8.8")
    # Players see this hostname before a match, and an IP cannot move.
    assert status_of(report, "hostname") == WARN


def test_documentation_ip_ranges_are_treated_as_unreachable():
    report = Preflight()
    check_base_url(report, "https://198.51.100.7")
    assert status_of(report, "reachability") == FAIL


def test_a_path_component_warns():
    report = Preflight()
    check_base_url(report, "https://example.com/secret-drawings")
    assert status_of(report, "path") == WARN


# --- live fetch -------------------------------------------------------------

def good_png_response(monkeypatch, **overrides):
    image = Image.new("RGBA", (config.CANVAS_SIZE, config.CANVAS_SIZE), (0, 0, 0, 0))
    image.putpixel((10, 10), (255, 0, 0, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    payload = {
        "status_code": 200,
        "headers": {
            "content-type": "image/png",
            "cache-control": "public, max-age=31536000, immutable",
            "access-control-allow-origin": "*",
        },
        "content": buffer.getvalue(),
    }
    payload.update(overrides)

    class FakeResponse:
        status_code = payload["status_code"]
        headers = payload["headers"]
        content = payload["content"]

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return FakeResponse()

    monkeypatch.setattr(preflight.httpx, "Client", FakeClient)


def test_a_healthy_image_passes_every_check(monkeypatch):
    good_png_response(monkeypatch)
    report = Preflight()
    check_live_fetch(report, "https://example.com/images/bulbasaur-alex-1.png")
    assert all(c.status == OK for c in report.checks), report.summary()


def test_a_non_png_url_fails(monkeypatch):
    good_png_response(monkeypatch)
    report = Preflight()
    check_live_fetch(report, "https://example.com/images/bulbasaur-alex-1.jpg")
    assert status_of(report, "url suffix") == FAIL


def test_a_wrong_content_type_fails(monkeypatch):
    good_png_response(monkeypatch, headers={"content-type": "text/html"})
    report = Preflight()
    check_live_fetch(report, "https://example.com/images/x-y-1.png")
    assert status_of(report, "content-type") == FAIL


def test_a_proxy_stripping_cache_headers_warns(monkeypatch):
    good_png_response(
        monkeypatch,
        headers={"content-type": "image/png", "cache-control": "no-store"},
    )
    report = Preflight()
    check_live_fetch(report, "https://example.com/images/x-y-1.png")
    assert status_of(report, "cache-control") == WARN
    assert status_of(report, "CORS") == WARN


def test_a_login_page_served_as_200_is_caught(monkeypatch):
    """A captive proxy returning HTML with a 200 must not look like success."""
    good_png_response(monkeypatch, content=b"<!doctype html><title>Log in</title>")
    report = Preflight()
    check_live_fetch(report, "https://example.com/images/x-y-1.png")
    assert status_of(report, "image body") == FAIL


def test_a_404_fails(monkeypatch):
    good_png_response(monkeypatch, status_code=404)
    report = Preflight()
    check_live_fetch(report, "https://example.com/images/x-y-1.png")
    assert status_of(report, "image fetch") == FAIL


def test_a_network_error_fails(monkeypatch):
    class Exploding:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            raise OSError("connection refused")

    monkeypatch.setattr(preflight.httpx, "Client", Exploding)
    report = Preflight()
    check_live_fetch(report, "https://example.com/images/x-y-1.png")
    assert status_of(report, "image fetch") == FAIL


# --- end to end -------------------------------------------------------------

def test_run_offline_reports_without_touching_the_network(session):
    get_or_create_settings(session).public_base_url = "https://pokedraw.example.com"
    session.flush()
    report = preflight.run(session, skip_network=True)
    assert status_of(report, "network checks") == WARN
    assert not report.failed


def test_run_warns_when_nothing_is_approved_yet(session):
    get_or_create_settings(session).public_base_url = "https://pokedraw.example.com"
    session.flush()
    report = preflight.run(session, skip_network=True)
    assert status_of(report, "approved drawings") == WARN


def test_run_stops_early_without_a_base_url(session):
    report = preflight.run(session, skip_network=True)
    assert report.failed
    assert names(report) == {"public base URL"}


def test_sample_url_prefers_a_real_approved_row(session):
    user = create_user(session, username="alex", password="x" * 12)
    catalog = make_catalog(session, 4)
    submission = add_submission(session, catalog[0], user, status=SubmissionStatus.APPROVED)
    submission.public_url = "https://pokedraw.example.com/images/mon1-alex-1.png"
    session.flush()

    url = preflight.sample_image_url(session, "https://pokedraw.example.com")
    assert url == submission.public_url


def test_summary_reports_a_verdict(session):
    report = preflight.run(session, skip_network=True)
    assert "preflight FAILED" in report.summary()
