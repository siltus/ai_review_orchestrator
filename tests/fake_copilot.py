"""Fake Copilot CLI used by integration tests.

Emits scripted JSONL on stdout and writes a canned review/fixes file to
disk. Accepts the same subset of flags aidor sends so argv parsing
roughly matches real copilot.

Scripts are driven by environment variables so a test can compose
behavior without modifying this file:

    FAKE_COPILOT_EMIT_FILE       path to write a canned file to
    FAKE_COPILOT_EMIT_CONTENT    content to write (default: a minimal
                                  review with a CLEAN+READY footer)
    FAKE_COPILOT_DELAY_S         seconds to sleep before exiting
    FAKE_COPILOT_EXIT_CODE       exit code (default 0)
    FAKE_COPILOT_STOP_REASON     value for the final stopReason event
                                  (default "end_turn")
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

DEFAULT_REVIEW = """\
# Review

All requirements met.

<!-- AIDOR:STATUS=CLEAN -->
<!-- AIDOR:ISSUES={"critical":0,"major":0,"minor":0,"nit":0} -->
<!-- AIDOR:PRODUCTION_READY=true -->
"""


def _emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-p", dest="prompt", default="")
    parser.add_argument("--agent", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--autopilot", action="store_true")
    parser.add_argument("--output-format", default="json")
    parser.add_argument("--share", default="")
    parser.add_argument("--continue", dest="resume", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    # Absorb --allow-tool / --deny-tool silently.
    parser.add_argument("--allow-tool", action="append", default=[])
    parser.add_argument("--deny-tool", action="append", default=[])
    args, _unknown = parser.parse_known_args(argv if argv is not None else sys.argv[1:])

    _emit({"type": "start", "agent": args.agent, "model": args.model})

    emit_file = os.environ.get("FAKE_COPILOT_EMIT_FILE")
    content = os.environ.get("FAKE_COPILOT_EMIT_CONTENT", DEFAULT_REVIEW)
    if emit_file:
        try:
            with open(emit_file, "w", encoding="utf-8") as f:
                f.write(content)
            _emit({"type": "tool", "name": "write_file", "path": emit_file})
        except OSError as exc:
            _emit({"type": "error", "message": str(exc)})

    delay = float(os.environ.get("FAKE_COPILOT_DELAY_S", "0"))
    if delay:
        time.sleep(delay)

    stop_reason = os.environ.get("FAKE_COPILOT_STOP_REASON", "end_turn")
    _emit({"type": "end", "stopReason": stop_reason})

    return int(os.environ.get("FAKE_COPILOT_EXIT_CODE", "0"))


if __name__ == "__main__":
    sys.exit(main())
