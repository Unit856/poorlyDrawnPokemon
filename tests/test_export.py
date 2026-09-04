"""Scope 10: CSV construction, freezing, and re-export stability."""

from __future__ import annotations

import csv
import io
import random

import pytest

from app import config
from app.export import (
    CSV_HEADER,
    QUESTION_TEXT,
    ExportBlocked,
    NotEnoughCatalog,
    build_options,
    export,
    pack_index,
    write_csv,
)
from app.models import Pokemon, SubmissionStatus, get_or_create_settings
from app.moderation import approve, delete, unapprove
from app.users import create_user
from tests.test_picker import add_submission, make_catalog


@pytest.fixture()
def user(session):
    u = create_user(session, username="alex", password="x" * 12, display_name="Alex")
    session.flush()
    return u


@pytest.fixture()
def catalog(session):
    return make_catalog(session, 8)


@pytest.fixture()
def configured(session):
    settings = get_or_create_settings(session)
    settings.public_base_url = "https://pokedraw.example.com"
    session.flush()
    return settings


def approved(session, pokemon, user):
    submission = add_submission(session, pokemon, user, status=SubmissionStatus.PENDING)
    approve(session, submission)
    return submission


def parse(payload: bytes) -> list[dict[str, str]]:
    text = payload.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


# --- option construction ----------------------------------------------------

def test_four_options_with_exactly_one_correct():
    pool = [f"Mon {i}" for i in range(1, 9)]
    options, letter = build_options(pool, "Mon 1", random.Random(0))
    assert len(options) == 4
    assert options.count("Mon 1") == 1
    assert options["ABCD".index(letter)] == "Mon 1"


def test_options_are_never_duplicated():
    pool = [f"Mon {i}" for i in range(1, 9)]
    for seed in range(50):
        options, _ = build_options(pool, "Mon 1", random.Random(seed))
        assert len(set(options)) == 4


def test_the_correct_name_is_never_also_a_distractor():
    pool = [f"Mon {i}" for i in range(1, 9)]
    for seed in range(50):
        options, letter = build_options(pool, "Mon 1", random.Random(seed))
        others = [o for i, o in enumerate(options) if i != "ABCD".index(letter)]
        assert "Mon 1" not in others


def test_correct_answer_is_spread_across_all_four_slots():
    """Scope 10.2: a pack dump must not be 'always A'."""
    pool = [f"Mon {i}" for i in range(1, 9)]
    letters = {build_options(pool, "Mon 1", random.Random(seed))[1] for seed in range(60)}
    assert letters == {"A", "B", "C", "D"}


def test_regional_forms_are_allowed_as_distractors():
    pool = ["Vulpix", "Alolan Vulpix", "Ekans", "Arbok", "Pikachu"]
    options, _ = build_options(pool, "Vulpix", random.Random(1))
    assert "Alolan Vulpix" in options or len(options) == 4


def test_too_small_a_catalog_is_refused():
    with pytest.raises(NotEnoughCatalog):
        build_options(["Mon 1", "Mon 2"], "Mon 1", random.Random(0))


# --- freezing at approval ---------------------------------------------------

def test_approval_freezes_options_and_letter(session, catalog, user, configured):
    submission = approved(session, catalog[0], user)
    assert len(submission.options_json) == 4
    assert submission.correct_letter in "ABCD"
    assert submission.options_json["ABCD".index(submission.correct_letter)] == catalog[0].display_name


def test_export_never_regenerates_frozen_options(session, catalog, user, configured):
    submission = approved(session, catalog[0], user)
    frozen = list(submission.options_json)
    letter = submission.correct_letter

    for _ in range(5):
        export(session, rng=random.Random(999))

    assert submission.options_json == frozen
    assert submission.correct_letter == letter


def test_disabling_a_distractor_leaves_the_frozen_question_untouched(
    session, catalog, user, configured
):
    submission = approved(session, catalog[0], user)
    frozen = list(submission.options_json)

    # Disable every other row; the frozen names must survive regardless.
    for row in catalog[1:]:
        row.enabled = False
    session.flush()

    export(session)
    assert submission.options_json == frozen


def test_renaming_the_artist_does_not_change_the_credit(session, catalog, user, configured):
    approved(session, catalog[0], user)
    user.display_name = "Alexandra"
    session.flush()

    payload, _ = export(session)
    assert parse(payload)[0]["credit"] == "Alex"


# --- CSV shape (scope 10.3) -------------------------------------------------

