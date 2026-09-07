#!/usr/bin/env bash
# Launch adapters for the Astra shared-conversation entrances.
#
# Every command here is an EXISTING Hermes entry point pointed at the
# shared-conversation profile. Nothing here invents a URL scheme, a deep link
# or a second frontend: the mode is a per-profile config flag, so "attach to
# the shared conversation" is simply "run the normal client under the profile
# that has the flag on".
#
# Usage:
#   astra-shared.sh install     # copy the profile template (idempotent)
#   astra-shared.sh terminal    # entrance 2: terminal TUI
#   astra-shared.sh serve       # backend for entrances 3 and 4 (Desktop/browser)
#   astra-shared.sh check       # attach, print status, detach — changes nothing
#   astra-shared.sh rollback    # remove the profile; the service is untouched
#
# The Discord DM (entrance 1) is served by the service itself and is not
# launched from here.

set -euo pipefail

PROFILE="${ASTRA_SHARED_PROFILE:-astra-shared}"
HERMES_ROOT="${HERMES_ROOT:-$HOME/.hermes}"
PROFILE_DIR="$HERMES_ROOT/profiles/$PROFILE"
TEMPLATE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/templates/astra-shared-conversation.config.yaml"
SERVE_HOST="${ASTRA_SHARED_HOST:-127.0.0.1}"
SERVE_PORT="${ASTRA_SHARED_PORT:-5199}"

die() { echo "error: $*" >&2; exit 1; }

cmd_install() {
  [ -f "$TEMPLATE" ] || die "template not found: $TEMPLATE"
  mkdir -p "$PROFILE_DIR"
  if [ -f "$PROFILE_DIR/config.yaml" ]; then
    echo "profile already exists: $PROFILE_DIR/config.yaml (left untouched)"
  else
    cp "$TEMPLATE" "$PROFILE_DIR/config.yaml"
    chmod 600 "$PROFILE_DIR/config.yaml"
    echo "installed: $PROFILE_DIR/config.yaml"
  fi
  echo "Edit socket_path/tcp_host and token_ref before first use."
}

cmd_rollback() {
  # Removes ONLY this client profile. The service, its owner, its conversation
  # database and its archive are not touched by anything in this script.
  if [ -d "$PROFILE_DIR" ]; then
    rm -rf "$PROFILE_DIR"
    echo "removed: $PROFILE_DIR"
  else
    echo "nothing to remove: $PROFILE_DIR"
  fi
  echo "The shared conversation service was NOT touched."
}

# Entrance 2 — terminal. `hermes --tui` is the stock terminal client; the
# profile flag makes its session.create/prompt.submit attach to the service
# instead of building a local agent.
cmd_terminal() {
  exec hermes --profile "$PROFILE" --tui "$@"
}

# Entrances 3 and 4 — Hermes Desktop and any browser-hosted Hermes frontend.
#
# Both speak JSON-RPC over the EXISTING /api/ws WebSocket that `hermes serve`
# mounts (tui_gateway/ws.py; web/src/lib/gatewayClient.ts connects to that same
# path). Start this backend under the shared profile and:
#
#   * Hermes Desktop: add a remote connection pointing at
#     ws://$SERVE_HOST:$SERVE_PORT/api/ws (Desktop's existing remote-client
#     configuration — no new launch surface).
#   * Browser / Cortex Workbench: open http://$SERVE_HOST:$SERVE_PORT/chat,
#     the dashboard's built-in Chat tab. It is a real Hermes frontend on the
#     same WebSocket, so it attaches to the shared conversation exactly as the
#     Desktop does. There is NO hermes:// scheme handler on this machine and
#     this script does not pretend otherwise — the Workbench should link to
#     this http(s) URL.
cmd_serve() {
  exec hermes --profile "$PROFILE" serve \
    --host "$SERVE_HOST" --port "$SERVE_PORT" "$@"
}

# Read-only probe: attaches as a client, prints service status, detaches.
# Takes no ownership and submits nothing.
cmd_check() {
  python3 - <<'PY'
import json
from tui_gateway import shared_conversation as sc

cfg = sc.active_config(force=True)
if cfg is None:
    raise SystemExit(
        f"shared_conversation is not enabled: {sc.config_error() or 'no block'}"
    )
client = sc.AttachClient(cfg.address, token=cfg.token, timeout=cfg.connect_timeout)
client.connect()
try:
    print(json.dumps({"ping": client.ping(), "status": client.status()}, indent=2))
finally:
    client.close()
PY
}

case "${1:-}" in
  install)  shift; cmd_install "$@" ;;
  rollback) shift; cmd_rollback "$@" ;;
  terminal) shift; cmd_terminal "$@" ;;
  serve)    shift; cmd_serve "$@" ;;
  check)    shift; cmd_check "$@" ;;
  *) die "usage: astra-shared.sh {install|terminal|serve|check|rollback}" ;;
esac
