#!/bin/bash
# Deploy MK Climate Explorer to Hetzner CX23
# Run from your Mac: bash deploy.sh
#
# Smart deploy — only restarts services when relevant files actually changed:
#   mk_api.py changed      →  clear disk cache + restart Flask
#   chat_config.py changed →  restart Flask (all chat state is in-memory;
#                              restart resets global counters, limiter, and
#                              invalidates any cached DL tokens if secret changed)
#   requirements.txt changed → pip install + restart Flask
#   static/ changed          →  no restart needed
#   data/ changed            →  no restart needed
#
# Cache clearing rationale:
#   mk_api.py changes can alter computation logic, making cached results stale.
#   Disk caches (today_*.json, annual_trend_*.json, cal_*.json) survive restarts,
#   so we clear them explicitly when mk_api.py changes.
#   chat_config.py only affects in-memory state (rate limits, global counters,
#   error messages) — all reset automatically on restart, no disk cache to clear.

set -e

SERVER="root@46.224.208.142"
APP_DIR="/opt/mk_climate"

echo "==> Syncing files (checking for changes)..."
ssh "$SERVER" "mkdir -p $APP_DIR/data $APP_DIR/static"

# Sync Python app + requirements — capture itemised output to detect changes
PY_CHANGES=$(rsync -az --checksum --itemize-changes \
  mk_api.py \
  chat_config.py \
  requirements.txt \
  "$SERVER:$APP_DIR/")

# Sync static files (no restart needed for these)
rsync -az --checksum --progress static/ "$SERVER:$APP_DIR/static/"

# Sync data directory (no restart needed)
rsync -az --checksum --progress data/ "$SERVER:$APP_DIR/data/"

# Determine what changed
API_CHANGED=false    # mk_api.py specifically (affects disk caches)
PY_CHANGED=false     # any Python file (triggers restart)
REQS_CHANGED=false

if echo "$PY_CHANGES" | grep -q "mk_api.py";        then API_CHANGED=true; PY_CHANGED=true; fi
if echo "$PY_CHANGES" | grep -q "chat_config.py";   then PY_CHANGED=true;                   fi
if echo "$PY_CHANGES" | grep -q "requirements.txt"; then REQS_CHANGED=true;                  fi

echo ""
if $API_CHANGED;  then echo "  ✓ mk_api.py changed  → will clear disk caches + restart"; fi
if $PY_CHANGED && ! $API_CHANGED; then
                       echo "  ✓ chat_config.py changed → will restart (resets counters/limiter)"; fi
if $REQS_CHANGED; then echo "  ✓ requirements.txt changed → will pip install"; fi
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
API_CHANGED="$API_CHANGED"
REQS_CHANGED="$REQS_CHANGED"

cd \$APP_DIR

if [ "\$REQS_CHANGED" = "true" ]; then
  echo "--- Installing updated dependencies..."
  venv/bin/pip install --quiet --upgrade pip
  venv/bin/pip install --quiet -r requirements.txt
  echo "    Done."
fi

# mk_api.py changed: clear all disk caches so stale computed results
# (today status, annual trend, calendar) are recomputed fresh after restart.
if [ "\$API_CHANGED" = "true" ]; then
  echo "--- Clearing disk caches (stale after mk_api.py change)..."
  CACHE_DIR="\$APP_DIR/cache"
  removed=0
  for pattern in "today_*.json" "annual_trend_*.json" "cal_*.json"; do
    count=\$(ls "\$CACHE_DIR"/\$pattern 2>/dev/null | wc -l)
    if [ "\$count" -gt 0 ]; then
      rm -f "\$CACHE_DIR"/\$pattern
      removed=\$((removed + count))
    fi
  done
  echo "    Removed \$removed cache file(s)."
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