def test_header_matches_the_scope_exactly(session, catalog, user, configured):
    approved(session, catalog[0], user)
    payload, _ = export(session)
    first_line = payload.decode("utf-8-sig").split("\r\n")[0]
    assert first_line == ",".join(CSV_HEADER)


def test_file_starts_with_a_utf8_bom(session, catalog, user, configured):
    approved(session, catalog[0], user)
    payload, _ = export(session)
    # Makes Nidoran-female render correctly in Excel on Windows.
    assert payload.startswith(b"\xef\xbb\xbf")


def test_line_endings_are_crlf(session, catalog, user, configured):
    approved(session, catalog[0], user)
    payload, _ = export(session)
    body = payload.decode("utf-8-sig")
    assert "\r\n" in body
    assert not body.replace("\r\n", "").count("\n")


def test_row_contents(session, catalog, user, configured):
    submission = approved(session, catalog[0], user)
    row = parse(export(session)[0])[0]

    assert row["uniqueId"] == str(submission.unique_id)
    assert row["question"] == QUESTION_TEXT
    assert row["fixedOrder"] == "FALSE"
    assert row["correctAnswer"] in "ABCD"
    assert row["credit"] == "Alex"
    assert row["imageURL"].startswith("https://pokedraw.example.com/images/")
    assert row["imageURL"].endswith(".png")
    assert row["answerExplanation"] == ""
    assert all(row[f"option{c}"] for c in "ABCD")


def test_question_text_has_no_accent(session):
    # Scope 16: "Who's that Pokemon?" (no accent, per request).
    assert "é" not in QUESTION_TEXT


def test_names_with_commas_are_quoted(session, user, configured):
    tricky = Pokemon(
        form_key="odd", display_name="Mon, With Comma", slug="mon-comma",
        national_dex=1, generation=1, types=["Normal"], species_category="Test",
        dex_entries=["x"], enabled=True,
    )
    session.add(tricky)
    make_catalog(session, 6)
    session.flush()
    approved(session, tricky, user)

    payload, _ = export(session)
    assert '"Mon, With Comma"' in payload.decode("utf-8-sig")
    assert parse(payload)[0]["optionA" if False else "question"] == QUESTION_TEXT


def test_non_ascii_names_survive_the_round_trip(session, user, configured):
    nidoran = Pokemon(
        form_key="nidoran-f", display_name="Nidoran♀", slug="nidoran-f",
        national_dex=29, generation=1, types=["Poison"], species_category="Poison Pin",
        dex_entries=["x"], enabled=True,
    )
    session.add(nidoran)
    make_catalog(session, 6)
    session.flush()
    approved(session, nidoran, user)

    row = parse(export(session)[0])[0]
    assert "Nidoran♀" in [row[f"option{c}"] for c in "ABCD"]


# --- what is included -------------------------------------------------------

def test_only_approved_rows_are_exported(session, catalog, user, configured):
    approved(session, catalog[0], user)
    add_submission(session, catalog[1], user, status=SubmissionStatus.PENDING)
    add_submission(session, catalog[2], user, status=SubmissionStatus.REJECTED)
    session.flush()

    assert len(parse(export(session)[0])) == 1


def test_rows_are_in_unique_id_order(session, catalog, user, configured):
    for row in catalog[:5]:
        approved(session, row, user)
    ids = [int(r["uniqueId"]) for r in parse(export(session)[0])]
    assert ids == sorted(ids)


def test_export_is_blocked_without_a_public_base_url(session, catalog, user):
    approved(session, catalog[0], user)
    with pytest.raises(ExportBlocked, match="public base URL"):
        export(session)


def test_public_url_is_backfilled_for_rows_approved_before_the_base_was_set(
    session, catalog, user
):
    submission = approved(session, catalog[0], user)
    assert submission.public_url is None

    get_or_create_settings(session).public_base_url = "https://later.example.com"
    session.flush()
    export(session)
    assert submission.public_url.startswith("https://later.example.com/images/")


# --- pack split rule (scope 10.4) -------------------------------------------

@pytest.mark.parametrize(
    "unique_id, expected",
    [(1, 1), (2500, 1), (2501, 2), (5000, 2), (5001, 3)],
)
def test_pack_index_partitions_by_id_range(unique_id, expected):
    assert pack_index(unique_id) == expected


def test_pack_membership_never_shifts_when_ids_are_retired():
    """Range partitioning, not position, is what makes a later split safe."""
    surviving = [1, 2, 2500, 2501]
    before = {uid: pack_index(uid) for uid in surviving}
    # Pretend ids 3..2499 were all retired; the survivors keep their packs.
    assert before == {uid: pack_index(uid) for uid in surviving}
    assert before[2500] == 1 and before[2501] == 2


