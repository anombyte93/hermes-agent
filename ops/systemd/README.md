# Default-board Kanban daemon

This user service keeps the Hermes **default** Kanban board moving when no long-lived default-profile gateway process owns the embedded dispatcher. It intentionally uses the repository virtual environment and does not set `HERMES_PROFILE`, so Hermes resolves the normal default home (`~/.hermes`).

The daemon runs in standalone `--force` mode because `kanban.dispatch_in_gateway` defaults to enabled. That legacy command does **not** acquire the embedded gateway lock itself, so the unit wraps it with `/usr/bin/flock --no-fork --nonblock $HOME/.hermes/kanban/.dispatcher.lock`. This is the exact lock path used by the gateway and it is **machine-global across profiles**, not per database. A gateway with embedded dispatch accidentally re-enabled therefore contends instead of racing this service; conversely, an embedded dispatcher for any other profile also contends, so keep gateway dispatch disabled when this unit owns the machine-global lock.

## Install

Record the current embedded-dispatch setting so rollback can restore the operator's prior choice:

```bash
PREVIOUS_DISPATCH_IN_GATEWAY="$(hermes config get kanban.dispatch_in_gateway)"
printf 'Previous kanban.dispatch_in_gateway=%s\n' "$PREVIOUS_DISPATCH_IN_GATEWAY"
```

When this service is the sole dispatcher, disable embedded gateway dispatch in the default home before enabling it:

```bash
hermes config set kanban.dispatch_in_gateway false
install -Dm644 ops/systemd/hermes-kanban-daemon.service \
  ~/.config/systemd/user/hermes-kanban-daemon.service
systemctl --user daemon-reload
systemctl --user enable --now hermes-kanban-daemon.service
```

## Observe

```bash
systemctl --user status hermes-kanban-daemon.service
journalctl --user -u hermes-kanban-daemon.service -f
hermes kanban --board default list --json
# While the service is active this must exit non-zero: the shared lock is held.
/usr/bin/flock --no-fork --nonblock \
  $HOME/.hermes/kanban/.dispatcher.lock /usr/bin/true
```

A normal verbose tick reports promoted, spawned, skipped and running counts. A zero-spawn tick is not evidence by itself that the daemon is healthy; prove it with a reversible default-board task and read back the corresponding task/run records.

## Maintenance safety

**Do not stop or restart this unit while it owns active workers.** A live acceptance run showed that stopping the service can terminate a worker it spawned, which the next dispatcher tick correctly records as a crash and may turn into a retry or circuit-breaker block.

There is not yet a first-class drain command. Before planned maintenance, check both running and ready work and wait for the board to quiesce:

```bash
hermes kanban --board default list --status running --json
hermes kanban --board default list --status ready --json
```

If either list is non-empty, do not stop or restart the service. Merely seeing no running cards is not a durable drain barrier: another ready card can be claimed on the next tick.

## Roll back

Stop and remove only this unit, then restore the `kanban.dispatch_in_gateway` value captured before installation:

```bash
systemctl --user disable --now hermes-kanban-daemon.service
rm -f ~/.config/systemd/user/hermes-kanban-daemon.service
systemctl --user daemon-reload
hermes config set kanban.dispatch_in_gateway "$PREVIOUS_DISPATCH_IN_GATEWAY"
```

Rollback does not delete the Kanban database, tasks, runs, logs, or worker worktrees.
