"""Shared Jinja environment."""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def render(request: Request, template: str, /, **context):
    """Render with the signed-in user always available to the layout."""
    context.setdefault("user", getattr(request.state, "user", None))
    return templates.TemplateResponse(request, template, context)
