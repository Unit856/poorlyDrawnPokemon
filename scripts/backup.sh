#!/usr/bin/env bash
# Back up the /data volume: database + images + session key.
#
# Scope 13: "Back up the /data volume. Images plus the DB are the entire product
# state." Losing it loses every drawing and every frozen question, and because
# uniqueIds and filenames are permanent, a partial restore is worse than none.
#
#   ./scripts/backup.sh /srv/backups
#
# Two deliberate choices:
#
#  * The live wtp.sqlite3 (and its -wal/-shm) is EXCLUDED. SQLite runs in WAL
#    mode, so copying those files while the app is writing can capture a torn
#    state. A consistent snapshot is taken through the sqlite3 backup API and
#    archived as wtp-snapshot.sqlite3 instead. Shipping both would be worse than
#    shipping neither: a restore would silently prefer the torn one.
#
#  * The PokeAPI response cache is EXCLUDED. It is large and entirely
#    re-fetchable with `python -m app.cli seed`.

set -euo pipefail

DEST="${1:-./backups}"
VOLUME="${WTP_VOLUME:-poorlydrawnpokemon_wtp-data}"
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="wtp-data-${STAMP}.tar.gz"
KEEP="${KEEP:-14}"

mkdir -p "$DEST"

if ! docker volume inspect "$VOLUME" >/dev/null 2>&1; then
  echo "volume '$VOLUME' not found. Set WTP_VOLUME to the right name:" >&2
  docker volume ls --format '  {{.Name}}' >&2
  exit 1
fi

echo "backing up volume '$VOLUME' -> ${DEST}/${ARCHIVE}"

docker run --rm -v "${VOLUME}:/data" python:3.12-slim python - <<'PY'
import sqlite3
from pathlib import Path

source = Path("/data/wtp.sqlite3")
target = Path("/data/wtp-snapshot.sqlite3")
target.unlink(missing_ok=True)

if source.exists():
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    print(f"consistent snapshot: {target.stat().st_size} bytes")
else:
    print("no database yet; archiving files only")
PY

docker run --rm \
  -v "${VOLUME}:/data:ro" \
  -v "$(cd "$DEST" && pwd):/backup" \
  alpine tar czf "/backup/${ARCHIVE}" \
    --exclude='./wtp.sqlite3' \
    --exclude='./wtp.sqlite3-wal' \
    --exclude='./wtp.sqlite3-shm' \
    --exclude='./cache' \
    -C /data .

docker run --rm -v "${VOLUME}:/data" alpine rm -f /data/wtp-snapshot.sqlite3

echo "wrote ${DEST}/${ARCHIVE} ($(du -h "${DEST}/${ARCHIVE}" | cut -f1))"
echo
echo "restore into a fresh volume with:"
echo "  docker run --rm -v ${VOLUME}:/data -v \"\$PWD:/backup\" alpine sh -c \\"
echo "    'tar xzf /backup/${ARCHIVE} -C /data && mv /data/wtp-snapshot.sqlite3 /data/wtp.sqlite3'"

# Prune old archives, keeping the most recent $KEEP.
ls -1t "${DEST}"/wtp-data-*.tar.gz 2>/dev/null | tail -n "+$((KEEP + 1))" | while read -r old; do
  echo "pruning $old"
  rm -f "$old"
done
