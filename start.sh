#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || python3 -m venv .venv
exec .venv/bin/python bootstrap.py --bind 0.0.0.0 "$@"
