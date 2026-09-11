"""
cua-replay: execute a saved capability deterministically (no LLM).

    cua-replay --artifact artifacts/lookup_member_balance/v1.0.0.json --input member_id=10001

Exit codes: 0 success, 3 business outcome, 1 failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from cua.artifact.store import load_artifact
from cua.escalation.handoff import HeadedBrowserHandoff
from cua.evidence.recorder import RunRecorder, ScreenshotMode
from cua.policy.model import Policy
from cua.policy.redaction import Redactor
from cua.policy.secrets import SecretStore
from cua.replay.engine import ReplayEngine, ReplayOptions
from cua.surface.playwright_surface import PlaywrightSurface

EXIT = {"success": 0, "business_outcome": 3, "failure": 1}


def _kv(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"expected name=value, got {pair!r}")
        name, value = pair.split("=", 1)
        out[name.strip()] = value
    return out


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Deterministic replay of a capability artifact")
    p.add_argument("--artifact", required=True, type=Path)
    p.add_argument("--input", action="append", metavar="NAME=VALUE")
    p.add_argument("--policy", default="policies/mock-portal.toml", type=Path)
    p.add_argument("--base-url", default=None, help="tenant/environment binding; default: the artifact's")
    p.add_argument("--tenant", default=None, help="apply this tenant's step overrides")
    p.add_argument("--allow-risky", action="store_true", help="pre-approve risky steps for this invocation")
    p.add_argument("--require-approved", action="store_true", help="refuse artifacts that are not approved")
    p.add_argument("--headed", action="store_true")
    p.add_argument(
        "--escalate",
        action="store_true",
        help="on a condition replay cannot handle, pause and hand the live browser to a human (implies --headed)",
    )
    p.add_argument("--escalation-timeout", type=float, default=600.0, help="seconds to wait for cua-resume")
    p.add_argument("--evidence-dir", type=Path, default=Path("evidence/replay"))
    p.add_argument("--screenshots", choices=["full", "on_failure", "none"], default="on_failure")
    p.add_argument("--show-sensitive", action="store_true", help="print sensitive outputs in the clear")
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv)
    artifact = load_artifact(args.artifact)
    policy = Policy.load(args.policy)
    secrets = SecretStore(policy.secrets.allowed)
    redactor = Redactor(policy.redaction.patterns, secrets=secrets.known_values())
    screenshot_mode: ScreenshotMode = args.screenshots
    recorder = RunRecorder(args.evidence_dir, "replay", redactor=redactor, screenshot_mode=screenshot_mode)
    options = ReplayOptions(
        allow_risky=args.allow_risky,
        require_approved=args.require_approved,
        base_url=args.base_url or os.environ.get("TARGET_APP_URL") or None,
        tenant=args.tenant,
    )
    headed = args.headed or args.escalate
    surface = PlaywrightSurface(headless=not headed, viewport=artifact.target.viewport)
    surface.open()
    try:
        handoff = None
        if args.escalate:
            handoff = HeadedBrowserHandoff(
                surface=surface, control=surface.control, recorder=recorder, timeout_s=args.escalation_timeout
            )
        engine = ReplayEngine(
            surface=surface,
            env_policy=policy,
            secrets=secrets,
            recorder=recorder,
            redactor=redactor,
            options=options,
            escalate=handoff,
        )
        result = engine.run(artifact, _kv(args.input))
    finally:
        surface.close()
        recorder.close()

    sensitive = [o.name for o in artifact.outputs if o.sensitive]
    shown = result if args.show_sensitive else result.redacted(sensitive)
    if args.json:
        print(shown.model_dump_json(indent=2, exclude_none=True))
    else:
        print(f"status    : {result.status}")
        if result.outcome:
            print(f"outcome   : {result.outcome.code} — {result.outcome.description}")
        if result.error:
            e = result.error
            print(f"error     : {e.code} [{e.category}] at {e.step_id}: {e.message}")
            if e.expected:
                print(f"  expected: {e.expected}")
            if e.observed:
                print(f"  observed: {e.observed}")
            if e.screenshot:
                print(f"  evidence: {recorder.dir / e.screenshot}")
        print(f"outputs   : {json.dumps(shown.outputs)}")
        print(f"steps     : {len(result.steps)} ({sum(s.status == 'ok' for s in result.steps)} ok)")
        if result.recoveries:
            print("recoveries: " + "; ".join(f"{r.code}@{r.step_id} ({r.action})" for r in result.recoveries))
        if result.drift.detected:
            print("drift     : " + "; ".join(f"{d['step_id']} via candidate {d['candidate_index']}" for d in result.drift.fallback_steps))
        if result.control_events:
            print("control   :")
            for ev in result.control_events:
                print(f"  {ev.at}  {ev.holder:<10} {ev.event:<13} {ev.detail}")
        print(f"evidence  : {recorder.dir}")
    return EXIT[result.status]


if __name__ == "__main__":
    sys.exit(main())
