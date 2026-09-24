#!/bin/sh
set -e

if [ $# -gt 0 ]; then
    exec "$@"
fi

STATE_DIR="${STATE_DIR:-/app/data}"
APP_MODE="${APP_MODE:-production}"
JOURNAL_MODE="${JOURNAL_MODE:-DELETE}"

# Initialize production workspace on first run if marker does not exist
if [ ! -f "$STATE_DIR/.workspace-mode" ]; then
    echo "==> Initializing $APP_MODE workspace in $STATE_DIR (journal mode: $JOURNAL_MODE)..."
    python manage.py init --state-dir "$STATE_DIR" --mode "$APP_MODE" --journal-mode "$JOURNAL_MODE"
    echo "==> Workspace initialized successfully."
fi

echo "==> Starting Flet Web application in $APP_MODE mode on $HOST:$PORT..."
exec python app.py
