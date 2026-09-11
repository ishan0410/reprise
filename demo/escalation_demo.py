"""
Escalation demo: replay hits a condition the capability never declared, hands
the live browser to a human, and resumes after the human fixes it.

    # terminal 1
    cua-target-app
    # terminal 2 (a real operator: the browser window opens; dismiss the dialog, then run cua-resume)
    python demo/escalation_demo.py --artifact artifacts/lookup_member_balance/v1.0.0.json
    # or unattended, with the operator simulated by a script acting on the same live page:
    python demo/escalation_demo.py --artifact ... --simulate-operator

The injected condition is a "password expires in 3 days" interstitial with a
"Remind Me Later" button. It is deliberately absent from the capability's
declared recoverables, so replay cannot handle it alone.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

from cua.artifact.store import load_artifact
from cua.escalation.handoff import INTERVENTION_FILE, RESUME_FILE, HeadedBrowserHandoff, write_resume_signal
from cua.evidence.recorder import RunRecorder
from cua.policy.model import Policy
from cua.policy.redaction import Redactor
from cua.policy.secrets import SecretStore
from cua.replay.engine import ReplayEngine, ReplayOptions
from cua.surface.playwright_surface import PlaywrightSurface


def inject(base_url: str, kind: str) -> None:
    req = urllib.request.Request(
        f"{base_url}/__admin/inject",
        data=json.dumps({"kind": kind}).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5):
        pass


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description="Human handoff demo")
    p.add_argument("--artifact", type=Path, default=Path("artifacts/lookup_member_balance/v1.0.0.json"))
    p.add_argument("--input", action="append", default=["member_id=10001"], metavar="NAME=VALUE")
    p.add_argument("--policy", type=Path, default=Path("policies/mock-portal.toml"))
    p.add_argument("--evidence-dir", type=Path, default=Path("evidence/escalation"))
    p.add_argument("--simulate-operator", action="store_true", help="a script plays the operator on the same live page")
    p.add_argument("--timeout", type=float, default=600.0)
    args = p.parse_args(argv)

    base_url = os.environ.get("TARGET_APP_URL", "http://localhost:4000").rstrip("/")
    inputs = dict(kv.split("=", 1) for kv in args.input)
    artifact = load_artifact(args.artifact)
    assert not any(r.code == "PASSWORD_EXPIRY" for r in artifact.recoverables), "the artifact must not know this dialog"

    policy = Policy.load(args.policy)
    secrets = SecretStore(policy.secrets.allowed)
    redactor = Redactor(policy.redaction.patterns, secrets=secrets.known_values())
    recorder = RunRecorder(args.evidence_dir, "escalation", redactor=redactor, screenshot_mode="on_failure")
    surface = PlaywrightSurface(headless=args.simulate_operator, viewport=artifact.target.viewport)
    surface.open()

    simulated = {"acted": False}

    def scripted_operator(elapsed: float) -> None:
        """Stands in for a person at the keyboard. Acts on the SAME live page, then signals resume."""
        if simulated["acted"] or elapsed < 1.0:
            return
        simulated["acted"] = True
        request = json.loads((recorder.dir / INTERVENTION_FILE).read_text())
        print(f"[operator] intervention: {request['error_code']} at {request['step_id']} on {request['title']!r}")
        surface.page.get_by_role("button", name="Remind Me Later").click()
        surface.page.wait_for_load_state("load")
        print(f"[operator] dismissed the dialog; page is now {surface.page.title()!r}")
        write_resume_signal(recorder.dir, "skip_step", "scripted-operator", "dismissed the password-expiry dialog by hand")
        print(f"[operator] wrote {recorder.dir / RESUME_FILE}")

    handoff = HeadedBrowserHandoff(
        surface=surface,
        control=surface.control,
        recorder=recorder,
        timeout_s=args.timeout,
        on_wait=scripted_operator if args.simulate_operator else None,
    )
    try:
        inject(base_url, "unexpected_dialog")
        engine = ReplayEngine(
            surface=surface, env_policy=policy, secrets=secrets, recorder=recorder, redactor=redactor,
            escalate=handoff, options=ReplayOptions(base_url=base_url, step_timeout_ms=4_000),
        )
        result = engine.run(artifact, inputs)
    finally:
        surface.close()
        recorder.close()

    print(f"\nstatus   : {result.status}")
    if result.error:
        print(f"error    : {result.error.code} at {result.error.step_id}: {result.error.message}")
    print(f"outputs  : {json.dumps(result.redacted([o.name for o in artifact.outputs if o.sensitive]).outputs)}")
    print("control  :")
    for ev in result.control_events:
        print(f"  {ev.at}  {ev.holder:<10} {ev.event:<13} {ev.detail}")
    print(f"evidence : {recorder.dir}")
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
