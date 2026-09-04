"""Trivia Tricks CSV export, scope 10.

The load-bearing rule: **export is a pure read of frozen columns.** Options, the
correct letter, the credit and the URL are decided once, at approval, and written
to the Submission row. Nothing here regenerates them, re-reads the artist's
current display name, or reshuffles. That is what makes re-export byte-stable for
every surviving row -- a row can change only by disappearing.

If a distractor's catalog row is later disabled or renamed, the frozen question is
left exactly as it is. A disabled row is still a real English Pokemon name and
remains a perfectly good wrong answer.
"""

from __future__ import annotations

import csv
import io
import logging
import random
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.images import resolve_public_url
from app.models import (
    Pokemon,
    Submission,
    SubmissionStatus,
    User,
    allocate_unique_id,
    get_or_create_settings,
)

log = logging.getLogger(__name__)

#: Scope 16: no accent, per request. Changing this after a pack ships alters
#: every existing question row.
QUESTION_TEXT = "Who's that Pokemon?"

#: Scope 10.3. Order and spelling are exact; Trivia Tricks parses on these names.
CSV_HEADER = (
    "uniqueId",
    "question",
    "optionA",
    "optionB",
    "optionC",
    "optionD",
    "fixedOrder",
    "correctAnswer",
    "credit",
    "imageURL",
    "answerExplanation",
)

OPTION_COUNT = 4
LETTERS = "ABCD"

#: Scope 10.2: an empty credit would become "Player Created" in-game.
FALLBACK_CREDIT = "Player Created"


class NotEnoughCatalog(RuntimeError):
    """Fewer than four enabled answer units, so a 4-option question is impossible."""


class ExportBlocked(RuntimeError):
    """The export cannot produce a valid pack."""


@dataclass(frozen=True)
class ExportReport:
    rows: int
    over_warn_threshold: bool
    packs: dict[int, int]
    missing_public_url: int

    @property
    def warning(self) -> str | None:
        if self.over_warn_threshold:
            return (
                f"{self.rows} rows: approaching the ~3,000-question Workshop cap. "
                f"Split at {config.PACK_CHUNK_SIZE} per pack before you exceed it."
            )
        return None


def enabled_names(db: Session) -> list[str]:
    return list(
        db.scalars(
            select(Pokemon.display_name).where(Pokemon.enabled.is_(True)).order_by(Pokemon.id)
        ).all()
    )


def build_options(
    pool: list[str], correct_name: str, rng: random.Random | None = None
) -> tuple[list[str], str]:
    """Pick three distractors and place the real name (scope 10.2).

    Uniform random from the enabled catalog at this moment; never equal to the
    correct name, never duplicated. Regional forms of the same species (Vulpix vs
    Alolan Vulpix) are deliberately allowed as distractors.
    """
    rng = rng or random.Random()
    candidates = sorted({name for name in pool if name != correct_name})
    if len(candidates) < OPTION_COUNT - 1:
        raise NotEnoughCatalog(
            f"need {OPTION_COUNT - 1} distractors for {correct_name!r}, "
            f"only {len(candidates)} other enabled answer units exist"
        )

    options = rng.sample(candidates, OPTION_COUNT - 1)
    # Random placement so a pack dump is not "always A". fixedOrder stays FALSE,
    # so the game may shuffle again at runtime anyway.
    slot = rng.randrange(OPTION_COUNT)
    options.insert(slot, correct_name)
    return options, LETTERS[slot]


