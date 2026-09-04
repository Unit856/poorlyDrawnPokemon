# Who's That Pokémon

Draw-from-memory web tool that produces Trivia Tricks Workshop picture questions.
Implements `Whos-That-Pokemon-Scope-v1.1.docx`, which is the authoritative spec —
section references in the code (`scope 8.1`, `scope 5.4`, …) point back into it.

## Stack

FastAPI + SQLite (SQLAlchemy 2.0) + server-rendered Jinja + vanilla-JS canvas, in
one container. The scope explicitly does not care about the stack (§16); this one
was chosen because the whole product is one box, one process, one volume.

## Build order

Slices follow scope §17. Slice 1 (catalog, data model, slugs, Docker layout) is
complete; the draw flow, approval and export land in later slices.

| Slice | Scope | Status |
|-------|-------|--------|
| 1. Catalog seed, data model, slugs, volume layout | §5, §8.1, §11 | done |
| 2. Auth, admin user-create, password reset | §3, §12 | done |
| 3. Weighted picker + hints | §6, §7.1 | done |
| 4. Canvas, PNG write, /images | §7.2–7.4, §8 | done |
| 5. My Drawings, approval queue | §7.5, §9 | done |
| 6. CSV export | §10 | done |
| 7. TLS, public_base_url, hotlink test | §13 | done — two human checks remain |

## Local development

```bash
python -m pip install -r requirements-dev.txt
export WTP_DATA_DIR=./data          # PowerShell: $env:WTP_DATA_DIR = "./data"

python -m app.cli initdb
python -m app.cli seed              # ~2,100 requests, a few minutes, then cached
python -m app.cli create-admin drake
python -m app.cli status

python -m pytest tests -q
```

Requires SQLAlchemy ≥ 2.0.40 on Python 3.13+; earlier versions fail to map
`list[str] | None` annotations. The container pins Python 3.12.

## Deploy

Full instructions: **[docs/DEPLOY.md](docs/DEPLOY.md)**.

```bash
cp .env.example .env && $EDITOR .env      # set WTP_HOSTNAME

docker compose --profile caddy up -d --build   # bundled TLS, or:
docker compose up -d --build                   # existing host proxy → 127.0.0.1:8000

docker compose exec app python -m app.cli seed
docker compose exec app python -m app.cli create-admin <name>
docker compose exec app python -m app.cli preflight
```

Caddy is behind a profile, not assumed: this box already runs other game
services, so ports 80/443 may be taken. `docs/DEPLOY.md` has the nginx server
block for that case — note that it **must** forward `X-Forwarded-Proto` (or the
Secure cookie flag is lost) and `X-Forwarded-For` (or every player shares one
rate-limit bucket).

Set `public_base_url` before the first export and treat it as permanent — it is
frozen into every published question URL (§13).

### Preflight

`python -m app.cli preflight` (or **Admin → Preflight**) fetches the app's own
published URL over the network, the way a Steam client does, and checks the §8.2
contract: 200, `.png` suffix, `image/png`, immutable caching, CORS, and that the
body really decodes as an 800×800 PNG rather than a login page served with a 200.

It fails loudly on loopback and RFC1918 addresses, because the LAN-only
deployment is the quiet killer: it previews perfectly in a browser on your
network and fails for every remote friend inside the game (§4). Exit code is
non-zero on failure, so it drops into a deploy script or cron.

It cannot play a Trivia Tricks match. Acceptance criteria 9 and 11 stay human.

### Backups

```bash
./scripts/backup.sh /srv/backups
```

Excludes the live `wtp.sqlite3` and archives a consistent snapshot taken through
the sqlite3 backup API instead — SQLite runs in WAL mode, and shipping both the
torn live copy and a good snapshot would let a restore silently prefer the wrong
one. Also excludes the PokéAPI cache, which is re-fetchable.

A partial restore is worse than none: filenames and uniqueIds are permanent, so a
database restored without its images yields questions pointing at 404s. Restore
the volume as a unit; the script prints the exact command.

## Catalog

Seeded from PokéAPI on explicit admin action only — never on app start (§5.5).
A full seed produces **1,082 answer units**: 1,025 National Dex species plus 57
regional forms (18 Alolan, 19 Galarian, 16 Hisuian, 4 Paldean including the three
Tauros breeds).

Two things about the seeder worth knowing:

