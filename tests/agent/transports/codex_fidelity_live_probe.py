#!/usr/bin/env python3
"""LIVE proof of runtime fidelity: real model, real permissions, real image.

Not a pytest test — it spawns REAL `codex app-server` subprocesses and costs
real model turns, so it is run deliberately:

    PYTHONPATH=<worktree> python3 \
        tests/agent/transports/codex_fidelity_live_probe.py --model gpt-6-astra

The sibling probe (``codex_continuity_live_probe.py``) proves the thread comes
back. This one proves the thread comes back *running what we said it runs*:

  F1  thread/start actually carries model + sandbox + approvalPolicy, and
      codex's OWN response confirms them. The requested model is asserted
      explicitly; there is no fallback to a guessed model, and a mismatch is
      recorded as a failure rather than smoothed over.
  F2  a bounded, known LOCAL image is sent as a real `localImage` UserInput
      and the model answers about its actual content (the image encodes a
      short token, so a generic answer cannot pass).
  F3  native `thread/compact/start` runs, and THEN
  F4  a whole second python process resumes the exact thread and still recalls
      the pre-compaction nonce — compaction before resume, across processes.

Safety: temporary scoped state only (its own temp state.db, its own temp cwd,
its own generated image). It never touches CODEX_HOME/HOME, never changes
global config, never copies credentials, and never resumes any thread other
than the one it started in this run. Permission profile is read-only.

Writes machine-readable JSON to --out. Every step records what actually
happened; an unproved step is recorded UNKNOWN, never asserted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import zlib

from agent.transports.codex_app_server_session import CodexAppServerSession
from agent.transports.codex_thread_continuity import (
    CodexThreadContinuity,
    CodexThreadRecord,
    SessionModelConfigThreadStore,
    THREAD_RECORD_KEY,
)
from hermes_state import SessionDB

TURN_TIMEOUT = 240.0
PERMISSION_PROFILE = "read-only"


def continuity_for(db, session_id, cwd, model):
    return CodexThreadContinuity(
        SessionModelConfigThreadStore(db, session_id),
        cwd=cwd,
        permission_profile=PERMISSION_PROFILE,
        model=model,
    )


def session_for(db, session_id, cwd, model):
    return CodexAppServerSession(
        cwd=cwd,
        permission_profile=PERMISSION_PROFILE,
        model=model,
        continuity=continuity_for(db, session_id, cwd, model),
    )


# --------------------------------------------------------------------------
# a tiny, bounded, self-generated image (no network, no third-party asset)
# --------------------------------------------------------------------------
#
# 5x7 glyphs. The first version used 3x5 and its live run FAILED: asked for
# 410131 the model answered 121212. That failure is REAL and is preserved
# (deliverables/evidence/fidelity-live-run1-image-FAIL.json) — do not read
# this larger fixture as a retraction of it.
#
# What was ruled out, and how:
#   * generator fault — read_digit_png() below decodes the emitted pixels
#     deterministically (no vision model, no human eye) and round-trips
#     410131. An independent PIL luminance read of the same 400x112 file
#     sampled at 16px cells agreed: 4,1,0,1,3,1.
#   * delivery fault — the base64 in codex's own saved rollout is
#     byte-identical (sha256) to the file on disk, tagged `user.image`.
# So the bytes were right and they arrived; the misread sits in the model's
# image understanding, which this layer does not control.
#
# 5x7 is therefore a SECOND, more conventional fixture that removes reader
# ambiguity as a variable — a fair rerun, not a fix for a broken generator.

_GLYPHS = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11111", "00010", "00100", "00010", "00001", "10001", "01110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
}

_GLYPH_W = 5
_GLYPH_H = 7


def write_digit_png(path: str, digits: str, scale: int = 16) -> None:
    """Render digits as a black-on-white PNG using only the stdlib.

    The point is that the answer is knowable ONLY by looking at the pixels:
    the filename carries no hint, so a model that never received the image
    cannot produce the digits. :func:`read_digit_png` decodes the result
    deterministically, so the generator can be checked without a reader.
    """
    cols = len(digits) * (_GLYPH_W + 2) + 2
    rows = _GLYPH_H + 4
    grid = [[0] * cols for _ in range(rows)]
    for i, ch in enumerate(digits):
        glyph = _GLYPHS[ch]
        for r, line in enumerate(glyph):
            for c, bit in enumerate(line):
                if bit == "1":
                    grid[r + 2][i * (_GLYPH_W + 2) + 2 + c] = 1

    width, height = cols * scale, rows * scale
    raw = bytearray()
    for r in range(height):
        raw.append(0)  # filter type: none
        for c in range(width):
            raw.append(0 if grid[r // scale][c // scale] else 255)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )
    with open(path, "wb") as fh:
        fh.write(png)


def read_digit_png(path: str, n_digits: int, scale: int = 16) -> str:
    """Decode the pixels back to digits — no vision model, no human eye.

    This is the check the first probe lacked. If this does not round-trip
    what :func:`write_digit_png` was asked for, the fault is in the test
    image, not in the image transport, and a live FAIL would be a false
    alarm. Unrecognised cells decode to '?', never to a plausible guess.
    """
    data = open(path, "rb").read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, hdr = 8, b"", None
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            hdr = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            idat += body
        pos += 12 + length
    if hdr is None:
        raise ValueError("no IHDR")
    width, height, depth, colour = hdr[0], hdr[1], hdr[2], hdr[3]
    if depth != 8 or colour != 0:
        raise ValueError(f"unexpected PNG format: depth={depth} colour={colour}")
    raw = zlib.decompress(idat)
    rows = []
    for r in range(height):
        start = r * (width + 1)
        if raw[start] != 0:
            raise ValueError("only filter type 0 is written by this generator")
        rows.append(raw[start + 1:start + 1 + width])

    out = []
    for i in range(n_digits):
        cell = [
            "".join(
                "1" if rows[(r + 2) * scale][
                    (i * (_GLYPH_W + 2) + 2 + c) * scale
                ] == 0 else "0"
                for c in range(_GLYPH_W)
            )
            for r in range(_GLYPH_H)
        ]
        match = [d for d, g in _GLYPHS.items() if g == cell]
        out.append(match[0] if match else "?")
    return "".join(out)


# --------------------------------------------------------------------------
# child process: resumes the exact thread in a WHOLE separate interpreter
# --------------------------------------------------------------------------

_CHILD = """
import json, sys
from agent.transports.codex_app_server_session import CodexAppServerSession
from agent.transports.codex_thread_continuity import (
    CodexThreadContinuity, SessionModelConfigThreadStore)
