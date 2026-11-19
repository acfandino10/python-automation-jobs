#!/bin/bash
# Scheduled entry point. Edit the three settings below, or export them from cron.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# How many applications this wake-up may submit.
RUN_LIMIT="${RUN_LIMIT:-4}"
# How many submitted applications are allowed on this machine's calendar day.
DAILY_LIMIT="${DAILY_LIMIT:-10}"
# Manifest slug or title. Comma-separate several, for example "backend-engineer,data-engineer".
# When input/ has several résumés, the same ROLES value is applied for each person.
: "${ROLES:?Set ROLES to a role slug from the manifest}"

mkdir -p "$ROOT/output/logs"
LOCKDIR="$ROOT/output/logs/apply.lock"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  echo "Another apply run is still going."
  exit 0
fi
trap 'rmdir "$LOCKDIR"' EXIT

exec "$ROOT/.venv/bin/python" "$ROOT/apply_jobs.py" \
  --roles "$ROLES" \
  --auto-submit \
  --headless \
  --limit "$RUN_LIMIT" \
  --daily-limit "$DAILY_LIMIT"
