"""Deployment self-check, scope 8.3 and 13.

Trivia Tricks clients fetch imageURL themselves, from wherever the player is
sitting. That makes one failure mode both very likely and very quiet: the pack
previews perfectly in a browser on the same LAN as the server and then fails for
every remote friend (scope 4).

This module does the machine-checkable half of the acceptance criteria: it
fetches the app's *own* published URL over the network, exactly as a Steam client
would, and checks what comes back. It cannot play a Trivia Tricks match -- that
part stays a human step.
"""

from __future__ import annotations

import io
import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.models import Submission, SubmissionStatus, get_or_create_settings

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str

    @property
    def symbol(self) -> str:
        return {OK: "PASS", WARN: "WARN", FAIL: "FAIL"}[self.status]


@dataclass
class Preflight:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str) -> None:
        self.checks.append(Check(name, status, detail))

    @property
    def failed(self) -> bool:
        return any(c.status == FAIL for c in self.checks)

    @property
    def warned(self) -> bool:
        return any(c.status == WARN for c in self.checks)

    def summary(self) -> str:
        lines = [f"[{c.symbol}] {c.name}: {c.detail}" for c in self.checks]
        verdict = "FAILED" if self.failed else ("passed with warnings" if self.warned else "passed")
        lines.append(f"\npreflight {verdict}")
        return "\n".join(lines)


def _is_private_host(host: str) -> bool:
    """True for loopback, RFC1918 and .local -- the LAN-only trap."""
    if host in {"localhost", ""} or host.endswith(".local") or host.endswith(".lan"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.ip_address(socket.gethostbyname(host))
        except (OSError, ValueError):
            return False
    return address.is_private or address.is_loopback or address.is_link_local


def _is_raw_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def check_base_url(report: Preflight, base: str) -> bool:
    if not base:
        report.add(
            "public base URL",
            FAIL,
            "not set. Every imageURL in the pack would be unusable. Set it in Settings.",
        )
        return False

    parsed = urlparse(base)
    host = parsed.hostname or ""

    if parsed.scheme != "https":
        report.add(
            "scheme",
            WARN,
            f"{parsed.scheme}:// — prefer HTTPS (scope 8.3). Steam clients will fetch this in the clear.",
        )
    else:
        report.add("scheme", OK, "https")

    if _is_private_host(host):
        report.add(
            "reachability",
            FAIL,
            f"{host} is loopback or a private/LAN address. This will preview fine in a "
            "browser on your network and fail for every remote friend inside Trivia Tricks.",
        )
    elif _is_raw_ip(host):
        report.add(
            "hostname",
            WARN,
            f"{host} is a raw IP. Prefer a dedicated subdomain — it is shown to every "
            "player before a match and a bare IP cannot move (scope 8.3, 16).",
        )
    else:
        report.add("hostname", OK, f"{host}")

    if parsed.path.strip("/"):
        report.add(
            "path",
            WARN,
            f"base URL has a path component ({parsed.path!r}). Keep it boring and "
            "put no secrets in the path — players see this host.",
        )

    return True


def sample_image_url(db: Session, base: str) -> str | None:
    """An actual published URL to test with, preferring a real approved row."""
    submission = db.scalar(
        select(Submission)
        .where(
            Submission.status == SubmissionStatus.APPROVED,
            Submission.public_url.is_not(None),
        )
        .order_by(Submission.unique_id)
    )
    if submission is not None:
        return submission.public_url

    existing = sorted(config.IMAGES_DIR.glob("*.png"))
    if existing:
        return f"{base.rstrip('/')}/images/{existing[0].name}"
    return None


def check_live_fetch(report: Preflight, url: str, *, timeout: float = 15.0) -> None:
    """Fetch the published URL the way a Steam client would."""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url)
    except Exception as exc:
        report.add("image fetch", FAIL, f"could not fetch {url}: {exc}")
        return

    if response.status_code != 200:
        report.add("image fetch", FAIL, f"{url} returned HTTP {response.status_code}")
        return
    report.add("image fetch", OK, f"200 from {url}")

    if not url.lower().endswith(".png"):
        report.add("url suffix", FAIL, "URL must end in .png (Trivia Tricks validates this)")
    else:
        report.add("url suffix", OK, "ends in .png")

    content_type = response.headers.get("content-type", "")
    if content_type.split(";")[0].strip() != "image/png":
        report.add("content-type", FAIL, f"{content_type!r}, expected image/png")
    else:
        report.add("content-type", OK, "image/png")

    cache = response.headers.get("cache-control", "")
    if "immutable" in cache and "max-age" in cache:
        report.add("cache-control", OK, cache)
    else:
        report.add(
            "cache-control",
            WARN,
            f"{cache!r} — expected a long immutable cache. A proxy may be rewriting it.",
        )

    cors = response.headers.get("access-control-allow-origin", "")
    if cors == "*":
        report.add("CORS", OK, "*")
    else:
        report.add(
            "CORS",
            WARN,
            f"{cors!r} — belt-and-braces only; the Steam client is not a browser, "
            "but a proxy stripping headers is worth knowing about.",
        )

    try:
        image = Image.open(io.BytesIO(response.content))
        size = (config.CANVAS_SIZE, config.CANVAS_SIZE)
        if image.format != "PNG":
            report.add("image body", FAIL, f"decoded as {image.format}, not PNG")
        elif image.size != size:
            report.add("image body", WARN, f"{image.width}x{image.height}, expected {size[0]}x{size[1]}")
        else:
            report.add("image body", OK, f"valid {image.width}x{image.height} PNG")
    except (UnidentifiedImageError, OSError) as exc:
        report.add(
            "image body",
            FAIL,
            f"response is not a decodable image ({exc}). A login page or error "
            "page served with a 200 would look like this.",
        )


def check_health(report: Preflight, base: str, *, timeout: float = 15.0) -> None:
    url = f"{base.rstrip('/')}/healthz"
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url)
        if response.status_code == 200 and response.json().get("status") == "ok":
            report.add("app reachable", OK, f"{url} responded")
        else:
            report.add("app reachable", FAIL, f"{url} returned HTTP {response.status_code}")
    except Exception as exc:
        report.add("app reachable", FAIL, f"could not reach {url}: {exc}")


def run(db: Session, *, skip_network: bool = False) -> Preflight:
    report = Preflight()
    settings = get_or_create_settings(db)
    base = settings.public_base_url

    if not check_base_url(report, base):
        return report

    approved = db.scalar(
        select(Submission).where(Submission.status == SubmissionStatus.APPROVED)
    )
    if approved is None:
        report.add(
            "approved drawings",
            WARN,
            "none yet — approve at least one before exporting a pack.",
        )

    if skip_network:
        report.add("network checks", WARN, "skipped")
        return report

    check_health(report, base)

    url = sample_image_url(db, base)
    if url is None:
        report.add(
            "image fetch",
            WARN,
            "no drawings exist yet, so the hotlink path could not be tested. "
            "Re-run this after the first drawing is approved.",
        )
    else:
        check_live_fetch(report, url)

    return report
