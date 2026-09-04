"""Scope 5.2 / 5.4 form parsing and hint selection, against fixture payloads."""

from __future__ import annotations

import pytest

from app.catalog.build import (
    build_display_name,
    build_row,
    clean_flavor,
    parse_variety,
    select_dex_entries,
    select_forms,
)


def species_payload(**overrides):
    base = {
        "id": 37,
        "name": "vulpix",
        "names": [{"language": {"name": "en"}, "name": "Vulpix"}],
        "genera": [{"language": {"name": "en"}, "genus": "Fox Pokémon"}],
        "generation": {"name": "generation-i"},
        "flavor_text_entries": [
            {"language": {"name": "en"}, "version": {"name": "red"}, "flavor_text": "Kanto text."},
            {"language": {"name": "en"}, "version": {"name": "sun"}, "flavor_text": "Alola text."},
            {"language": {"name": "ja"}, "version": {"name": "sun"}, "flavor_text": "Japanese."},
        ],
        "varieties": [
            {"is_default": True, "pokemon": {"name": "vulpix"}},
            {"is_default": False, "pokemon": {"name": "vulpix-alola"}},
        ],
    }
    base.update(overrides)
    return base


# --- form parsing -----------------------------------------------------------

def test_default_variety_is_the_base_answer_unit():
    ref, skip = parse_variety("vulpix", "vulpix", is_default=True)
    assert skip is None
    assert ref.region_key is None and ref.breed_key is None


def test_plain_regional_form_is_kept():
    ref, _ = parse_variety("vulpix", "vulpix-alola", is_default=False)
    assert ref.region_key == "alolan"
    assert ref.breed_key is None


def test_paldean_tauros_breeds_strip_the_trailing_breed_token():
    ref, _ = parse_variety("tauros", "tauros-paldea-combat-breed", is_default=False)
    assert (ref.region_key, ref.breed_key) == ("paldean", "combat")


def test_galarian_darmanitan_standard_is_the_canonical_form():
    # Upstream splits this into -standard and -zen; standard *is* Galarian
    # Darmanitan, so it must not be mistaken for an unrecognised sub-form.
    ref, skip = parse_variety("darmanitan", "darmanitan-galar-standard", is_default=False)
    assert skip is None
    assert ref.region_key == "galarian" and ref.breed_key is None


def test_zen_mode_is_dropped_rather_than_becoming_a_second_answer():
    ref, skip = parse_variety("darmanitan", "darmanitan-galar-zen", is_default=False)
    assert ref is None
    assert "battle mode" in skip.reason


def test_alola_cap_pikachu_is_excluded():
    ref, skip = parse_variety("pikachu", "pikachu-alola-cap", is_default=False)
    assert ref is None
    assert "excluded form" in skip.reason


def test_non_regional_alternate_forms_collapse_into_the_default():
    ref, skip = parse_variety("toxtricity", "toxtricity-low-key", is_default=False)
    assert ref is None
    assert skip.reason == "non-regional alternate form"


def test_mega_and_gmax_never_become_answers():
    for name in ("charizard-mega-x", "charizard-gmax"):
        ref, skip = parse_variety("charizard", name, is_default=False)
        assert ref is None, name
        assert "excluded form" in skip.reason


def test_select_forms_returns_base_plus_regional():
    keep, _ = select_forms(species_payload())
    assert [f.form_key for f in keep] == ["vulpix", "vulpix-alola"]


# --- display names ----------------------------------------------------------

@pytest.mark.parametrize(
    "region, breed, expected",
    [
        (None, None, "Vulpix"),
        ("alolan", None, "Alolan Vulpix"),
        ("galarian", None, "Galarian Vulpix"),
    ],
)
def test_display_name_leads_with_region(region, breed, expected):
    assert build_display_name("Vulpix", region, breed) == expected


def test_paldean_tauros_display_name():
    assert build_display_name("Tauros", "paldean", "blaze") == "Paldean Tauros (Blaze)"


# --- hint fields ------------------------------------------------------------

def test_regional_generation_override_beats_the_species_generation():
    species = species_payload()
    ref, _ = parse_variety("vulpix", "vulpix-alola", is_default=False)
    row = build_row(ref, species, {"types": [{"slot": 1, "type": {"name": "ice"}}]})
    # Vulpix is a Generation 1 species; the Alolan form is Generation 7.
    assert row.generation == 7
    assert row.display_name == "Alolan Vulpix"
    assert row.slug == "vulpix-alolan"
    assert row.types == ["Ice"]


def test_base_form_keeps_the_species_generation():
    ref, _ = parse_variety("vulpix", "vulpix", is_default=True)
    row = build_row(ref, species_payload(), {"types": [{"slot": 1, "type": {"name": "fire"}}]})
    assert row.generation == 1
    assert row.types == ["Fire"]


def test_regional_dex_entries_prefer_the_forms_own_games():
    assert select_dex_entries(species_payload(), "alolan") == ["Alola text."]


def test_regional_dex_entries_fall_back_rather_than_showing_nothing():
    # Hisuian prefers legends-arceus, which this fixture has no entry for.
    entries = select_dex_entries(species_payload(), "hisuian")
    assert entries, "empty hint panel is never acceptable"
    assert "Alola text." in entries or "Kanto text." in entries


def test_dex_entries_are_english_only():
    assert all("Japanese" not in e for e in select_dex_entries(species_payload(), None))


def test_types_are_ordered_by_slot():
    ref, _ = parse_variety("vulpix", "vulpix", is_default=True)
    row = build_row(
        ref,
        species_payload(),
        {"types": [{"slot": 2, "type": {"name": "flying"}}, {"slot": 1, "type": {"name": "fire"}}]},
    )
    assert row.types == ["Fire", "Flying"]


def test_species_category_is_inherited_by_regional_forms():
    ref, _ = parse_variety("vulpix", "vulpix-alola", is_default=False)
    row = build_row(ref, species_payload(), {"types": []})
    assert row.species_category == "Fox Pokémon"


def test_clean_flavor_unwraps_game_text():
    raw = "It can freely\ncontrol fire.\x0cIts tail­flame burns."
    assert clean_flavor(raw) == "It can freely control fire. Its tailflame burns."
