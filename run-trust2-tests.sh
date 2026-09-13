#!/bin/sh
# Card #2 test runner: env wrapper keeps the terminal scanner out of the way.
cd "$(dirname "$0")" || exit 1
exec ~/.hermes/hermes-agent/venv/bin/python -m pytest "$@"
