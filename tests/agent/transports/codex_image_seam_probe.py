#!/usr/bin/env python3
"""Rollout-level proof that a localImage UserInput reaches the model.

Why this exists: two live probes disagreed with the transcript. Asked to read
digits, the model answered wrong digits; asked whether an image was attached,
it answered NO_IMAGE. Both times codex's own saved rollout contained a real
``input_image`` content part with the exact file bytes. A model's self-report
is therefore NOT evidence about the transport — the rollout is.

This script asserts the seam at the only layer that can settle it:

  * the request Hermes builds contains a ``localImage`` item;
  * codex's saved rollout for that thread contains an ``input_image`` part;
  * the base64 in that part is BYTE-IDENTICAL (sha256) to the file on disk;
  * codex's own ``content_item_kinds`` lists ``user.image``.

What it deliberately does NOT claim: that the model correctly perceives the
image. That is a model-capability question, out of this layer's control, and
it is reported separately as an observation rather than folded into the
verdict.

    PYTHONPATH=<worktree> python3 \
        tests/agent/transports/codex_image_seam_probe.py --model gpt-6-astra

Temporary scoped state only; read-only permission profile.
"""

from __future__ import annotations

import argparse
import base64
import glob
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from codex_fidelity_live_probe import write_digit_png  # noqa: E402

from agent.transports.codex_app_server_session import (  # noqa: E402
    CodexAppServerSession,
    _build_turn_input,
)
from agent.transports.codex_thread_continuity import (  # noqa: E402
    CodexThreadContinuity,
    SessionModelConfigThreadStore,
)
from hermes_state import SessionDB  # noqa: E402

PROMPT = (
    "Answer with ONE line only.\n"
    "If an image was attached to this message, reply exactly: "
    "IMAGE_RECEIVED <the digits you see, no spaces>\n"
    "If no image was attached to this message, reply exactly: NO_IMAGE\n"
    "Do not guess digits. If you cannot see an image, say NO_IMAGE."
)


def find_rollout(thread_id: str) -> str | None:
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    hits = glob.glob(
        os.path.join(home, "sessions", "**", f"*{thread_id}.jsonl"),
        recursive=True,
    )
    return hits[0] if hits else None


def inspect_rollout(path: str, image_path: str) -> dict:
    """Extract ONLY the image-delivery facts. Never reproduce the transcript."""
    on_disk = open(image_path, "rb").read()
    facts = {
        "rollout_path": path,
        "input_image_parts": 0,
        "bytes_identical": False,
        "sha256_on_disk": hashlib.sha256(on_disk).hexdigest(),
        "sha256_in_rollout": None,
        "detail": None,
        "content_item_kinds": None,
    }
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            payload = rec.get("payload", {})
            if payload.get("type") != "message" or payload.get("role") != "user":
                continue
            for part in payload.get("content", []) or []:
                if part.get("type") != "input_image":
                    continue
                facts["input_image_parts"] += 1
                facts["detail"] = part.get("detail")
                sent = base64.b64decode(
                    re.sub(r"^data:[^,]+,", "", part.get("image_url", ""))
                )
                facts["sha256_in_rollout"] = hashlib.sha256(sent).hexdigest()
                facts["bytes_identical"] = sent == on_disk
                facts["content_item_kinds"] = payload.get(
                    "internal_chat_message_metadata_passthrough", {}
                ).get("content_item_kinds")
                return facts
    return facts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", default="deliverables/evidence/image-seam.json")
    args = ap.parse_args()

    digits = f"{uuid.uuid4().int % 1000000:06d}"
    tmp = tempfile.mkdtemp(prefix="codex-image-seam-")
    cwd = os.path.join(tmp, "workspace")
    os.makedirs(cwd, exist_ok=True)
    image_path = os.path.join(tmp, "token.png")
    write_digit_png(image_path, digits)

    db = SessionDB(db_path=pathlib.Path(tmp) / "state.db")
    sid = "imageseam-" + uuid.uuid4().hex[:8]
    db.create_session(sid, source="cli")

    user_input = [
        {"type": "text", "text": PROMPT},
        {"type": "image_url", "image_url": {"url": f"file://{image_path}"}},
    ]
    built = _build_turn_input(user_input)
    report = {
        "codex_version_note": "run against the codex on PATH; record it",
        "expected_digits": digits,
        "image_path": image_path,
        "image_bytes": os.path.getsize(image_path),
        "built_turn_input": built,
        "request_carries_localImage": any(
            i.get("type") == "localImage" for i in built
        ),
    }

    session = CodexAppServerSession(
        cwd=cwd,
        permission_profile="read-only",
        model=args.model,
        continuity=CodexThreadContinuity(
            SessionModelConfigThreadStore(db, sid),
            cwd=cwd, permission_profile="read-only", model=args.model,
        ),
    )
    try:
        report["thread_id"] = session.ensure_started()
        report["runtime"] = session.runtime
        r = session.run_turn(user_input=user_input, turn_timeout=240.0)
        text = (r.final_text or "").strip()
        report["error"] = r.error
        # Model SELF-REPORT: recorded as an observation, never as evidence
        # about the transport. Measured 2026-09-06: the model answered
        # NO_IMAGE on a turn whose rollout provably contained the image.
        report["model_self_report"] = {
            "final_text": text[:400],
            "claims_image_received": "IMAGE_RECEIVED" in text,
            "claims_no_image": "NO_IMAGE" in text,
            "digits_correct": digits in text,
            "note": "self-report only; not evidence about delivery",
        }
    except Exception as exc:
        report["exception"] = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()

    rollout = find_rollout(report.get("thread_id", "")) if report.get(
        "thread_id"
    ) else None
    if rollout:
        report["rollout_evidence"] = inspect_rollout(rollout, image_path)
    else:
        report["rollout_evidence"] = {"error": "rollout not found"}

    ev = report["rollout_evidence"]
    delivered = (
        report.get("request_carries_localImage")
        and ev.get("input_image_parts", 0) >= 1
        and ev.get("bytes_identical") is True
        and "user.image" in (ev.get("content_item_kinds") or [])
    )
    report["verdict"] = (
        "IMAGE_DELIVERED_TO_MODEL" if delivered else "IMAGE_DELIVERY_UNPROVEN"
    )

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, default=str)
    print(f"{report['verdict']}: wrote {args.out}")
    return 0 if delivered else 1


if __name__ == "__main__":
    sys.exit(main())
