"""Catalog seeding and re-seeding, scope 5.5.

Re-seed is an explicit Admin action, never automatic and never on app start. It
is additive and non-destructive:

* new rows are inserted;
* existing rows have their hint fields refreshed;
* rows that vanish upstream are disabled, not deleted;
* slugs are never rewritten, because files already reference them.

One consequence worth stating: a row that vanishes upstream is disabled, and if
it later reappears it stays disabled until an Admin re-enables it. That is
deliberate -- silently re-enabling would override a deliberate admin disable,
and the two are indistinguishable once written.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog.build import CatalogRow, SkippedForm, build_row, select_forms
from app.catalog.pokeapi import PokeApiClient
from app.models import Pokemon, get_or_create_settings
from app.slugs import assert_unique

log = logging.getLogger(__name__)


@dataclass
class SeedReport:
    fetched_species: int = 0
    built_rows: int = 0
    inserted: int = 0
    updated: int = 0
    disabled_missing: int = 0
    slug_conflicts: list[str] = field(default_factory=list)
    skipped_forms: list[SkippedForm] = field(default_factory=list)
    unknown_forms: list[SkippedForm] = field(default_factory=list)
    snapshot_label: str = ""

    def summary(self) -> str:
        lines = [
            f"species fetched : {self.fetched_species}",
            f"answer units    : {self.built_rows}",
            f"inserted        : {self.inserted}",
            f"updated         : {self.updated}",
            f"disabled (gone) : {self.disabled_missing}",
        ]
        if self.slug_conflicts:
            lines.append(f"slug kept (would have changed): {len(self.slug_conflicts)}")
            lines.extend(f"  - {c}" for c in self.slug_conflicts)
        if self.unknown_forms:
            lines.append(
                f"UNRECOGNISED FORMS SKIPPED: {len(self.unknown_forms)} "
                "-- these are absent from the catalog, review app/catalog/overrides.py"
            )
            lines.extend(f"  - {s.form_key}: {s.reason}" for s in self.unknown_forms)
        return "\n".join(lines)


async def fetch_catalog(
    client: PokeApiClient, *, limit: int | None = None
) -> tuple[list[CatalogRow], SeedReport]:
    report = SeedReport()

    species_refs = await client.list_species()
    if limit:
        species_refs = species_refs[:limit]
    report.fetched_species = len(species_refs)

    species_payloads = await client.get_many([ref["url"] for ref in species_refs])

    # Collect every variety we intend to keep, then fetch their /pokemon entries
    # in one batch so type data comes back concurrently.
    plan: list[tuple[object, dict]] = []
    for species in species_payloads:
        keep, skipped = select_forms(species)
        report.skipped_forms.extend(skipped)
        report.unknown_forms.extend(
            s for s in skipped if s.reason.startswith("unrecognised") or "unexpected" in s.reason
        )
        for ref in keep:
            plan.append((ref, species))

    variety_payloads = await client.get_many([f"pokemon/{ref.form_key}" for ref, _ in plan])

    rows = [build_row(ref, species, variety) for (ref, species), variety in zip(plan, variety_payloads)]
    rows.sort(key=lambda r: (r.national_dex, r.region_key or "", r.breed_key or ""))
    report.built_rows = len(rows)

    # Scope 8.1: hard failure, never an auto-suffix.
    assert_unique([(r.slug, r.display_name) for r in rows], label="pokemon slug")
    assert_unique([(r.display_name, r.form_key) for r in rows], label="display name")

    report.snapshot_label = (
        f"pokeapi.co @ {date.today().isoformat()} - "
        f"{report.fetched_species} species, {report.built_rows} answer units"
    )
    return rows, report


def apply_catalog(session: Session, rows: list[CatalogRow], report: SeedReport) -> SeedReport:
    existing = {p.form_key: p for p in session.scalars(select(Pokemon)).all()}
    seen: set[str] = set()

    for row in rows:
        seen.add(row.form_key)
        current = existing.get(row.form_key)
        if current is None:
            session.add(
                Pokemon(
                    form_key=row.form_key,
                    display_name=row.display_name,
                    slug=row.slug,
                    national_dex=row.national_dex,
                    generation=row.generation,
                    region_key=row.region_key,
                    breed_key=row.breed_key,
                    types=row.types,
                    species_category=row.species_category,
                    dex_entries=row.dex_entries,
                    enabled=True,
                )
            )
            report.inserted += 1
            continue

        if current.slug != row.slug:
            # Files already point at the old slug, so it wins (scope 5.5).
            report.slug_conflicts.append(
                f"{row.form_key}: keeping {current.slug!r}, upstream now implies {row.slug!r}"
            )
            log.warning("slug change suppressed for %s", row.form_key)

        current.display_name = row.display_name
        current.national_dex = row.national_dex
        current.generation = row.generation
        current.region_key = row.region_key
        current.breed_key = row.breed_key
        current.types = row.types
        current.species_category = row.species_category
        current.dex_entries = row.dex_entries
        # `enabled` is untouched: it is admin state, not upstream state.
        report.updated += 1

    for form_key, pokemon in existing.items():
        if form_key not in seen and pokemon.enabled:
            pokemon.enabled = False
            report.disabled_missing += 1

    settings = get_or_create_settings(session)
    settings.catalog_snapshot_label = report.snapshot_label
    return report


async def seed_async(
    session: Session, *, limit: int | None = None, use_cache: bool = True
) -> SeedReport:
    async with PokeApiClient(use_cache=use_cache) as client:
        rows, report = await fetch_catalog(client, limit=limit)
        log.info("fetched %d rows (%d requests, %d cache hits)", len(rows), client.request_count, client.cache_hits)
    return apply_catalog(session, rows, report)


def seed(session: Session, *, limit: int | None = None, use_cache: bool = True) -> SeedReport:
    return asyncio.run(seed_async(session, limit=limit, use_cache=use_cache))
