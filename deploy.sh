#!/bin/bash
# Deploy MK Climate Explorer to Hetzner CX23
# Run from your Mac: bash deploy.sh
#
# Smart deploy — only restarts services when relevant files actually changed:
#   mk_api.py / requirements.txt changed  →  pip install (if needed) + restart Flask
#   static/ changed                        →  no restart needed
#   data/ changed                          →  no restart needed

set -e

SERVER="root@46.224.208.142"
APP_DIR="/opt/mk_climate"

echo "==> Syncing files (checking for changes)..."
ssh "$SERVER" "mkdir -p $APP_DIR/data $APP_DIR/static"

# Sync Python app + requirements — capture itemised output to detect changes
PY_CHANGES=$(rsync -az --checksum --itemize-changes \
  mk_api.py \
  requirements.txt \
  "$SERVER:$APP_DIR/")

# Sync static files (no restart needed for these)
rsync -az --checksum --progress static/ "$SERVER:$APP_DIR/static/"

# Sync data directory (no restart needed)
rsync -az --checksum --progress data/ "$SERVER:$APP_DIR/data/"

# Determine what changed
PY_CHANGED=false
REQS_CHANGED=false

if echo "$PY_CHANGES" | grep -q "mk_api.py";       then PY_CHANGED=true;   fi
if echo "$PY_CHANGES" | grep -q "requirements.txt"; then REQS_CHANGED=true; fi

echo ""
if $PY_CHANGED;   then echo "  ✓ mk_api.py changed"; fi
if $REQS_CHANGED; then echo "  ✓ requirements.txt changed"; fi
if ! $PY_CHANGED && ! $REQS_CHANGED; then
  echo "  — No Python changes detected. Skipping Flask restart."
  echo ""
  echo "==> Deploy complete (static/data files updated if needed)."
  echo "    Open: http://46.224.208.142/"
  exit 0
fi

echo ""
echo "==> Applying changes on server..."

ssh "$SERVER" bash << REMOTE
set -e
APP_DIR="$APP_DIR"
REQS_CHANGED="$REQS_CHANGED"

cd \$APP_DIR

if [ "\$REQS_CHANGED" = "true" ]; then
  echo "--- Installing updated dependencies..."
  venv/bin/pip install --quiet --upgrade pip
  venv/bin/pip install --quiet -r requirements.txt
  echo "    Done."
fi

echo "--- Fixing permissions..."
chown -R www-data:www-data \$APP_DIR

echo "--- Restarting Flask service..."
systemctl restart mk_climate
sleep 2
systemctl status mk_climate --no-pager

REMOTE

echo ""
echo "==> Deploy complete."
echo "    Open: http://46.224.208.142/"
