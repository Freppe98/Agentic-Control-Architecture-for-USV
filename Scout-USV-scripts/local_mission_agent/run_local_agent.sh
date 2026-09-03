#!/usr/bin/env bash
# Runs the Scout Local Mission Agent in the foreground.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

exec python3 -u local_agent.py
