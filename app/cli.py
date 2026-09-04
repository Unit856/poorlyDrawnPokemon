"""Admin CLI: python -m app.cli <command>."""

from __future__ import annotations

import argparse
import getpass
import logging
import random
import sys
from pathlib import Path

from sqlalchemy import func, select

from app import config
from app.catalog.seed import seed
from app.db import init_db, session_scope
from app import preflight
from app.export import ExportBlocked, export
from app.images import resolve_public_url
from app.models import (
    Pokemon,
    Role,
    Submission,
    SubmissionStatus,
    User,
    get_or_create_settings,
)
from app.picker import coverage, drawing_counts, min_tier, sequence
from app.slugs import SlugCollision
from app.users import SlugTaken, UsernameTaken, create_user


def cmd_initdb(_args: argparse.Namespace) -> int:
    init_db()
    with session_scope() as session:
        get_or_create_settings(session)
    print(f"initialised {config.DATABASE_URL}")
    print(f"data dir: {config.DATA_DIR}")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    init_db()
    with session_scope() as session:
        try:
            report = seed(session, limit=args.limit, use_cache=not args.no_cache)
        except SlugCollision as exc:
            print(f"SEED ABORTED: {exc}", file=sys.stderr)
            return 2
        print(report.summary())
        print(f"snapshot: {report.snapshot_label}")
    return 0


def cmd_create_admin(args: argparse.Namespace) -> int:
    init_db()
    password = args.password or getpass.getpass("password: ")
    with session_scope() as session:
        try:
            user = create_user(
                session,
                username=args.username,
                password=password,
                display_name=args.display_name,
                role=Role.ADMIN,
            )
        except (UsernameTaken, SlugTaken) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"created admin {user.username!r} (slug {user.slug!r}, credit {user.display_name!r})")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    init_db()
    with session_scope() as session:
        settings = get_or_create_settings(session)
        total = session.scalar(select(func.count()).select_from(Pokemon)) or 0
        enabled = (
            session.scalar(select(func.count()).select_from(Pokemon).where(Pokemon.enabled)) or 0
        )
        regional = (
            session.scalar(
                select(func.count()).select_from(Pokemon).where(Pokemon.region_key.is_not(None))
            )
            or 0
        )
        print(f"catalog rows   : {total} ({enabled} enabled, {regional} regional forms)")
        print(f"snapshot       : {settings.catalog_snapshot_label or '(never seeded)'}")
        print(f"public base url: {settings.public_base_url or '(unset)'}")
        print(f"require approval: {settings.require_approval}")
        print(f"next uniqueId  : {settings.next_unique_id}")
    return 0


def cmd_coverage(_args: argparse.Namespace) -> int:
    init_db()
    with session_scope() as session:
        counts = drawing_counts(session)
        if not counts:
            print("no enabled catalog rows; seed first")
            return 1
        print(f"answer units : {len(counts)}")
        print(f"min tier     : {len(min_tier(counts))} rows at {min(counts.values())} drawing(s)")
        print("drawings  answer units")
        for drawings, units in coverage(counts).items():
            print(f"{drawings:>8}  {units}")
    return 0


def cmd_simulate_picker(args: argparse.Namespace) -> int:
    """Verify the scope 6 weighting against the real catalog, writing nothing."""
    init_db()
    with session_scope() as session:
        counts = drawing_counts(session)
        if not counts:
            print("no enabled catalog rows; seed first")
            return 1

        units = len(counts)
        rng = random.Random(args.seed)
        picked = sequence(counts, args.draws, rng)

        distinct = len(set(picked))
        final = dict(counts)
        for pid in picked:
            final[pid] += 1
        histogram = coverage(final)

        print(f"answer units      : {units}")
        print(f"draws simulated   : {args.draws} (seed {args.seed})")
        print(f"distinct picked   : {distinct}")
        print(f"duplicates        : {args.draws - distinct}")
        print("resulting spread:")
        for drawings, n in histogram.items():
            print(f"  {drawings} drawing(s): {n} answer units")

        # The scope 6 guarantee: counts never differ by more than one within a
        # run that starts from a level pool.
        spread = max(final.values()) - min(final.values())
        print(f"max-min spread    : {spread}")
        if args.draws <= units and distinct != args.draws:
            print("FAIL: a repeat occurred before every answer unit had one drawing")
            return 2
        print("OK: min-tier ordering held")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    init_db()
    with session_scope() as session:
        try:
            payload, report = export(session)
        except ExportBlocked as exc:
            print(f"EXPORT BLOCKED: {exc}", file=sys.stderr)
            return 2

        if report.rows == 0:
            print("no approved drawings to export")
            return 1

        Path(args.out).write_bytes(payload)
        print(f"wrote {report.rows} rows to {args.out} ({len(payload)} bytes)")
        if len(report.packs) > 1:
            for index, count in report.packs.items():
                print(f"  pack {index}: {count} rows")
        if report.warning:
            print(f"WARNING: {report.warning}", file=sys.stderr)
    return 0


