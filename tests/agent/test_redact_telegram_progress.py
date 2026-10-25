"""Telegram masking must preserve matches without stalling on numeric output."""

import re
import subprocess
import sys
import textwrap
from pathlib import Path

from agent.redact import redact_sensitive_text, redact_terminal_output
from tools.terminal_tool_result import _redact_spill_file


def test_telegram_masking_preserves_embedded_and_adjacent_matches(tmp_path):
    body = "aB0_-" * 6
    bot_id = "12345678"
    token = f"{bot_id}:{body}"
    masked = f"{bot_id}:***"
    unicode_id = "\u0667" * 8
    cases = [
        (token, masked),
        ("bot" + token, "bot" + masked),
        ("word" + token + "/getMe", "word" + masked + "/getMe"),
        ("https://api.telegram.org/bot" + token + "/getMe",
         "https://api.telegram.org/bot" + masked + "/getMe"),
        (token + ",bot" + token, masked + ",bot" + masked),
        (token + "/" + token, masked + "/" + masked),
        (bot_id + ":" + "9" * 30, masked),
        ("7" * 7 + ":" + body, "7" * 7 + ":" + body),
        (bot_id + ":" + body[:-1], bot_id + ":" + body[:-1]),
        ("x:" + "7" * 80 + "x", "x:" + "7" * 80 + "x"),
        ("bot" + bot_id + ":short " + token, "bot" + bot_id + ":short " + masked),
        # A preceding match can end INSIDE a Unicode digit run: ASCII digits
        # belong to the first body but non-ASCII decimal digits do not. A
        # (?<!\d) boundary would miss the second token and leak its body.
        (bot_id + ":" + "a" * 29 + "7" + unicode_id + ":" + body,
         masked + unicode_id + ":***"),
    ]
    legacy = re.compile(r"(bot)?(\d{8,}):([-A-Za-z0-9_]{30,})")
    for index, (raw, expected) in enumerate(cases):
        assert legacy.sub(lambda m: f"{m[1] or ''}{m[2]}:***", raw) == expected
        assert redact_sensitive_text(raw, force=True) == expected
        for command in ("printf synthetic", "printenv"):
            assert redact_terminal_output(raw, command, force=True) == expected
            spill = tmp_path / f"spill-{index}.txt"
            spill.write_text("\x1b[31m" + raw + "\x1b[0m", encoding="utf-8")
            fields = dict(_redact_spill_file(str(spill), len(raw), command))
            assert fields["full_output_path"] == str(spill)
            assert fields["output_total_chars"] == len(raw)
            assert spill.read_text(encoding="utf-8") == expected


def test_large_numeric_output_progresses_through_redaction_and_spill(tmp_path):
    # A child timeout is essential: Python's regex engine holds the GIL, so
    # an in-process timer/thread cannot interrupt the original quadratic scan.
    # Ten seconds is deliberately loose; the old scan of a million digits
    # takes orders of magnitude longer, rather than narrowly missing a budget.
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent("""
            import json
            import sys
            import time
            from pathlib import Path
            from agent.redact import redact_sensitive_text, redact_terminal_output
            from tools.terminal_tool_result import _redact_spill_file

            body = "aB0_-" * 6
            bot_id = "12345678"
            token = bot_id + ":" + body
            masked = bot_id + ":***"
            runs = [
                "x:" + "7" * 1_000_000 + "x",
                "7" * 1_000_000 + ":short!",
                "bot" + "7" * 1_000_000 + ":" + "b" * 29 + "!",
                "x:" + "\\u0667" * 1_000_000 + "x",
            ]
            raw = "\\n".join(runs + ["prefix:" + token + ",bot" + token + "/done"])
            expected = "\\n".join(runs + ["prefix:" + masked + ",bot" + masked + "/done"])
            timings = {}
            for route, operation in (
                ("generic", lambda: redact_sensitive_text(raw, force=True)),
                ("terminal", lambda: redact_terminal_output(raw, "printf synthetic", force=True)),
            ):
                start = time.monotonic()
                assert operation() == expected, route
                timings[route] = time.monotonic() - start
            spill = Path(sys.argv[1]) / "large-synthetic-spill.txt"
            spill.write_text("\\x1b[31m" + raw + "\\x1b[0m", encoding="utf-8")
            start = time.monotonic()
            fields = dict(_redact_spill_file(str(spill), len(raw), "printf synthetic"))
            assert fields["full_output_path"] == str(spill)
            assert fields["output_total_chars"] == len(raw)
            assert spill.read_text(encoding="utf-8") == expected
            timings["spill"] = time.monotonic() - start
            print(json.dumps({"chars": len(raw), "routes": timings}), flush=True)
        """), str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert '"generic"' in result.stdout
    assert '"terminal"' in result.stdout
    assert '"spill"' in result.stdout
    print(result.stdout.strip())
