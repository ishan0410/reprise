"""
cua-catalog: the agent-facing capability interface.

    cua-catalog list                       # tool definitions for every capability
    cua-catalog describe lookup_member_balance
    cua-catalog approve lookup_member_balance --by alice     # draft -> approved
    cua-catalog invoke lookup_member_balance --arg member_id=10001
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from cua.artifact.store import save_artifact
from cua.catalog import CapabilityNotFound, CapabilityNotInvocable, Catalog, result_for_agent
from cua.evidence.recorder import RunRecorder
from cua.policy.model import Policy
from cua.policy.redaction import Redactor
from cua.policy.secrets import SecretStore
from cua.replay.engine import ReplayEngine, ReplayOptions
from cua.surface.playwright_surface import PlaywrightSurface


def _kv(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"expected name=value, got {pair!r}")
        name, value = pair.split("=", 1)
        out[name.strip()] = value
    return out


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Agent-facing capability catalog")
    p.add_argument("--artifacts-dir", type=Path, default=Path("artifacts"))
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="print tool definitions (function-calling shape)")
    d = sub.add_parser("describe", help="print one capability's tool definition and return schema")
    d.add_argument("name")
    a = sub.add_parser("approve", help="mark the latest version of a capability as approved for unattended replay")
    a.add_argument("name")
    a.add_argument("--by", required=True, help="reviewer")
    a.add_argument("--notes", default="")
    i = sub.add_parser("invoke", help="invoke a capability with typed arguments (deterministic replay)")
    i.add_argument("name")
    i.add_argument("--arg", action="append", metavar="NAME=VALUE")
    i.add_argument("--policy", default="policies/mock-portal.toml", type=Path)
    i.add_argument("--allow-draft", action="store_true", help="invoke even if not approved (development only)")
    i.add_argument("--allow-risky", action="store_true")
    i.add_argument("--headed", action="store_true")
    i.add_argument("--evidence-dir", type=Path, default=Path("evidence/replay"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv)
    catalog = Catalog(args.artifacts_dir, require_approved=not getattr(args, "allow_draft", False))

    if args.command == "list":
        print(json.dumps([{**t.as_tool(), "version": t.version, "status": t.status} for t in catalog.tools()], indent=2))
        return 0
    if args.command == "describe":
        try:
            t = catalog.describe(args.name)
        except CapabilityNotFound:
            print(f"error: no capability named {args.name!r}", file=sys.stderr)
            return 2
        print(json.dumps({**t.as_tool(), "returns": t.returns, "version": t.version, "status": t.status}, indent=2))
        return 0
    if args.command == "approve":
        try:
            artifact = catalog.latest(args.name)
        except CapabilityNotFound:
            print(f"error: no capability named {args.name!r}", file=sys.stderr)
            return 2
        artifact.status = "approved"
        artifact.provenance.reviewed_by = args.by
        artifact.provenance.review_notes = f"{datetime.now(UTC).isoformat()}: {args.notes}".rstrip(": ")
        path = save_artifact(args.artifacts_dir, artifact)
        print(f"approved {artifact.name} v{artifact.version} by {args.by} -> {path}")
        return 0

    # invoke
    policy = Policy.load(args.policy)
    secrets = SecretStore(policy.secrets.allowed)
    redactor = Redactor(policy.redaction.patterns, secrets=secrets.known_values())
    try:
        artifact = catalog.latest(args.name)
    except CapabilityNotFound:
        print(f"error: no capability named {args.name!r}", file=sys.stderr)
        return 2
    if catalog.require_approved and artifact.status != "approved":
        print(f"error: {args.name} v{artifact.version} is {artifact.status}; approve it before agents may invoke it", file=sys.stderr)
        return 4
    recorder = RunRecorder(args.evidence_dir, "invoke", redactor=redactor, screenshot_mode="on_failure")
    surface = PlaywrightSurface(headless=not args.headed, viewport=artifact.target.viewport)
    surface.open()
    try:
        engine = ReplayEngine(
            surface=surface, env_policy=policy, secrets=secrets, recorder=recorder, redactor=redactor,
            options=ReplayOptions(allow_risky=args.allow_risky, base_url=os.environ.get("TARGET_APP_URL") or None),
        )
        result = catalog.invoke(args.name, _kv(args.arg), engine)
    except CapabilityNotInvocable as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 4
    finally:
        surface.close()
        recorder.close()
    print(json.dumps(result_for_agent(result, artifact), indent=2))
    return {"success": 0, "business_outcome": 3, "failure": 1}[result.status]


if __name__ == "__main__":
    sys.exit(main())
