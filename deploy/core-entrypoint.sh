#!/bin/sh
# ADR-0001: Core is the configuration authority, so its per-site config must
# survive restarts. The image ships defaults in /app/configs; on first run we
# seed the persistent volume (CONFIG_DIR) from those defaults, then run Core.
# The data model stays baked (versioned artifact) and is not seeded here.
set -e
SEED_DIR=/app/configs
DATA_DIR="${CONFIG_DIR:-/data/config}"
mkdir -p "$DATA_DIR"
if [ -z "$(ls -A "$DATA_DIR" 2>/dev/null)" ]; then
  echo "core-entrypoint: seeding config volume $DATA_DIR from $SEED_DIR"
  cp -a "$SEED_DIR"/. "$DATA_DIR"/
fi
exec "$@"