def cmd_rebase_urls(args: argparse.Namespace) -> int:
    """Repoint already-frozen imageURLs at a new base (scope 13).

    Normally imageURL is frozen at approval and never rewritten, because
    re-export has to be byte-stable for rows players have already seen. The
    scope qualifies that with "once the first pack is published" -- so this
    exists for the window *before* publication, when the base URL was simply
    wrong and the alternative is unapprove/re-approve churning every uniqueId.

    Only the URL moves. uniqueId, options, correct letter and credit are all
    left alone, so the questions themselves are unchanged.
    """
    init_db()
    with session_scope() as session:
        settings = get_or_create_settings(session)

        base = (args.to or settings.public_base_url or "").strip().rstrip("/")
        if not base:
            print(
                "no target base URL: pass --to, or set one with 'set-url' first",
                file=sys.stderr,
            )
            return 2
        if not base.startswith(("http://", "https://")):
            print("URL must start with http:// or https://", file=sys.stderr)
            return 2

        rows = session.scalars(
            select(Submission).where(Submission.public_url.is_not(None))
        ).all()
        changes = [
            (s, s.public_url, resolve_public_url(base, s.file_path))
            for s in rows
            if s.public_url != resolve_public_url(base, s.file_path)
        ]

        if not changes:
            print(f"nothing to do: every frozen URL already uses {base}")
            return 0

        print(f"{len(changes)} frozen URL(s) would change to base {base}:")
        for submission, old, new in changes[:10]:
            print(f"  #{submission.unique_id}  {old}")
            print(f"           -> {new}")
        if len(changes) > 10:
            print(f"  ... and {len(changes) - 10} more")

        if not args.yes:
            print()
            print("dry run. Re-run with --yes to apply.")
            print(
                "Only do this BEFORE the pack is uploaded to the Workshop. If players "
                "already have it, re-exporting after this changes imageURL on questions "
                "they have seen, which is exactly what freezing exists to prevent.",
                file=sys.stderr,
            )
            return 0

        for submission, _old, new in changes:
            submission.public_url = new
            session.add(submission)
        if args.to:
            settings.public_base_url = base
            session.add(settings)

        print(f"rewrote {len(changes)} URL(s). Re-export to pick them up.")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """Show every submission and whether it would reach the CSV.

    Diagnostic for "my pack is missing drawings". The export filters on status
    alone, so anything absent is absent because of its status or its freeze
    state -- this prints both.
    """
    init_db()
    with session_scope() as session:
        rows = session.execute(
            select(Submission, Pokemon, User)
            .join(Pokemon, Submission.pokemon_id == Pokemon.id)
            .join(User, Submission.user_id == User.id)
            .order_by(Submission.id)
        ).all()

        if not rows:
            print("no submissions at all")
            return 0

        by_artist: dict[str, dict[str, int]] = {}
        exported = 0

        print(f"{'id':>4} {'uid':>5}  {'artist':<14} {'pokemon':<22} {'status':<9} exported?")
        print("-" * 74)
        for submission, pokemon, user in rows:
            status = SubmissionStatus(submission.status).value
            will_export = status == SubmissionStatus.APPROVED.value
            exported += will_export
            by_artist.setdefault(user.username, {}).setdefault(status, 0)
            by_artist[user.username][status] += 1
            print(
                f"{submission.id:>4} {str(submission.unique_id or '-'):>5}  "
                f"{user.username:<14} {pokemon.display_name:<22} {status:<9} "
                f"{'yes' if will_export else 'NO'}"
            )

        print()
        print("by artist:")
        for username, counts in sorted(by_artist.items()):
            breakdown = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            print(f"  {username:<14} {breakdown}")

        print()
        print(f"would appear in the CSV: {exported} of {len(rows)}")
        if exported != len(rows):
            print(
                "Anything marked NO is pending, rejected or deleted. "
                "Approve it in Admin > Queue, then export again."
            )
    return 0


