"""Image writing and the filename contract, scope 8.

Two things here are permanent and therefore paranoid:

* A file is written once and never overwritten. It is served `immutable` with a
  one-year cache, so a rewrite would leave stale copies in Steam clients and
  proxies forever.
* An `index` is never reused for a (pokemon, artist) pair, even after a delete.
  The next drawing gets the next integer.

Uploaded bytes are decoded and re-encoded rather than trusted. The canvas is
client-side, the result is served publicly, and "the browser sent it" is not a
guarantee of anything.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config
from app.models import Pokemon, Submission, User

#: Only ever a slug triple. Guards the static route against traversal and
#: against serving anything this app did not write.
FILENAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.png$")


class ImageRejected(ValueError):
    """The uploaded bytes are not an acceptable drawing."""


@dataclass(frozen=True)
class StoredImage:
    filename: str
    path: Path
    index: int

    @property
    def public_path(self) -> str:
        return f"/images/{self.filename}"


def build_filename(pokemon_slug: str, artist_slug: str, index: int) -> str:
    """Scope 8.1: /images/{pokemon_slug}-{artist_slug}-{index}.png"""
    if index < 1:
        raise ValueError("index is a positive integer")
    return f"{pokemon_slug}-{artist_slug}-{index}.png"


def next_index(db: Session, pokemon: Pokemon, user: User) -> int:
    """The next unused index for this (pokemon, artist) pair.

    Counts *every* submission including deleted ones, because indexes are never
    reused -- a second drawing after a delete still gets 3, not 2 (scope 8.1).
    """
    highest = db.scalar(
        select(func.max(Submission.index)).where(
            Submission.pokemon_id == pokemon.id, Submission.user_id == user.id
        )
    )
    return (highest or 0) + 1


def normalise_png(data: bytes) -> tuple[bytes, bool]:
    """Validate and re-encode an uploaded drawing.

    Returns (png_bytes, is_blank). Blankness here means "no non-transparent
    pixels at all" -- a defensive check against publishing an empty image to a
    permanent URL, separate from the scope 7.3 stroke-count rule enforced in the
    browser.
    """
    if not data:
        raise ImageRejected("empty upload")
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise ImageRejected("image is too large")

    try:
        probe = Image.open(io.BytesIO(data))
        probe.verify()  # structural check; consumes the file object
        image = Image.open(io.BytesIO(data))
    except (UnidentifiedImageError, OSError) as exc:
        raise ImageRejected("not a readable image") from exc

    if image.format != "PNG":
        raise ImageRejected(f"expected a PNG, got {image.format}")

    size = (config.CANVAS_SIZE, config.CANVAS_SIZE)
    if image.size != size:
        raise ImageRejected(f"expected {size[0]}x{size[1]}, got {image.width}x{image.height}")

    image = image.convert("RGBA")
    is_blank = image.getbbox() is None

    out = io.BytesIO()
    # Re-encoded so what we serve is something we produced: no stray metadata,
    # no exotic chunks, dimensions guaranteed.
    image.save(out, format="PNG", optimize=True)
    return out.getvalue(), is_blank


def write_once(filename: str, data: bytes) -> Path:
    """Write to the images dir, refusing to clobber an existing file."""
    if not FILENAME_RE.match(filename):
        raise ImageRejected(f"refusing to write unexpected filename {filename!r}")

    config.ensure_dirs()
    target = config.IMAGES_DIR / filename
    if target.exists():
        raise ImageRejected(f"{filename} already exists; files are never overwritten")

    # Write to a temp file in the same directory, then atomically rename, so a
    # crash mid-write cannot leave a truncated PNG at a public URL.
    tmp = target.with_name(target.name + ".part")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def store_drawing(db: Session, pokemon: Pokemon, user: User, data: bytes) -> StoredImage:
    png, is_blank = normalise_png(data)
    if is_blank:
        raise ImageRejected("the canvas is empty")

    index = next_index(db, pokemon, user)
    filename = build_filename(pokemon.slug, user.slug, index)
    path = write_once(filename, png)
    return StoredImage(filename=filename, path=path, index=index)


def resolve_public_url(base_url: str, filename: str) -> str:
    """Absolute URL for the CSV, built from the configured public base (scope 8.1)."""
    return f"{base_url.rstrip('/')}/images/{filename}"
