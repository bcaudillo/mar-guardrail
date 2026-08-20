#!/bin/bash
# SessionStart hook for Claude Code on the web.
#
# Installs the mar-guardrail Python dependencies so a fresh web session can
# import the modules and run main.py / app.py without manual setup. The
# container state is cached after this completes, so subsequent sessions start
# warm. Safe to run repeatedly (pip install is idempotent).
#
# Only runs in the remote (web) environment — local runs exit immediately so
# this never interferes with your own machine.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# Minimal deps from requirements.txt (psycopg2-binary, requests, streamlit,
# python-dotenv). Add anything new there and it'll be picked up automatically.
# --break-system-packages keeps pip happy on PEP-668 "externally managed"
# Pythons (the Debian base image); harmless if your image uses a venv.
python -m pip install --quiet --break-system-packages -r requirements.txt

# Smoke check: the framework's pure-Python modules must import cleanly. This is
# the closest thing to a "test" in this repo today — extend with real tests
# (e.g. pytest) here if you add them.
python - << 'PY'
import config, query, fivetran_api, triggers, main
config.validate_config()
print("mar-guardrail: imports OK, config valid")
PY
