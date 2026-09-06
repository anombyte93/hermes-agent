#!/usr/bin/env python3
"""Race child for test_two_real_processes_cannot_both_start.

Usage: _continuity_race_child.py <db_path> <session_id> <cwd>

Contends for the same Hermes session's codex thread reservation as its
siblings, holds it briefly if it wins, and prints one JSON line describing
what actually happened. No codex subprocess is spawned — the contended
resource is the reservation itself, which is the thing under test.
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.environ.get("PYTHONPATH", "").split(os.pathsep)[0])

from agent.transports.codex_thread_continuity import (  # noqa: E402
    CodexThreadContinuity,
    CodexThreadContinuityError,
    SessionModelConfigThreadStore,
)
from hermes_state import SessionDB  # noqa: E402


def main() -> int:
    db_path, session_id, cwd = sys.argv[1], sys.argv[2], sys.argv[3]
    continuity = CodexThreadContinuity(
        SessionModelConfigThreadStore(SessionDB(db_path=Path(db_path)),
                                      session_id),
        cwd=cwd,
        permission_profile="workspace-write",
    )
    # Line all children up on roughly the same instant so the contention is
    # real rather than accidentally sequential.
    start = float(os.environ.get("RACE_AT", "0")) or (time.time() + 0.35)
    while time.time() < start:
        time.sleep(0.005)

    out = {"pid": os.getpid()}
    try:
        record = continuity.load_resumable()
        out["outcome"] = "reserved"
        out["record"] = record.thread_id if record else None
        # Hold it long enough that every sibling has certainly tried.
        time.sleep(1.5)
        continuity.release()
    except CodexThreadContinuityError as exc:
        out["outcome"] = "refused"
        out["error_type"] = type(exc).__name__
        out["error"] = str(exc)[:200]
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