- **PokéAPI returns 403 without a `User-Agent`.** `config.USER_AGENT` is required,
  not politeness.
- **Regional forms need the hardcoded overrides in `app/catalog/overrides.py`.**
  Upstream hangs generation, Pokédex category and flavor text off the *species*,
  which a base form shares with its regional forms. Without the override table
  Alolan Vulpix would report Generation 1 and Kanto flavor text. The Pokédex
  category is genuinely unavailable per-form and is inherited by design (§5.4) —
  Paldean Wooper reads "Water Fish Pokémon".

Forms excluded as answers (§5.1): Mega, Gigantamax, Totem, cap/costume Pikachu,
and non-regional alternate forms, which collapse into their default variety.
Galarian Darmanitan's Standard mode is the canonical form; Zen mode is dropped.

## Auth

Username + password (Argon2id), an HTTP-only `SameSite=Lax` session cookie, and
no public registration (§12). The first Admin is created with the CLI; everyone
else is created by an Admin. Password reset is an Admin setting a new password —
no email flow, no reset tokens.

- The session signing key lives at `/data/secret_key`, generated on first boot,
  so sessions survive `compose up` (§14). Override with `WTP_SECRET_KEY`.
- The `Secure` cookie flag follows `X-Forwarded-Proto`, since behind Caddy the
  app itself speaks plain HTTP. Force it with `WTP_SECURE_COOKIES=1`.
- Login is rate limited to 10 attempts per 5 minutes per username+IP, cleared on
  success. The limiter is in-process: it resets on restart and would not work
  across multiple workers. Fine for one container and 5–8 friends; wrong the
  moment this is scaled out.
- There is no CSRF token. `SameSite=Lax` is what stops cross-site form posts,
  which is the level of hardening §12 asks for.

**Templates never hold ORM objects.** Route handlers pass `auth.UserView`, a
frozen snapshot. Error pages render *after* the request's session has been rolled
back and closed, so a mapped instance reaching the layout raises
`DetachedInstanceError` and turns a tidy 403 into a 500. `tests/test_session_lifetime.py`
uses production session lifetime specifically to catch that class of bug — the
ordinary tests hold one session open and cannot see it.

## Picker

Min-tier selection (§6): restrict to the answer units with the lowest
`drawing_count`, then pick uniformly. Every unit gets one drawing before any gets
a second.

Counting rules — pending, approved and **rejected** all count; only deleted
releases a row back to the picker. Rejected counting is what stops rejection
being a re-roll loophole.

Verify the weighting without writing anything:

```bash
python -m app.cli coverage                          # drawings per answer unit
python -m app.cli simulate-picker --draws 1082      # dry run over the real catalog
```

Against the seeded catalog, 1,082 draws produce 1,082 distinct picks with zero
duplicates; the 1,083rd is the first second-drawing.

**No reservation is taken at assignment** (decision A1). Two players starting at
the same moment can be handed the same Pokémon and both drawings are kept.

**A player has at most one open assignment**, enforced by a partial unique index
(`uq_open_draw_session`). Resume (§7.1) is only well-defined if there is exactly
one assignment to resume. `assign()` catches the race on a savepoint and resumes
the winner, so double-clicking Draw is a no-op rather than a 500.

There is no migration framework. Indexes added after a table already shipped live
in `db._RETROFIT_DDL` as idempotent `CREATE INDEX IF NOT EXISTS` statements —
`create_all` skips existing tables, so a plain declarative index would never
reach a database created by an earlier slice.

## Canvas and images

The canvas backing store is always 800×800 regardless of display size, so the
exported PNG matches §7.2 on any window. The background stays transparent and the
eraser paints transparency (`destination-out`) rather than white.

- **Undo history is stored as compressed data URLs, not `ImageData`.** Thirty
  frames of raw 800×800 RGBA would be ~77 MB; line art compresses to a few
  hundred KB per frame.
- **Flood fill** is 4-connected with a fixed ±32/255 per-channel tolerance on a
  rasterised snapshot. Leaks through antialiased stroke edges are a known,
  accepted limitation.
- **Uploads are decoded and re-encoded, never passed through.** The canvas is
  client-side and the result is served publicly — "the browser sent it" is not a
  guarantee. Re-encoding pins the dimensions, strips stray chunks, and means we
  serve bytes we produced.
