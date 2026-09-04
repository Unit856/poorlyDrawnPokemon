"""Pure transforms from PokeAPI payloads to catalog rows.

Deliberately free of both network and database access so the awkward parts --
form parsing, the scope 5.4 overrides, flavor-text selection -- can be tested
against fixtures instead of against the live API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.catalog import overrides as ov
from app.slugs import pokemon_slug

_WHITESPACE = re.compile(r"\s+")


@dataclass
class CatalogRow:
    """One answer unit (scope 5.1)."""

    form_key: str
    display_name: str
    slug: str
    national_dex: int
    generation: int
    region_key: str | None = None
    breed_key: str | None = None
    types: list[str] = field(default_factory=list)
    species_category: str = ""
    dex_entries: list[str] = field(default_factory=list)


@dataclass
class FormRef:
    """A variety we intend to keep, before its hint data is fetched."""

    form_key: str
    species_name: str
    region_key: str | None
    breed_key: str | None


@dataclass
class SkippedForm:
    form_key: str
    reason: str


def english(entries: list[dict[str, Any]], key: str) -> str | None:
    for entry in entries:
        if entry.get("language", {}).get("name") == "en":
            return entry.get(key)
    return None


def clean_flavor(text: str) -> str:
    """PokeAPI flavor text carries hard line breaks, form feeds and soft hyphens."""
    unwrapped = (
        text.replace("\u00ad", "")  # soft hyphen from the games' line wrapping
        .replace("\x0c", " ")  # form feed, used upstream as a paragraph break
        .replace("\r", " ")
        .replace("\n", " ")
    )
    return _WHITESPACE.sub(" ", unwrapped).strip()


def parse_variety(species_name: str, variety_name: str, *, is_default: bool) -> tuple[
    FormRef | None, SkippedForm | None
]:
    """Classify one PokeAPI variety.

    Returns (keep, skip) with exactly one of them set.
    """
    if is_default:
        return FormRef(variety_name, species_name, None, None), None

    for fragment in ov.EXCLUDED_FRAGMENTS:
        if fragment in variety_name:
            return None, SkippedForm(variety_name, f"excluded form ({fragment.strip('-')})")

    prefix = f"{species_name}-"
    if not variety_name.startswith(prefix):
        return None, SkippedForm(variety_name, "non-default variety with unexpected name shape")

    tokens = variety_name[len(prefix):].split("-")
    stem = tokens[0]
    region = ov.BY_API_STEM.get(stem)
    if region is None:
        # A non-regional alternate form (Zygarde, Toxtricity, Lycanroc...). These
        # collapse into their default variety by design (scope 5.1).
        return None, SkippedForm(variety_name, "non-regional alternate form")

    rest = tokens[1:]
    if rest and rest[-1] == ov.BREED_SUFFIX.strip("-"):
        rest = rest[:-1]

    if not rest:
        return FormRef(variety_name, species_name, region.region_key, None), None

    if len(rest) == 1:
        token = rest[0]
        if token in ov.KNOWN_BREEDS:
            return FormRef(variety_name, species_name, region.region_key, token), None
        if token in ov.CANONICAL_MODES:
            # Galarian Darmanitan (Standard) is simply "Galarian Darmanitan".
            return FormRef(variety_name, species_name, region.region_key, None), None
        if token in ov.DROPPED_MODES:
            return None, SkippedForm(variety_name, f"battle mode ({token})")

    return None, SkippedForm(variety_name, "unrecognised regional sub-form")


def select_forms(species: dict[str, Any]) -> tuple[list[FormRef], list[SkippedForm]]:
    keep: list[FormRef] = []
    skipped: list[SkippedForm] = []
    for variety in species.get("varieties", []):
        ref, skip = parse_variety(
            species["name"],
            variety["pokemon"]["name"],
            is_default=bool(variety.get("is_default")),
        )
        if ref is not None:
            keep.append(ref)
        elif skip is not None:
            skipped.append(skip)
    return keep, skipped


def build_display_name(
    species_english_name: str, region_key: str | None, breed_key: str | None
) -> str:
    """Scope 5.2: the display name leads with the region, the slug trails with it."""
    if region_key is None:
        return species_english_name
    region = ov.BY_REGION_KEY[region_key]
    name = f"{region.display_prefix} {species_english_name}"
    if breed_key:
        name = f"{name} ({breed_key.capitalize()})"
    return name


def select_dex_entries(
    species: dict[str, Any], region_key: str | None, limit: int = 3
) -> list[str]:
    """Scope 5.3 / 5.4.

    Regional forms prefer flavor text from their own games. If a form has none in
    those versions, fall back to the species entries newest-first rather than
    showing an empty hint panel.
    """
    english_entries = [
        e for e in species.get("flavor_text_entries", []) if e.get("language", {}).get("name") == "en"
    ]

    def dedupe(texts: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for text in texts:
            cleaned = clean_flavor(text)
            marker = cleaned.lower()
            if cleaned and marker not in seen:
                seen.add(marker)
                out.append(cleaned)
        return out

    if region_key is not None:
        preferred = ov.BY_REGION_KEY[region_key].preferred_versions
        picked = dedupe(
            [
                e["flavor_text"]
                for version in preferred
                for e in english_entries
                if e.get("version", {}).get("name") == version
            ]
        )
        if picked:
            return picked[:limit]

    # Newest first: PokeAPI returns entries oldest-first by version.
    return dedupe([e["flavor_text"] for e in reversed(english_entries)])[:limit]


def build_row(
    ref: FormRef,
    species: dict[str, Any],
    variety: dict[str, Any],
) -> CatalogRow:
    species_english = english(species.get("names", []), "name") or species["name"].title()
    display_name = build_display_name(species_english, ref.region_key, ref.breed_key)

    if ref.region_key is not None:
        generation = ov.BY_REGION_KEY[ref.region_key].generation
    else:
        gen_name = species.get("generation", {}).get("name", "")
        generation = ov.GENERATION_NAME_TO_NUMBER.get(gen_name, 0)

    types = [
        t["type"]["name"].capitalize()
        for t in sorted(variety.get("types", []), key=lambda t: t.get("slot", 0))
    ]

    return CatalogRow(
        form_key=ref.form_key,
        display_name=display_name,
        slug=pokemon_slug(species_english, ref.region_key, ref.breed_key),
        national_dex=int(species.get("id", 0)),
        generation=generation,
        region_key=ref.region_key,
        breed_key=ref.breed_key,
        types=types,
        # Genus is species-level upstream, so regional forms inherit the base
        # category. Accepted as a hint (scope 5.4).
        species_category=english(species.get("genera", []), "genus") or "",
        dex_entries=select_dex_entries(species, ref.region_key),
    )
