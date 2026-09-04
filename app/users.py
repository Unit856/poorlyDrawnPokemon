"""User creation, scope 3 and 8.1.

Account creation is the only place artist_slug is ever decided. Uniqueness is
enforced here and never auto-resolved: the slug goes into permanent filenames,
so a collision must be settled by a human choosing a different username.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Role, User
from app.security import hash_password
from app.slugs import artist_slug


class SlugTaken(ValueError):
    pass


class UsernameTaken(ValueError):
    pass


def create_user(
    session: Session,
    *,
    username: str,
    password: str,
    display_name: str | None = None,
    role: Role = Role.PLAYER,
) -> User:
    username = username.strip()
    if not username:
        raise ValueError("username must not be empty")

    if session.scalar(select(User).where(User.username == username)):
        raise UsernameTaken(f"username {username!r} is already taken")

    slug = artist_slug(username)
    clash = session.scalar(select(User).where(User.slug == slug))
    if clash is not None:
        raise SlugTaken(
            f"username {username!r} slugifies to {slug!r}, already used by {clash.username!r}. "
            "Choose a different username; artist slugs are never auto-suffixed."
        )

    user = User(
        username=username,
        password_hash=hash_password(password),
        display_name=(display_name or username).strip(),
        slug=slug,
        role=role,
    )
    session.add(user)
    session.flush()
    return user


def set_password(session: Session, user: User, new_password: str) -> None:
    """Admin-driven reset (scope 12). No tokens, no email flow."""
    user.password_hash = hash_password(new_password)
    session.add(user)
