"""Scope 8.1. The parametrised cases are exactly the worked-example table."""

from __future__ import annotations

import pytest

from app.slugs import SlugCollision, artist_slug, assert_unique, pokemon_slug, slugify


@pytest.mark.parametrize(
    "display_name, region, breed, expected",
    [
        ("Bulbasaur", None, None, "bulbasaur"),
        ("Mr. Mime", None, None, "mr-mime"),
        ("Mime Jr.", None, None, "mime-jr"),
        ("Type: Null", None, None, "type-null"),
        ("Farfetch'd", None, None, "farfetchd"),
        ("Ho-Oh", None, None, "ho-oh"),
        ("Porygon-Z", None, None, "porygon-z"),
        ("Nidoran♀", None, None, "nidoran-f"),
        ("Nidoran♂", None, None, "nidoran-m"),
        ("Flabébé", None, None, "flabebe"),
        ("Vulpix", "alolan", None, "vulpix-alolan"),
        ("Weezing", "galarian", None, "weezing-galarian"),
        ("Tauros", "paldean", "blaze", "tauros-paldean-blaze"),
    ],
)
def test_scope_worked_examples(display_name, region, breed, expected):
    assert pokemon_slug(display_name, region, breed) == expected


def test_apostrophes_are_deleted_not_hyphenated():
    # The whole point of step 4: farfetch-d would be wrong.
    assert slugify("Farfetch'd") == "farfetchd"
    assert slugify("Sirfetch’d") == "sirfetchd"


def test_gender_signs_survive_the_non_alphanumeric_sweep():
    # Step 3 must run before step 6, or these collapse to a bare "nidoran".
    assert slugify("Nidoran♀") != slugify("Nidoran♂")


def test_accents_fold_rather_than_vanish():
    assert slugify("Flabébé") == "flabebe"
    assert slugify("Pokémon") == "pokemon"


def test_region_trails_in_slug_while_display_name_leads():
    assert pokemon_slug("Vulpix", "alolan") == "vulpix-alolan"


def test_empty_slug_is_an_error():
    with pytest.raises(ValueError):
        pokemon_slug("???")


def test_artist_slug():
    assert artist_slug("Alex") == "alex"
    assert artist_slug("Drake E.") == "drake-e"
    with pytest.raises(ValueError):
        artist_slug("!!!")


def test_assert_unique_passes_on_distinct_slugs():
    assert_unique([("a", "A"), ("b", "B")])


def test_assert_unique_names_both_offenders():
    with pytest.raises(SlugCollision) as exc:
        assert_unique([("vulpix", "Vulpix"), ("vulpix", "Vulpix Clone")])
    message = str(exc.value)
    assert "Vulpix Clone" in message and "never auto-suffixed" in message
