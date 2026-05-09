#!/usr/bin/env bash
# One-time Python venv setup. Re-running is safe (idempotent pip install).

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

if [[ ! -d .venv ]]; then
    echo "→ creating .venv"
    python3 -m venv .venv
fi

echo "→ upgrading pip"
.venv/bin/pip install --quiet --upgrade pip

echo "→ installing dependencies"
.venv/bin/pip install --quiet pyobjc-framework-EventKit lunardate pyyaml

echo
echo "✓ setup complete"
echo
echo "next steps:"
echo "  1. .venv/bin/python scripts/calendar_sync.py    # grant Calendar access"
echo "  2. ./scripts/install_launchd.sh                 # register daily 06:00 job"
echo "  3. .venv/bin/python scripts/daily_check.py      # first real run"
