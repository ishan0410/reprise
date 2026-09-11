"""
cua-resume: the operator's half of the handoff protocol.

    cua-resume --run evidence/replay/<run_id> --resolution skip_step --operator alice --notes "dismissed the notice"

Writes resume.json into the run directory; the paused automation picks it up and continues.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cua.escalation.handoff import INTERVENTION_FILE, write_resume_signal


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Hand control back to a paused automation run")
    p.add_argument("--run", required=True, type=Path, help="the run's evidence directory")
    p.add_argument("--resolution", required=True, choices=["retry_step", "skip_step", "restart_phase", "abort"])
    p.add_argument("--operator", required=True, help="who is handing control back")
    p.add_argument("--notes", default="", help="what you did / why")
    p.add_argument("--show", action="store_true", help="print the pending intervention request first")
    args = p.parse_args(argv)

    pending = args.run / INTERVENTION_FILE
    if args.show and pending.exists():
        print(json.dumps(json.loads(pending.read_text()), indent=2))
    try:
        path = write_resume_signal(args.run, args.resolution, args.operator, args.notes)
    except FileNotFoundError as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 2
    print(f"resume signal written: {path} ({args.resolution} by {args.operator})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