def ensure_frozen(
    db: Session,
    submission: Submission,
    *,
    pool: list[str] | None = None,
    rng: random.Random | None = None,
) -> bool:
    """Complete any frozen field that approval could not fill in.

    Approval freezes everything it can. Two fields can legitimately be missing:

    * `public_url`, when the drawing was auto-approved before an Admin had ever
      set the public base URL;
    * `options_json`, if the catalog had fewer than four enabled rows at approval.

    They are backfilled here, once, and are frozen from then on. Returns True if
    anything was written.
    """
    changed = False

    if submission.unique_id is None:
        # Belt and braces: an approved row without an id would silently vanish
        # from every export, so repair it rather than drop it.
        submission.unique_id = allocate_unique_id(db)
        changed = True

    if not submission.options_json:
        pokemon = db.get(Pokemon, submission.pokemon_id)
        names = pool if pool is not None else enabled_names(db)
        # The correct answer must be present even if its own row was disabled.
        if pokemon.display_name not in names:
            names = names + [pokemon.display_name]
        options, letter = build_options(names, pokemon.display_name, rng)
        submission.options_json = options
        submission.correct_letter = letter
        changed = True

    if not submission.credit_name:
        artist = db.get(User, submission.user_id)
        submission.credit_name = artist.display_name if artist else FALLBACK_CREDIT
        changed = True

    if not submission.public_url:
        base = get_or_create_settings(db).public_base_url
        if not base:
            raise ExportBlocked(
                "The public base URL is not set. Trivia Tricks downloads these images "
                "directly, so every imageURL would be unusable. Set it in Settings first."
            )
        submission.public_url = resolve_public_url(base, submission.file_path)
        changed = True

    if changed:
        db.add(submission)
    return changed


def approved_submissions(db: Session) -> list[Submission]:
    """Every currently approved drawing, in uniqueId order (scope 10.1)."""
    return list(
        db.scalars(
            select(Submission)
            .where(Submission.status == SubmissionStatus.APPROVED)
            .order_by(Submission.unique_id)
        ).all()
    )


def pack_index(unique_id: int) -> int:
    """Scope 10.4, reserved split rule.

    Partitions by uniqueId *range*, not by position in the sorted list. Because
    uniqueIds are stable and never reused, a question can never migrate between
    packs; retired ids just leave a pack slightly smaller.
    """
    return (unique_id - 1) // config.PACK_CHUNK_SIZE + 1


def row_for(submission: Submission) -> dict[str, object]:
    options = list(submission.options_json or [])
    if len(options) != OPTION_COUNT:  # pragma: no cover - ensure_frozen guarantees this
        raise ExportBlocked(f"submission {submission.id} has {len(options)} options, expected 4")

    return {
        "uniqueId": submission.unique_id,
        "question": QUESTION_TEXT,
        "optionA": options[0],
        "optionB": options[1],
        "optionC": options[2],
        "optionD": options[3],
        "fixedOrder": "FALSE",
        "correctAnswer": submission.correct_letter,
        "credit": submission.credit_name or FALLBACK_CREDIT,
        "imageURL": submission.public_url,
        "answerExplanation": "",
    }


def build_rows(db: Session, *, rng: random.Random | None = None) -> list[dict[str, object]]:
    submissions = approved_submissions(db)
    pool = enabled_names(db)
    for submission in submissions:
        ensure_frozen(db, submission, pool=pool, rng=rng)
    db.flush()
    return [row_for(s) for s in submissions]


def write_csv(rows: list[dict[str, object]]) -> bytes:
    """Scope 10.3: UTF-8 with BOM, CRLF, RFC 4180 quoting.

    The BOM is what makes Nidoran-female render correctly when an Admin opens the
    file in Excel on Windows; LibreOffice and Ganymede's Lab both tolerate it.
    """
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=CSV_HEADER,
        lineterminator="\r\n",
        quoting=csv.QUOTE_MINIMAL,
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8-sig")


def export(db: Session, *, rng: random.Random | None = None) -> tuple[bytes, ExportReport]:
    rows = build_rows(db, rng=rng)

    packs: dict[int, int] = {}
    for row in rows:
        index = pack_index(int(row["uniqueId"]))
        packs[index] = packs.get(index, 0) + 1

    report = ExportReport(
        rows=len(rows),
        over_warn_threshold=len(rows) > config.PACK_ROW_WARN_THRESHOLD,
        packs=dict(sorted(packs.items())),
        missing_public_url=sum(1 for row in rows if not row["imageURL"]),
    )
    return write_csv(rows), report