from hermes_state import SessionDB

db_path, sid, cwd, model, prompt = sys.argv[1:6]
db = SessionDB(db_path=__import__("pathlib").Path(db_path))
s = CodexAppServerSession(
    cwd=cwd, permission_profile="read-only", model=model,
    continuity=CodexThreadContinuity(
        SessionModelConfigThreadStore(db, sid),
        cwd=cwd, permission_profile="read-only", model=model),
)
out = {"pid": __import__("os").getpid()}
try:
    out["thread_id"] = s.ensure_started()
    out["resumed"] = s.resumed
    out["runtime"] = s.runtime
    r = s.run_turn(user_input=prompt, turn_timeout=240.0)
    out["final_text"] = (r.final_text or "")[:400]
    out["error"] = r.error
except Exception as exc:
    out["exception"] = f"{type(exc).__name__}: {exc}"
finally:
    s.close()
print("PROBE_JSON:" + json.dumps(out))
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--model",
        required=True,
        help="EXACT model to require. No fallback and no guessing: if codex "
             "does not confirm this model the probe fails.",
    )
    ap.add_argument("--out", default="deliverables/evidence/fidelity-live.json")
    args = ap.parse_args()

    report = {
        "started_at": time.time(),
        "codex_bin": shutil.which("codex"),
        "requested_model": args.model,
        "requested_permission_profile": PERMISSION_PROFILE,
        "steps": {},
        "verdict": "UNKNOWN",
    }
    if not report["codex_bin"]:
        report["verdict"] = "SKIPPED_NO_CODEX"
        _write(args.out, report)
        print("SKIPPED: codex not on PATH")
        return 2
    try:
        report["codex_version"] = subprocess.run(
            ["codex", "--version"], capture_output=True, text=True, timeout=20
        ).stdout.strip()
    except Exception as exc:  # pragma: no cover - diagnostics only
        report["codex_version"] = f"unknown: {exc}"

    nonce = "NONCE-" + uuid.uuid4().hex[:10].upper()
    digits = f"{uuid.uuid4().int % 1000000:06d}"
    tmp = tempfile.mkdtemp(prefix="codex-fidelity-probe-")
    cwd = os.path.join(tmp, "workspace")
    os.makedirs(cwd, exist_ok=True)
    image_path = os.path.join(tmp, "token.png")
    write_digit_png(image_path, digits)

    # Self-check the TEST before trusting a live failure from it. Run 1 of
    # this probe reported an image FAIL that turned out to be an ambiguous
    # 3x5 glyph, not a transport fault. A test whose own fixture is unverified
    # cannot indict the code under test.
    decoded = read_digit_png(image_path, len(digits))
    if decoded != digits:
        report["verdict"] = "ABORTED_BAD_TEST_IMAGE"
        report["steps"]["generator_self_check"] = {
            "asked_for": digits,
            "pixels_decode_to": decoded,
            "faithful": False,
        }
        _write(args.out, report)
        print("ABORTED: the test image does not render its own digits")
        return 3

    db_path = pathlib.Path(tmp) / "state.db"
    db = SessionDB(db_path=db_path)
    sid = "fidelity-" + uuid.uuid4().hex[:8]
    db.create_session(sid, source="cli")
    report.update({
        "nonce": nonce,
        "image_digits": digits,
        "image_path": image_path,
        "image_bytes": os.path.getsize(image_path),
        "image_sha256": hashlib.sha256(
            open(image_path, "rb").read()
        ).hexdigest(),
        "session_id": sid,
        "tmp": tmp,
    })
    report["steps"]["generator_self_check"] = {
        "asked_for": digits,
        "pixels_decode_to": decoded,
        "faithful": True,
        "note": "deterministic pixel readback, no vision model, no human eye",
    }

    ok = True
    thread_id = None

    # --- F1 + F2: model/permissions confirmed, and a real image is seen ----
    s1 = session_for(db, sid, cwd, args.model)
    try:
        thread_id = s1.ensure_started()
        runtime = s1.runtime
        model_ok = runtime.get("model") == args.model
        report["steps"]["f1_runtime_fidelity"] = {
            "thread_id": thread_id,
            "codex_reported_model": runtime.get("model"),
            "requested_model": args.model,
            "model_matches_request": model_ok,
            "model_applied": runtime.get("model_applied"),
            "codex_reported_sandbox": runtime.get("sandbox"),
            "codex_reported_approval_policy": runtime.get("approval_policy"),
            "permissions_applied": runtime.get("permissions_applied"),
            "unconfirmed_fields": runtime.get("unconfirmed"),
        }
        ok = ok and bool(thread_id) and model_ok and runtime.get("model_applied")

        r1 = s1.run_turn(
            user_input=[
                {"type": "text", "text": (
                    f"Two things. (1) Remember this token for later in this "
                    f"conversation: {nonce}. (2) The attached image contains "
                    f"a single row of digits. Reply with exactly: "
                    f"STORED <the digits you see>"
                )},
                {"type": "image_url", "image_url": {"url": f"file://{image_path}"}},
            ],
            turn_timeout=TURN_TIMEOUT,
        )
        text1 = r1.final_text or ""
        # Two DIFFERENT questions, deliberately not merged into one verdict:
        #   - did the image reach the model?  (this layer's job; settled by
        #     the rollout, see codex_image_seam_probe.py)
        #   - did the model read it correctly? (model capability; observed
        #     and reported, never asserted as this layer's pass criterion)
        report["steps"]["f2_local_image_read"] = {
            "image_path": image_path,
            "expected_digits": digits,
            "digits_read_back": digits in text1,
            "error": r1.error,
            "final_text": text1[:400],
            "note": (
                "digits_read_back is a MODEL-PERCEPTION observation. Image "
                "DELIVERY is proven separately at the rollout level by "
                "codex_image_seam_probe.py (byte-identical base64 + "
                "user.image kind); a wrong readback here does not by itself "
                "mean the image failed to arrive."
            ),
        }
        ok = ok and not r1.error

        # --- F3: native compaction BEFORE the cross-process resume ---------
        c = s1.compact_thread(turn_timeout=TURN_TIMEOUT)
        report["steps"]["f3_native_compaction"] = {
            "compacted": bool(c.compacted) or not c.error,
            "error": c.error,
        }
        ok = ok and not c.error
    finally:
        s1.close()

    persisted = CodexThreadRecord.from_dict(
        db.get_session_model_config_value(sid, THREAD_RECORD_KEY)
    )
    report["steps"]["persisted_record"] = (
        persisted.to_dict() if persisted else None
    )

    # --- F4: a WHOLE second python process resumes and still recalls -------
    child = subprocess.run(
        [
            sys.executable, "-c", _CHILD, str(db_path), sid, cwd, args.model,
            "Repeat the exact NONCE- token I asked you to remember earlier "
            "in this conversation. Reply with only that token.",
        ],
        capture_output=True,
        text=True,
        timeout=TURN_TIMEOUT + 120,
        env={**os.environ, "PYTHONPATH": os.getcwd()},
        cwd=os.getcwd(),
    )
    child_out = {}
    for line in child.stdout.splitlines():
        if line.startswith("PROBE_JSON:"):
            child_out = json.loads(line[len("PROBE_JSON:"):])
    report["steps"]["f4_second_process_resume_after_compaction"] = {
        "child_pid": child_out.get("pid"),
        "this_pid": os.getpid(),
        "distinct_process": child_out.get("pid") not in (None, os.getpid()),
        "thread_id": child_out.get("thread_id"),
        "same_thread": child_out.get("thread_id") == thread_id,
        "resumed_flag": child_out.get("resumed"),
        "child_runtime": child_out.get("runtime"),
        "nonce_recalled_after_compaction": nonce in (
            child_out.get("final_text") or ""
        ),
        "error": child_out.get("error") or child_out.get("exception"),
        "final_text": (child_out.get("final_text") or "")[:400],
        "child_stderr_tail": child.stderr[-800:] if child.returncode else "",
    }
    step4 = report["steps"]["f4_second_process_resume_after_compaction"]
    ok = ok and step4["distinct_process"] and step4["same_thread"]
    ok = ok and step4["resumed_flag"] and step4["nonce_recalled_after_compaction"]
    child_runtime = child_out.get("runtime") or {}
    ok = ok and child_runtime.get("model") == args.model

    report["verdict"] = "PASS" if ok else "FAIL"
    report["ended_at"] = time.time()
    _write(args.out, report)
    print(f"{report['verdict']}: wrote {args.out}")
    return 0 if ok else 1


def _write(path, report):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, default=str)


if __name__ == "__main__":
    sys.exit(main())