- **Files are written once and never overwritten** (atomic temp-file + rename, so
  a crash cannot leave a truncated PNG at a permanent URL), and an `index` is
  never reused for a (pokemon, artist) pair even after a delete.
- `/images/{name}` is a hand-written route rather than a `StaticFiles` mount,
  because the §8.2 headers are part of the contract. The filename must match the
  slug-triple regex exactly, which is also what blocks traversal.

### Emptiness has two rules, deliberately

§7.3 defines empty as **zero committed strokes**, which is a browser-side concept
the server cannot verify. The server additionally refuses a PNG with no
non-transparent pixels. These agree except in one case: strokes drawn and then
fully erased are non-empty by the scope's rule but rejected by the server's. The
server wins, because publishing a blank image to a permanent immutable URL is
worse than a confusing error.

## Moderation and the uniqueId lifecycle

`app/moderation.py` owns the whole state machine, because approval is what
creates and destroys a uniqueId.

| Action | Status | uniqueId | Counts toward picker? | In artist's gallery? |
|---|---|---|---|---|
| approve | approved | assigned (first time) | yes | yes |
| unapprove | pending | **retired** | yes | yes |
| reject | rejected | retired if it had one | **yes** | yes |
| unreject | pending / approved | new on approve | yes | yes |
| delete (admin only) | deleted | **retired** | **no** | no |

Two consequences worth internalising:

- **Reject and delete differ on purpose.** Rejecting still counts toward
  `drawing_count`, so it is not a way to get the same Pokémon dealt again.
  Deleting releases the answer unit back to the picker — which is exactly why
  delete is Admin-only.
- **Unapprove is not a reversible toggle** (decision A5). It retires the uniqueId
  *and clears the whole frozen payload*. Re-approving mints a new question with a
  new number; the old one is gone from the pack. The queue UI says this out loud
  in the flash message, because it is surprising.

Retirement is enforced by construction: `Settings.next_unique_id` only ever
increases, so a retired number can never be handed out again.

### Freezing

`moderation.freeze()` writes `unique_id`, `options_json`, `correct_letter`,
`credit_name` and `public_url` at first approval, exactly once, never rewritten.

**Auto-approve is first approval and must freeze.** With approval off — the
default — a submitted drawing is approved immediately, so `create_submission`
calls `freeze()` directly. Skipping that leaves an approved row with no uniqueId,
which is invisible to an export that only reads frozen columns; every drawing
would silently never reach a pack.

`public_url` is the one field allowed to start empty: auto-approve can fire before
an Admin has ever opened the settings page, and blocking approval on a setting
would break submitting outright. The export backfills it once, and
`export.ensure_frozen` will also repair an approved row missing an id or options
rather than dropping it.

## Export

`GET /admin/export` previews, `POST /admin/export` downloads. CLI equivalent:

```bash
python -m app.cli export --out WhosThatPokemon.csv
```

The format was validated against `triviaTricksExampleCSVFile.csv`, the official
example shipped with the game: identical header, UTF-8 **BOM**, CRLF. All three
match, which independently confirms decision B9.

**Export is a pure read of frozen columns.** It never regenerates options,
re-reads the artist's current display name, or reshuffles. Consequences:

- Re-export is byte-identical for every surviving row; a row changes only by
  disappearing.
- A distractor whose catalog row is later disabled or renamed stays in the frozen
  question — it is still a real English Pokémon name and a fine wrong answer.
- Retired uniqueIds never come back and are never handed to a new row.

`tests/test_export.py::test_re_export_stability_under_mutation` is acceptance
criterion 10 in full: export, unapprove a row, rename the artist, add a drawing,
disable a distractor, re-export, assert survivors are byte-identical.

The §10.4 split rule (`pack_index`) partitions by uniqueId **range**, not list
position, so adopting it later cannot renumber anything. v1 still emits one file
and warns above 2,800 rows.

## Invariants

Two rules are load-bearing across the whole app:

1. **Slugs and per-pair indexes are permanent.** They appear in filenames served
   `immutable` and never overwritten. A slug collision is a hard seed failure,
   never auto-suffixed — that is what `app/slugs.assert_unique` is for.
2. **Anything written at first approval is written once.** `unique_id`,
   `options_json`, `correct_letter`, `credit_name`, `public_url`. Export is a pure
   read of those columns, which is what makes re-export byte-stable (§10.2).
