"""Scope 5.5 re-seed semantics, and scope 3/8.1 account creation."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.catalog.build import CatalogRow
from app.catalog.seed import SeedReport, apply_catalog
from app.models import Pokemon, Role
from app.users import SlugTaken, UsernameTaken, create_user


def row(form_key="vulpix", slug="vulpix", name="Vulpix", **kw):
    return CatalogRow(
        form_key=form_key,
        display_name=name,
        slug=slug,
        national_dex=kw.pop("national_dex", 37),
        generation=kw.pop("generation", 1),
        types=kw.pop("types", ["Fire"]),
        species_category=kw.pop("species_category", "Fox Pokémon"),
        dex_entries=kw.pop("dex_entries", ["Kanto text."]),
        **kw,
    )


def test_first_seed_inserts(session):
    report = apply_catalog(session, [row()], SeedReport())
    assert report.inserted == 1
    assert session.scalar(select(Pokemon).where(Pokemon.slug == "vulpix")).enabled is True


def test_reseed_refreshes_hint_fields(session):
    apply_catalog(session, [row()], SeedReport())
    apply_catalog(session, [row(dex_entries=["Updated text."], types=["Fire", "Ice"])], SeedReport())
    pokemon = session.scalar(select(Pokemon).where(Pokemon.form_key == "vulpix"))
    assert pokemon.dex_entries == ["Updated text."]
    assert pokemon.types == ["Fire", "Ice"]


def test_reseed_never_rewrites_a_slug(session):
    apply_catalog(session, [row()], SeedReport())
    report = apply_catalog(session, [row(slug="vulpix-renamed")], SeedReport())
    pokemon = session.scalar(select(Pokemon).where(Pokemon.form_key == "vulpix"))
    # Files on disk already reference the old slug and are never overwritten.
    assert pokemon.slug == "vulpix"
    assert report.slug_conflicts and "vulpix-renamed" in report.slug_conflicts[0]


def test_rows_that_vanish_upstream_are_disabled_not_deleted(session):
    apply_catalog(session, [row(), row(form_key="ekans", slug="ekans", name="Ekans")], SeedReport())
    report = apply_catalog(session, [row()], SeedReport())
    ekans = session.scalar(select(Pokemon).where(Pokemon.form_key == "ekans"))
    assert ekans is not None, "rows are never deleted"
    assert ekans.enabled is False
    assert report.disabled_missing == 1


def test_reseed_does_not_resurrect_an_admin_disabled_row(session):
    apply_catalog(session, [row()], SeedReport())
    session.scalar(select(Pokemon).where(Pokemon.form_key == "vulpix")).enabled = False
    session.flush()
    apply_catalog(session, [row()], SeedReport())
    # `enabled` is admin state, not upstream state.
    assert session.scalar(select(Pokemon).where(Pokemon.form_key == "vulpix")).enabled is False


def test_snapshot_label_is_recorded(session):
    report = SeedReport()
    report.snapshot_label = "pokeapi.co @ 2026-09-03 - 1025 species, 1083 answer units"
    apply_catalog(session, [row()], report)
    from app.models import get_or_create_settings

    assert "1083 answer units" in get_or_create_settings(session).catalog_snapshot_label


# --- account creation -------------------------------------------------------

def test_create_user_freezes_slug(session):
    user = create_user(session, username="Alex", password="hunter2", role=Role.ADMIN)
    assert user.slug == "alex"
    assert user.is_admin


def test_duplicate_username_rejected(session):
    create_user(session, username="alex", password="x")
    with pytest.raises(UsernameTaken):
        create_user(session, username="alex", password="y")


def test_colliding_slug_rejected_at_creation_not_auto_suffixed(session):
    # Distinct usernames, same slug: case is the classic way in.
    create_user(session, username="Alex", password="x")
    with pytest.raises(SlugTaken) as exc:
        create_user(session, username="ALEX", password="y")
    assert "never auto-suffixed" in str(exc.value)


def test_punctuation_in_usernames_does_not_silently_collide(session):
    create_user(session, username="Alex", password="x")
    # "A.L.E.X" slugifies to a-l-e-x, which is genuinely a different artist_slug.
    user = create_user(session, username="A.L.E.X", password="y")
    assert user.slug == "a-l-e-x"


def test_display_name_defaults_to_username_and_is_the_credit(session):
    user = create_user(session, username="alex", password="x")
    assert user.display_name == "alex"
