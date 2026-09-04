# Deploying Who's That Pokémon

Target: the existing Ubuntu game-server box, one Docker Compose stack, one volume
(scope §13). Resource budget is tiny — 5–8 concurrent canvases and a few thousand
PNGs of 100–400 KB each.

## The one thing that will bite you

Trivia Tricks clients **download `imageURL` themselves**, from wherever each
player is sitting. A LAN-only deployment previews perfectly in a browser on your
network and fails for every remote friend inside the game (§4).

So the image paths must be reachable from the public internet over a stable
hostname, and `public_base_url` must be set to that hostname **before you export a
pack** — it is frozen into every published question URL at approval and is not
rewritten afterwards (§13).

`python -m app.cli preflight` exists to catch exactly this.

---

## 1. Choose a hostname

Trivia Tricks shows the image host domain to every player before a match (§8.3).

- Use a boring, dedicated subdomain: `pokedraw.example.com`.
- Not a raw IP — players see it, and an IP cannot move.
- No secrets in the path.
- Point an A/AAAA record at the box before continuing.

Treat it as permanent once the first pack is published.

## 2. Bring the stack up

```bash
git clone <repo> /srv/whos-that-pokemon
cd /srv/whos-that-pokemon
cp .env.example .env
$EDITOR .env            # set WTP_HOSTNAME
```

### Option A — bundled Caddy (automatic TLS)

Use this if ports 80 and 443 are free on the box.

```bash
docker compose --profile caddy up -d --build
```

Caddy obtains and renews the certificate itself. Certificates live in the
`caddy-data` volume; keep it, or repeated re-issues will hit rate limits.

### Option B — an existing reverse proxy

The app binds `127.0.0.1:8000` by default, so point your existing nginx at it:

```nginx
server {
    listen 443 ssl http2;
    server_name pokedraw.example.com;

    ssl_certificate     /etc/letsencrypt/live/pokedraw.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/pokedraw.example.com/privkey.pem;

    # A drawing is ~100-400 KB; this is headroom, not a target.
    client_max_body_size 8m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        # Required: the Secure cookie flag and the login rate limiter both read
        # these. Without the proto header every session cookie is sent without
        # Secure; without the real IP every player shares one rate-limit bucket.
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /images/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        # Do NOT add your own Cache-Control or CORS headers here. The app sets
        # `immutable` caching and `Access-Control-Allow-Origin: *` deliberately
        # (scope 8.2); overriding them is a silent regression.
        proxy_hide_header X-Powered-By;
    }
}
```

```bash
docker compose up -d --build
```

## 3. Seed and bootstrap

```bash
docker compose exec app python -m app.cli seed          # ~2,100 requests, a few minutes
docker compose exec app python -m app.cli create-admin <name>
docker compose exec app python -m app.cli status
```

Seeding is an explicit action and never happens on app start (§5.5).

## 4. Configure

Sign in and open **Settings**:

- **Public base URL** → `https://pokedraw.example.com`
- **Default timer** → taste
- **Require approval** → off by default; on means nothing exports until reviewed

## 5. Preflight

```bash
docker compose exec app python -m app.cli preflight
```

Or **Admin → Preflight → Run the live network checks** in the UI. It fetches your
own published URL over the network the way a Steam client would, and checks:

| Check | Why |
|---|---|
| base URL set, HTTPS, real hostname | §8.3; a raw IP or private address is the LAN-only trap |
| not loopback/RFC1918 | fails loudly — this is the quiet killer |
| `/healthz` reachable | the proxy is actually wired up |
| image fetch returns 200 | the hotlink path works from outside |
| URL ends `.png` | Trivia Tricks validates this |
| `Content-Type: image/png` | ditto |
| `Cache-Control: immutable` | a proxy rewriting it is worth knowing about |
| `Access-Control-Allow-Origin: *` | belt-and-braces (§8.2) |
| body decodes as an 800×800 PNG | catches a login/error page served with a 200 |

Exit code is non-zero if anything fails, so it works in a cron or a deploy script.

## 6. Export and upload

```bash
docker compose exec app python -m app.cli export --out /data/WhosThatPokemon.csv
docker compose cp app:/data/WhosThatPokemon.csv ./WhosThatPokemon.csv
```

Or **Admin → Export → Download**. Put the file in a folder named
`WhosThatPokemon` and upload it through Ganymede's Lab.

## 7. Two checks only a human can do

Preflight proves the file is fetchable and well-formed. It cannot play the game.

- [ ] **Acceptance criterion 9** — upload through Ganymede's Lab, confirm the CSV
      parses and the picture shows in preview.
- [ ] **Acceptance criterion 11** — play one question in a real Trivia Tricks
      match and confirm the drawing is legible against the in-game question frame.

That second one is the decision most likely to flip (§16). Drawings export with a
**transparent** background. If the game composites them badly, set the canvas
background to `#F7F7F7` in `app/config.py` (`CANVAS_FALLBACK_BACKGROUND`) and
redraw — already-published PNGs are immutable and will not change.

## 8. Backups

```bash
./scripts/backup.sh /srv/backups
```

The database and images are the entire product state (§13). The script takes a
consistent SQLite snapshot rather than copying WAL files mid-write, then archives
the volume and prunes to the most recent 14.

A partial restore is worse than none: filenames and uniqueIds are permanent, so a
database restored without its matching images yields questions pointing at 404s.
Always restore the volume as a unit.

Nightly:

```cron
17 4 * * * cd /srv/whos-that-pokemon && ./scripts/backup.sh /srv/backups >> /var/log/wtp-backup.log 2>&1
```

## Upgrades

```bash
git pull
docker compose up -d --build
```

State lives in the volume, so a rebuild keeps everything. There is no migration
framework; index additions are applied idempotently at start-up
(`db._RETROFIT_DDL`). A future change needing new columns will say so in its
release notes.

## Troubleshooting

**Images 404 for friends but work for you** — the classic. `preflight` fails the
reachability check. Your DNS or port forwarding is not public.

**Questions show a broken image in-game** — `public_base_url` was wrong when those
rows were approved. The URL is frozen per row; fixing the setting does not rewrite
published rows. Unapprove and re-approve to re-freeze, accepting that this mints
new uniqueIds (§9).

**Everyone logged out after a redeploy** — `/data/secret_key` was lost, meaning the
volume was recreated. Check you are not running with an anonymous volume.

**Login rate limit triggers for everyone at once** — the proxy is not sending
`X-Forwarded-For`, so all players share one bucket.
