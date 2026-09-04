"""Slug generation, scope 8.1.

Slugs end up inside permanent public filenames, so this module is deliberately
boring and total: every rule is explicit, nothing is guessed, and a collision is
a hard failure rather than something quietly patched with a numeric suffix.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

# Step 3: gender signs have no Unicode decomposition, so they must be mapped
# before the non-alphanumeric sweep turns them into hyphens. They map to a
# *separated* letter, because the scope specifies nidoran-f rather than nidoranf
# and there is no punctuation of its own between the name and the sign.
_GENDER_MAP = {
    "♀": "-f",  # female sign
    "♂": "-m",  # male sign
}

# Step 4: apostrophes are deleted rather than hyphenated, so Farfetch'd becomes
# farfetchd and not farfetch-d.
_DELETED_CHARS = "'’ʼ´`\"“”"

_NON_SLUG = re.compile(r"[^a-z0-9]+")


class SlugCollision(RuntimeError):
    """Two catalog rows produced the same slug.

    Never resolved automatically: slugs appear in filenames that are written
    once and never overwritten, so an auto-suffix would silently point new
    drawings at a different species' naming space.
    """


def slugify(text: str) -> str:
    """Apply scope 8.1 steps 2-7 to an arbitrary display string."""
    # Step 2: NFKD normalise and drop combining marks (Flabebe).
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))

    # Step 3: explicit gender-sign mapping.
    mapped = "".join(_GENDER_MAP.get(c, c) for c in stripped)

    # Step 4: delete apostrophes and typographic quotes outright.
    without_quotes = "".join(c for c in mapped if c not in _DELETED_CHARS)

    # Step 5: lowercase.
    lowered = without_quotes.lower()

    # Steps 6 and 7: collapse non-slug runs to a single hyphen, then trim.
    return _NON_SLUG.sub("-", lowered).strip("-")


def pokemon_slug(
    base_species_name: str,
    region_key: str | None = None,
    breed_key: str | None = None,
) -> str:
    """Build a catalog slug.

    ``base_species_name`` is the species name *without* the regional prefix:
    "Alolan Vulpix" is passed as "Vulpix". Step 8 then appends the region and
    breed, so the display name leads with the region while the slug trails with
    it (scope 5.2).
    """
    parts = [slugify(base_species_name)]
    if not parts[0]:
        raise ValueError(f"species name {base_species_name!r} slugified to nothing")
    if region_key:
        parts.append(slugify(region_key))
    if breed_key:
        parts.append(slugify(breed_key))
    return "-".join(p for p in parts if p)


def artist_slug(username: str) -> str:
    """Slug for a user account.

    Frozen at account creation and never regenerated, because it is baked into
    filenames that outlive any later display-name change (scope 8.1, C10).
    """
    slug = slugify(username)
    if not slug:
        raise ValueError(f"username {username!r} slugified to nothing")
    return slug


def assert_unique(pairs: Iterable[tuple[str, str]], *, label: str = "slug") -> None:
    """Fail loudly on duplicate slugs.

    ``pairs`` is (slug, human_identifier). Runs at the end of every seed and
    re-seed (scope 8.1); the error names both offenders so the override table
    can be corrected by hand.
    """
    seen: dict[str, str] = {}
    for slug, owner in pairs:
        if slug in seen:
            raise SlugCollision(
                f"duplicate {label} {slug!r}: {seen[slug]!r} and {owner!r}. "
                "Resolve in the catalog override table; slugs are never auto-suffixed."
            )
        seen[slug] = owner
