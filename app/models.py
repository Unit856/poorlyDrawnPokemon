"""Logical data model, scope 11.

Two invariants are load-bearing across the whole app and are worth stating once
here:

* Anything written at first approval (unique_id, options_json, correct_letter,
  credit_name, public_url) is written exactly once and never updated. Export is
  a pure read of those columns (scope 10.2).
* Slugs and per-pair indexes are permanent. They appear in filenames that are
  served immutable and never overwritten (scope 8.2).
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    PLAYER = "player"
    ADMIN = "admin"


class SubmissionStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    DELETED = "deleted"


class SessionOutcome(str, enum.Enum):
    SUBMITTED = "submitted"
    SKIPPED = "skipped"
    TIMED_OUT_EMPTY = "timed_out_empty"


#: Statuses that count toward drawing_count in the weighted picker (scope 6).
#: Deleted is the only status that releases an answer unit back to the picker;
#: rejected deliberately still counts, so rejection is not a re-roll loophole.
COUNTING_STATUSES: frozenset[SubmissionStatus] = frozenset(
    {SubmissionStatus.PENDING, SubmissionStatus.APPROVED, SubmissionStatus.REJECTED}
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Frozen at creation. Never follows a later display_name edit, because it is
    # already baked into every PNG this user has written (scope 8.1).
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    role: Mapped[Role] = mapped_column(String(16), default=Role.PLAYER, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    submissions: Mapped[list["Submission"]] = relationship(back_populates="user")

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN or self.role == Role.ADMIN.value


class Pokemon(Base):
    __tablename__ = "pokemon"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    national_dex: Mapped[int] = mapped_column(Integer, nullable=False)

    # Upstream variety name, e.g. "vulpix" or "vulpix-alola". Stable identity for
    # re-seed upserts (scope 5.5) and distinct from region_key by design.
    form_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    region_key: Mapped[str | None] = mapped_column(String(16), nullable=True)
    breed_key: Mapped[str | None] = mapped_column(String(16), nullable=True)

    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    types: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    species_category: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    dex_entries: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    # Admin control. Disabled rows are skipped by the picker and never chosen as
    # distractors, but frozen questions that already reference them keep working.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    submissions: Mapped[list["Submission"]] = relationship(back_populates="pokemon")

    __table_args__ = (Index("ix_pokemon_enabled", "enabled"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Pokemon {self.slug}>"


class Submission(Base):
    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    pokemon_id: Mapped[int] = mapped_column(ForeignKey("pokemon.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    # Unique per (pokemon, artist) and never reused, even after a delete.
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    file_path: Mapped[str] = mapped_column(String(255), nullable=False)

    status: Mapped[SubmissionStatus] = mapped_column(
        String(16), default=SubmissionStatus.PENDING, nullable=False
    )

    # --- frozen at first approval, never updated afterwards (scope 10.2) ------
    unique_id: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)
    public_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    options_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    correct_letter: Mapped[str | None] = mapped_column(String(1), nullable=True)
    credit_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # -------------------------------------------------------------------------

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    pokemon: Mapped[Pokemon] = relationship(back_populates="submissions")
    user: Mapped[User] = relationship(back_populates="submissions")

    __table_args__ = (
        UniqueConstraint("pokemon_id", "user_id", "index", name="uq_submission_pair_index"),
        CheckConstraint(
            "correct_letter IS NULL OR correct_letter IN ('A','B','C','D')",
            name="ck_submission_correct_letter",
        ),
        Index("ix_submission_status", "status"),
        Index("ix_submission_pokemon_status", "pokemon_id", "status"),
    )

    @property
    def counts_toward_drawing_count(self) -> bool:
        return SubmissionStatus(self.status) in COUNTING_STATUSES

    @property
    def is_frozen(self) -> bool:
        return self.unique_id is not None


class DrawSession(Base):
    __tablename__ = "draw_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    pokemon_id: Mapped[int] = mapped_column(ForeignKey("pokemon.id"), nullable=False)
    timer_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[SessionOutcome | None] = mapped_column(String(20), nullable=True)

    user: Mapped[User] = relationship()
    pokemon: Mapped[Pokemon] = relationship()

    __table_args__ = (
        Index("ix_draw_session_open", "user_id", "resolved_at"),
        # At most one *open* session per player. Resume (scope 7.1) is only
        # well-defined if there is exactly one assignment to resume, so this is
        # enforced in the schema rather than trusted to the service layer.
        Index(
            "uq_open_draw_session",
            "user_id",
            unique=True,
            sqlite_where=text("resolved_at IS NULL"),
        ),
    )


class Settings(Base):
    """Single-row config table. Always id=1."""

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    public_base_url: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    require_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    default_timer_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    catalog_snapshot_label: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # Monotonic. Only ever increases; a retired uniqueId is simply one no live
    # row points at, which is how "never reused" is enforced (scope 11).
    next_unique_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    __table_args__ = (CheckConstraint("id = 1", name="ck_settings_singleton"),)


def allocate_unique_id(session) -> int:
    """Hand out the next uniqueId.

    Lives here rather than in a service module so both the approval path and the
    export backfill can call it without importing each other. Monotonic: it only
    ever increases, which is exactly how "never reused" is enforced (scope 11).
    """
    settings = get_or_create_settings(session)
    value = settings.next_unique_id
    settings.next_unique_id = value + 1
    session.add(settings)
    session.flush()
    return value


def get_or_create_settings(session) -> Settings:
    settings = session.get(Settings, 1)
    if settings is None:
        settings = Settings(id=1)
        session.add(settings)
        session.flush()
    return settings
