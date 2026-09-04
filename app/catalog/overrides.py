"""Regional form overrides, scope 5.4.

PokeAPI hangs generation, Pokedex category and flavor text off *pokemon-species*,
which a base species shares with all of its regional forms. Left alone, Alolan
Vulpix reports Generation 1 and Kanto flavor text. This table is the hardcoded
correction.

Note that PokeAPI's variety names use the region stem ("alola") while the scope's
region_key uses the demonym ("alolan"). Both are kept: the stem parses upstream
names, the demonym is what we store and put in slugs.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RegionOverride:
    api_stem: str
    """Suffix PokeAPI uses in variety names, e.g. vulpix-alola."""

    region_key: str
    """Stored key and slug suffix, e.g. vulpix-alolan."""

    display_prefix: str
    """Leads the display name, e.g. "Alolan Vulpix"."""

    generation: int
    """National Dex introduction generation of the *form*, not the species."""

    preferred_versions: tuple[str, ...] = field(default_factory=tuple)
    """PokeAPI version names to prefer for flavor text.

    The scope names version *groups*; PokeAPI tags flavor text with individual
    versions, so each group is expanded here.
    """


REGIONS: tuple[RegionOverride, ...] = (
    RegionOverride(
        api_stem="alola",
        region_key="alolan",
        display_prefix="Alolan",
        generation=7,
        preferred_versions=("sun", "moon", "ultra-sun", "ultra-moon"),
    ),
    RegionOverride(
        api_stem="galar",
        region_key="galarian",
        display_prefix="Galarian",
        generation=8,
        preferred_versions=("sword", "shield"),
    ),
    RegionOverride(
        api_stem="hisui",
        region_key="hisuian",
        display_prefix="Hisuian",
        generation=8,
        preferred_versions=("legends-arceus",),
    ),
    RegionOverride(
        api_stem="paldea",
        region_key="paldean",
        display_prefix="Paldean",
        generation=9,
        preferred_versions=("scarlet", "violet"),
    ),
)

BY_API_STEM: dict[str, RegionOverride] = {r.api_stem: r for r in REGIONS}
BY_REGION_KEY: dict[str, RegionOverride] = {r.region_key: r for r in REGIONS}

# Breed suffixes that survive as separate answer units (scope 5.2). PokeAPI names
# these tauros-paldea-combat-breed, so the trailing "-breed" is noise to strip.
BREED_SUFFIX = "-breed"
KNOWN_BREEDS: frozenset[str] = frozenset({"combat", "blaze", "aqua"})

# Battle modes that appear *after* a region stem. Galarian Darmanitan is the only
# current case: upstream has darmanitan-galar-standard and darmanitan-galar-zen.
# Zen Mode is a temporary in-battle state, not a separate answer (scope 5.1), so
# the standard mode is taken as the canonical Galarian Darmanitan and Zen is
# dropped.
CANONICAL_MODES: frozenset[str] = frozenset({"standard"})
DROPPED_MODES: frozenset[str] = frozenset({"zen"})

# Variety name fragments that are explicitly *not* separate answers (scope 2.2 /
# 5.1). Checked before the region parser so that, say, a hypothetical
# "-galar-gmax" variety is rejected rather than mistaken for a plain Galarian form.
EXCLUDED_FRAGMENTS: tuple[str, ...] = (
    "-mega",
    "-gmax",
    "-totem",
    "-cap",
    "-starter",
    "-cosplay",
    "-original",
    "-world",
    "-rock-star",
    "-belle",
    "-pop-star",
    "-phd",
    "-libre",
)

GENERATION_NAME_TO_NUMBER: dict[str, int] = {
    "generation-i": 1,
    "generation-ii": 2,
    "generation-iii": 3,
    "generation-iv": 4,
    "generation-v": 5,
    "generation-vi": 6,
    "generation-vii": 7,
    "generation-viii": 8,
    "generation-ix": 9,
    "generation-x": 10,
}
