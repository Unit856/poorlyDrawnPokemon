"""Serving drawings, scope 8.2.

Deliberately a hand-written route rather than a StaticFiles mount, because the
headers are part of the contract: Trivia Tricks hotlinks these URLs from Steam
clients, and the files are immutable for a year.

Public and unauthenticated by design -- the whole point is that anyone's game
client can fetch them.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.responses import FileResponse

from app import config
from app.images import FILENAME_RE

router = APIRouter()

CACHE_CONTROL = "public, max-age=31536000, immutable"


def _headers() -> dict[str, str]:
    return {
        "Cache-Control": CACHE_CONTROL,
        # Belt-and-braces: the Trivia Tricks client is not a browser and is not
        # expected to preflight, but this keeps in-browser previews working.
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD",
        "X-Content-Type-Options": "nosniff",
    }


@router.get("/images/{filename}")
def serve_image(filename: str) -> Response:
    # No directory listing, no traversal, and nothing served that this app did
    # not write: the name must match the slug triple exactly.
    if not FILENAME_RE.match(filename):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    path = (config.IMAGES_DIR / filename).resolve()
    try:
        path.relative_to(config.IMAGES_DIR.resolve())
    except ValueError:  # pragma: no cover - FILENAME_RE already prevents this
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    return FileResponse(path, media_type="image/png", headers=_headers())


@router.get("/images")
@router.get("/images/")
def no_directory_listing() -> Response:
    """Scope 8.2: no directory listing."""
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