def cmd_set_display_name(args: argparse.Namespace) -> int:
    """Change the credit a player's *future* questions are published under.

    Deliberately does not touch anything already approved: `credit_name` is
    snapshotted at approval and `slug` was frozen at account creation, so
    published rows and existing filenames keep the old name (scope 8.1, 10.2).
    """
    new_name = args.display_name.strip()
    if not new_name:
        print("display name must not be empty", file=sys.stderr)
        return 2
    if len(new_name) > 32:
        print("display name must be 32 characters or fewer", file=sys.stderr)
        return 2

    init_db()
    with session_scope() as session:
        user = session.scalar(select(User).where(User.username == args.username))
        if user is None:
            print(f"no user named {args.username!r}", file=sys.stderr)
            return 2

        previous = user.display_name
        user.display_name = new_name
        session.add(user)

        frozen = session.scalar(
            select(func.count())
            .select_from(Submission)
            .where(Submission.user_id == user.id, Submission.credit_name.is_not(None))
        ) or 0
        slug = user.slug

    print(f"{args.username}: display name {previous!r} -> {new_name!r}")
    print(f"artist slug stays {slug!r} (it is in every filename this account has written)")
    if frozen:
        print(
            f"NOTE: {frozen} already-approved drawing(s) keep the credit {previous!r}. "
            "Only questions approved from now on use the new name.",
            file=sys.stderr,
        )
    return 0


def cmd_set_url(args: argparse.Namespace) -> int:
    """Set public_base_url without needing the web UI (scope 13).

    This is the base every exported imageURL is built from, so it must be the
    address *players* reach — not how the box reaches itself.
    """
    url = args.url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        print("URL must start with http:// or https://", file=sys.stderr)
        return 2

    init_db()
    with session_scope() as session:
        settings = get_or_create_settings(session)
        previous = settings.public_base_url
        settings.public_base_url = url
        session.add(settings)

        frozen = session.scalar(
            select(func.count())
            .select_from(Submission)
            .where(Submission.public_url.is_not(None))
        ) or 0

    print(f"public_base_url: {previous or '(unset)'} -> {url}")
    if frozen:
        # Scope 10.2: imageURL is frozen per row at approval and is never
        # rewritten, so this only affects rows approved from now on.
        print(
            f"NOTE: {frozen} already-approved drawing(s) keep the URL they were "
            "frozen with. Changing this does not rewrite them.",
            file=sys.stderr,
        )
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    """Scope 13: verify the deployment before handing a pack to friends."""
    init_db()
    with session_scope() as session:
        report = preflight.run(session, skip_network=args.offline)
        print(report.summary())
        if report.failed:
            return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(prog="app.cli", description="Who's That Pokemon admin CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("initdb", help="create tables and the settings row").set_defaults(
        func=cmd_initdb
    )

    p_seed = sub.add_parser("seed", help="seed or re-seed the catalog from PokeAPI")
    p_seed.add_argument("--limit", type=int, default=None, help="only fetch the first N species")
    p_seed.add_argument("--no-cache", action="store_true", help="ignore the on-disk response cache")
    p_seed.set_defaults(func=cmd_seed)

    p_admin = sub.add_parser("create-admin", help="create the first Admin account")
    p_admin.add_argument("username")
    p_admin.add_argument("--password", default=None, help="omit to be prompted")
    p_admin.add_argument("--display-name", default=None)
    p_admin.set_defaults(func=cmd_create_admin)

    sub.add_parser("status", help="show catalog and settings state").set_defaults(func=cmd_status)

    sub.add_parser("coverage", help="histogram of drawings per answer unit").set_defaults(
        func=cmd_coverage
    )

    p_sim = sub.add_parser(
        "simulate-picker", help="dry-run the weighted picker against the real catalog"
    )
    p_sim.add_argument("--draws", type=int, default=1200)
    p_sim.add_argument("--seed", type=int, default=0)
    p_sim.set_defaults(func=cmd_simulate_picker)

    sub.add_parser(
        "audit", help="list every submission and whether it would reach the CSV"
    ).set_defaults(func=cmd_audit)

    p_rebase = sub.add_parser(
        "rebase-urls",
        help="repoint already-frozen imageURLs at a new base (pre-publication only)",
    )
    p_rebase.add_argument("--to", default=None, help="new base URL; also updates the setting")
    p_rebase.add_argument("--yes", action="store_true", help="apply (default is a dry run)")
    p_rebase.set_defaults(func=cmd_rebase_urls)

    p_name = sub.add_parser(
        "set-display-name", help="change the credit a player's future questions use"
    )
    p_name.add_argument("username")
    p_name.add_argument("display_name")
    p_name.set_defaults(func=cmd_set_display_name)

    p_url = sub.add_parser("set-url", help="set the public base URL players will reach")
    p_url.add_argument("url", help="e.g. https://pokedraw.example.com")
    p_url.set_defaults(func=cmd_set_url)

    p_pre = sub.add_parser(
        "preflight", help="check the public deployment the way a Steam client sees it"
    )
    p_pre.add_argument("--offline", action="store_true", help="skip the network fetches")
    p_pre.set_defaults(func=cmd_preflight)

    p_export = sub.add_parser("export", help="write the Trivia Tricks CSV")
    p_export.add_argument("--out", default="WhosThatPokemon.csv")
    p_export.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
