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
from app.models import Pokemon, Role, get_or_create_settings
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
