# Default-board Kanban daemon

This user service keeps the Hermes **default** Kanban board moving when no long-lived default-profile gateway process owns the embedded dispatcher. It intentionally uses the repository virtual environment and does not set `HERMES_PROFILE`, so Hermes resolves the normal default home (`~/.hermes`).

The daemon runs in standalone `--force` mode because `kanban.dispatch_in_gateway` defaults to enabled. The same daemon singleton lock used by embedded dispatch still prevents two dispatch loops from owning the board at once.

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
```

A normal verbose tick reports promoted, spawned, skipped and running counts. A zero-spawn tick is not evidence by itself that the daemon is healthy; prove it with a reversible default-board task and read back the corresponding task/run records.

## Roll back

Stop and remove only this unit, then restore the `kanban.dispatch_in_gateway` value captured before installation:

```bash
systemctl --user disable --now hermes-kanban-daemon.service
rm -f ~/.config/systemd/user/hermes-kanban-daemon.service
systemctl --user daemon-reload
hermes config set kanban.dispatch_in_gateway "$PREVIOUS_DISPATCH_IN_GATEWAY"
```

Rollback does not delete the Kanban database, tasks, runs, logs, or worker worktrees.
