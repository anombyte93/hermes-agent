"""RED control for #113 tests: run the new tests against the OLD inline
formatter to prove they are non-vacuous (they must FAIL there)."""

import sys
from pathlib import Path

# Recreate the OLD formatter verbatim from the pre-fix line
def old_format(ev_payload):
    limit_seconds = ev_payload.get("limit_seconds")
    return f"max_runtime={int(limit_seconds or 0)}s"

cases = [
    ({"iterations": 150, "max_iterations": 150, "limit_seconds": 7200},
     ["150/150" , "7200"], "iteration exhaustion names iterations"),
    ({}, ["unknown"], "missing limit renders unknown"),
    (None, ["unknown"], "none payload unknown"),
]

failures = 0
for payload, musts, label in cases:
    out = old_format(payload or {})
    for m in musts:
        if m not in out:
            print(f"RED OK: old formatter fails '{label}' (missing {m!r} in {out!r})")
            failures += 1
            break
    else:
        print(f"VACUOUS: old formatter satisfies '{label}' — test is worthless")
        sys.exit(1)

print(f"old formatter fails {failures}/{len(cases)} case groups -> tests are non-vacuous")
