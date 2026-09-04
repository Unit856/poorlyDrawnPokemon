"""Scope 8: the filename contract, PNG handling and serving rules."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app import config
from app.images import (
    FILENAME_RE,
    ImageRejected,
    build_filename,
    next_index,
    normalise_png,
    resolve_public_url,
    store_drawing,
    write_once,
)
from app.models import Submission, SubmissionStatus
from app.users import create_user
from tests.test_picker import add_submission, make_catalog


def png_bytes(size=None, colour=(20, 30, 40, 255), mode="RGBA"):
    size = size or (config.CANVAS_SIZE, config.CANVAS_SIZE)
    image = Image.new(mode, size, colour)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def blank_png():
    return png_bytes(colour=(0, 0, 0, 0))


@pytest.fixture(autouse=True)
def isolated_images(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "images")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "images").mkdir(parents=True, exist_ok=True)
    yield


@pytest.fixture()
def user(session):
    u = create_user(session, username="alex", password="x" * 12)
    session.flush()
    return u


# --- filename contract ------------------------------------------------------

def test_filename_follows_the_scope_contract():
    assert build_filename("vulpix-alolan", "alex", 2) == "vulpix-alolan-alex-2.png"


def test_filename_always_ends_in_png():
    # Trivia Tricks has validated this (scope 8.2).
    assert build_filename("bulbasaur", "sam", 1).endswith(".png")


def test_index_must_be_positive():
    with pytest.raises(ValueError):
        build_filename("bulbasaur", "sam", 0)


@pytest.mark.parametrize(
    "name, ok",
    [
        ("vulpix-alolan-alex-2.png", True),
        ("nidoran-f-sam-1.png", True),
        ("../../etc/passwd", False),
        ("..%2Fsecret.png", False),
        ("Vulpix-Alex-1.png", False),   # uppercase never appears in our slugs
        ("vulpix-alex-1.jpg", False),
        ("vulpix--alex-1.png", False),  # empty slug segment
        ("", False),
    ],
)
def test_filename_regex_guards_the_static_route(name, ok):
    assert bool(FILENAME_RE.match(name)) is ok


def test_next_index_starts_at_one(session, user):
    rows = make_catalog(session, 1)
    assert next_index(session, rows[0], user) == 1


def test_second_drawing_by_the_same_artist_gets_index_two(session, user):
    """Acceptance criterion 3: not an overwrite."""
    rows = make_catalog(session, 1)
    add_submission(session, rows[0], user, index=1)
    assert next_index(session, rows[0], user) == 2


def test_index_is_never_reused_after_a_delete(session, user):
    rows = make_catalog(session, 1)
    sub = add_submission(session, rows[0], user, index=1)
    sub.status = SubmissionStatus.DELETED
    session.flush()
    # Scope 8.1: never reused even if an earlier drawing is deleted.
    assert next_index(session, rows[0], user) == 2


def test_indexes_are_per_pair_not_global(session, user):
    rows = make_catalog(session, 2)
    add_submission(session, rows[0], user, index=1)
    assert next_index(session, rows[1], user) == 1


def test_public_url_is_built_from_the_configured_base():
    assert (
        resolve_public_url("https://pokedraw.example.com/", "vulpix-alolan-alex-2.png")
        == "https://pokedraw.example.com/images/vulpix-alolan-alex-2.png"
    )


# --- PNG validation ---------------------------------------------------------

def test_a_valid_canvas_png_is_accepted():
    data, blank = normalise_png(png_bytes())
    assert data.startswith(b"\x89PNG")
    assert blank is False


def test_a_fully_transparent_png_is_reported_blank():
    _, blank = normalise_png(blank_png())
    assert blank is True


def test_wrong_dimensions_are_rejected():
    with pytest.raises(ImageRejected, match="expected 800x800"):
        normalise_png(png_bytes(size=(512, 512)))


def test_non_png_images_are_rejected():
    image = Image.new("RGB", (config.CANVAS_SIZE, config.CANVAS_SIZE), (255, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    with pytest.raises(ImageRejected, match="expected a PNG"):
        normalise_png(buffer.getvalue())


def test_garbage_bytes_are_rejected():
    with pytest.raises(ImageRejected, match="not a readable image"):
        normalise_png(b"this is not an image at all")


def test_empty_upload_is_rejected():
    with pytest.raises(ImageRejected, match="empty upload"):
        normalise_png(b"")


def test_oversized_upload_is_rejected():
    with pytest.raises(ImageRejected, match="too large"):
        normalise_png(b"\x89PNG" + b"0" * (config.MAX_UPLOAD_BYTES + 1))


def test_output_is_reencoded_not_passed_through():
    """We serve bytes we produced, not bytes a client handed us."""
    original = png_bytes()
    tampered = original + b"TRAILING JUNK APPENDED BY A CLIENT"
    cleaned, _ = normalise_png(tampered)
    assert b"TRAILING JUNK" not in cleaned


def test_transparency_survives_the_round_trip():
    image = Image.new("RGBA", (config.CANVAS_SIZE, config.CANVAS_SIZE), (0, 0, 0, 0))
    image.putpixel((400, 400), (255, 0, 0, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    cleaned, blank = normalise_png(buffer.getvalue())
    assert blank is False
    restored = Image.open(io.BytesIO(cleaned))
    assert restored.mode == "RGBA"
    assert restored.getpixel((0, 0))[3] == 0, "background must stay transparent"


# --- writing ----------------------------------------------------------------

def test_write_once_creates_the_file():
    path = write_once("bulbasaur-alex-1.png", png_bytes())
    assert path.is_file()


def test_write_once_refuses_to_overwrite():
    write_once("bulbasaur-alex-1.png", png_bytes())
    with pytest.raises(ImageRejected, match="never overwritten"):
        write_once("bulbasaur-alex-1.png", png_bytes())


def test_write_once_refuses_a_suspicious_filename():
    with pytest.raises(ImageRejected, match="unexpected filename"):
        write_once("../escape.png", png_bytes())


def test_no_partial_file_is_left_behind():
    write_once("bulbasaur-alex-1.png", png_bytes())
    leftovers = list(config.IMAGES_DIR.glob("*.part"))
    assert leftovers == []


def test_store_drawing_writes_and_allocates(session, user):
    rows = make_catalog(session, 1)
    stored = store_drawing(session, rows[0], user, png_bytes())
    assert stored.index == 1
    assert stored.filename == f"{rows[0].slug}-alex-1.png"
    assert stored.public_path == f"/images/{rows[0].slug}-alex-1.png"
    assert stored.path.is_file()


def test_store_drawing_refuses_a_blank_canvas(session, user):
    rows = make_catalog(session, 1)
    with pytest.raises(ImageRejected, match="empty"):
        store_drawing(session, rows[0], user, blank_png())