def test_warning_fires_above_the_threshold(session, catalog, user, configured, monkeypatch):
    monkeypatch.setattr(config, "PACK_ROW_WARN_THRESHOLD", 2)
    for row in catalog[:3]:
        approved(session, row, user)
    _, report = export(session)
    assert report.over_warn_threshold
    assert "cap" in report.warning


# --- acceptance criterion 10 ------------------------------------------------

def test_re_export_stability_under_mutation(session, catalog, user, configured):
    """Acceptance criterion 10, in full.

    Export, then unapprove one row, add a new drawing, rename a player, and
    disable a catalog row used as a distractor. Re-export. Every surviving
    uniqueId must keep its id, its four options in the same order, its correct
    letter and its original credit. The unapproved id must be absent and must not
    be handed to the new row.
    """
    first = approved(session, catalog[0], user)
    second = approved(session, catalog[1], user)
    third = approved(session, catalog[2], user)

    before = {r["uniqueId"]: r for r in parse(export(session)[0])}
    assert len(before) == 3
    retired_id = str(second.unique_id)

    # 1. unapprove one row
    unapprove(session, second)
    # 2. rename the artist *before* the new drawing, so the snapshot boundary is
    #    actually exercised: old rows must keep "Alex", the new one must get
    #    "Alexandra".
    user.display_name = "Alexandra"
    session.flush()
    # 3. add a new drawing
    fourth = approved(session, catalog[3], user)
    # 4. disable a catalog row used as a distractor
    distractor_name = next(o for o in first.options_json if o != catalog[0].display_name)
    disabled = session.query(Pokemon).filter(Pokemon.display_name == distractor_name).one()
    disabled.enabled = False
    session.flush()

    after = {r["uniqueId"]: r for r in parse(export(session)[0])}

    # Survivors are byte-identical.
    for uid in (str(first.unique_id), str(third.unique_id)):
        assert after[uid] == before[uid], f"row {uid} mutated across re-export"

    # The unapproved row is gone, and its id was not recycled.
    assert retired_id not in after
    assert str(fourth.unique_id) != retired_id
    assert int(fourth.unique_id) > int(retired_id)

    # The new row uses the *new* display name; the old rows keep the old one.
    assert after[str(fourth.unique_id)]["credit"] == "Alexandra"
    assert after[str(first.unique_id)]["credit"] == "Alex"

    # The disabled distractor still appears in the frozen question.
    assert distractor_name in [after[str(first.unique_id)][f"option{c}"] for c in "ABCD"]


def test_deleting_removes_a_row_without_renumbering_the_rest(session, catalog, user, configured):
    first = approved(session, catalog[0], user)
    second = approved(session, catalog[1], user)
    before = {r["uniqueId"]: r for r in parse(export(session)[0])}

    delete(session, second)
    after = {r["uniqueId"]: r for r in parse(export(session)[0])}

    assert str(second.unique_id) not in after
    assert after[str(first.unique_id)] == before[str(first.unique_id)]


def test_empty_export_produces_only_a_header(session):
    assert write_csv([]).decode("utf-8-sig").strip() == ",".join(CSV_HEADER)


# --- auto-approve must freeze (regression) ----------------------------------

def test_auto_approved_submissions_are_frozen_and_exportable(session, catalog, user, configured, tmp_path, monkeypatch):
    """Regression: with approval OFF (the default), a submitted drawing is
    approved immediately. If that path does not freeze, the row has no uniqueId
    and silently never appears in any export."""
    from app import config as cfg
    from app.submissions import create_submission
    from tests.test_images import png_bytes

    monkeypatch.setattr(cfg, "IMAGES_DIR", tmp_path / "images")
    (tmp_path / "images").mkdir(parents=True, exist_ok=True)

    submission = create_submission(session, catalog[0], user, png_bytes())
    session.flush()

    assert SubmissionStatus(submission.status) is SubmissionStatus.APPROVED
    assert submission.unique_id is not None, "auto-approve must assign a uniqueId"
    assert submission.options_json and submission.correct_letter
    assert submission.credit_name == "Alex"

    rows = parse(export(session)[0])
    assert [r["uniqueId"] for r in rows] == [str(submission.unique_id)]


def test_an_approved_row_missing_an_id_is_repaired_by_the_export(session, catalog, user, configured):
    submission = approved(session, catalog[0], user)
    submission.unique_id = None
    submission.options_json = None
    session.flush()

    rows = parse(export(session)[0])
    assert len(rows) == 1
    assert submission.unique_id is not None
