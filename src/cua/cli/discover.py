"""
cua-discover: run an LLM-driven discovery of a goal against the target app.

    cua-discover --goal "Look up member 10001 and read the savings balance" \\
        --name lookup_member_balance --input member_id=10001

Evidence lands in evidence/discovery/<run_id>/.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from cua.agent.llm import FallbackProvider, LLMProvider
from cua.agent.loop import DiscoveryAgent, DiscoveryRequest
from cua.artifact.builder import build_artifact
from cua.artifact.schema import DeclaredConditions
from cua.artifact.store import next_version, save_artifact
from cua.evidence.recorder import RunRecorder, ScreenshotMode
from cua.policy.gate import PolicyGate
from cua.policy.model import Policy
from cua.policy.redaction import Redactor
from cua.policy.secrets import SecretStore
from cua.surface.playwright_surface import PlaywrightSurface


def _kv(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"expected name=value, got {pair!r}")
        name, value = pair.split("=", 1)
        out[name.strip()] = value
    return out


def build_providers(names: list[str], script_path: Path | None) -> list[LLMProvider]:
    providers: list[LLMProvider] = []
    for name in names:
        if name == "gemini":
            from cua.agent.providers.gemini import DEFAULT_MODEL, GeminiProvider

            key = os.environ.get("GEMINI_API_KEY")
            if not key:
                raise SystemExit("GEMINI_API_KEY is not set (see .env.example)")
            providers.append(GeminiProvider(key, os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL))
        elif name == "groq":
            from cua.agent.providers.groq_provider import DEFAULT_MODEL, GroqProvider

            key = os.environ.get("GROQ_API_KEY")
            if not key:
                raise SystemExit("GROQ_API_KEY is not set (see .env.example)")
            providers.append(
                GroqProvider(
                    key,
                    os.environ.get("GROQ_MODEL") or DEFAULT_MODEL,
                    supports_vision=os.environ.get("GROQ_VISION", "0") == "1",
                )
            )
        elif name == "scripted":
            from cua.agent.providers.scripted import ScriptedProvider

            if script_path is None:
                raise SystemExit("--script is required with --provider scripted")
            providers.append(ScriptedProvider(json.loads(script_path.read_text())))
        else:
            raise SystemExit(f"unknown provider {name!r}")
    return providers


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="LLM-driven discovery run")
    p.add_argument("--goal", required=True)
    p.add_argument("--name", default="capability", help="capability name for the resulting artifact")
    p.add_argument("--url", default=None, help="start URL (default: $TARGET_APP_URL/login)")
    p.add_argument("--input", action="append", metavar="NAME=VALUE", help="task input (repeatable)")
    p.add_argument("--sensitive-input", action="append", metavar="NAME=VALUE", help="input hidden from the model")
    p.add_argument("--policy", default="policies/mock-portal.toml", type=Path)
    p.add_argument(
        "--conditions",
        type=Path,
        default=Path("policies/mock-portal.conditions.json"),
        help="reviewed per-app outcomes/recoverables/failures merged into the artifact",
    )
    p.add_argument("--app", default="harborview-member-console", help="logical application id for the artifact")
    p.add_argument("--app-version", default="v4.2")
    p.add_argument("--artifacts-dir", type=Path, default=Path("artifacts"))
    p.add_argument("--provider", default="gemini,groq", help="comma-separated chain: gemini,groq | scripted")
    p.add_argument("--script", type=Path, default=None, help="tool-call script for --provider scripted")
    p.add_argument("--headed", action="store_true", help="show the browser window")
    p.add_argument("--max-steps", type=int, default=25)
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument("--evidence-dir", type=Path, default=Path("evidence/discovery"))
    p.add_argument("--screenshots", choices=["full", "on_failure", "none"], default="full")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv)
    base_url = os.environ.get("TARGET_APP_URL", "http://localhost:4000").rstrip("/")
    start_url = args.url or f"{base_url}/login"

    policy = Policy.load(args.policy)
    gate = PolicyGate(policy)
    secrets = SecretStore(policy.secrets.allowed)
    redactor = Redactor(policy.redaction.patterns, secrets=secrets.known_values())
    sensitive_inputs = _kv(args.sensitive_input)
    for name, value in sensitive_inputs.items():
        redactor.add_sensitive(name, value)
    inputs = {**_kv(args.input), **sensitive_inputs}

    screenshot_mode: ScreenshotMode = args.screenshots
    recorder = RunRecorder(args.evidence_dir, "discovery", redactor=redactor, screenshot_mode=screenshot_mode)
    providers = build_providers([n.strip() for n in args.provider.split(",") if n.strip()], args.script)

    surface = PlaywrightSurface(headless=not args.headed)
    surface.open()
    try:
        agent = DiscoveryAgent(
            surface=surface, llm=providers[0], gate=gate, secrets=secrets, recorder=recorder, redactor=redactor
        )
        if len(providers) > 1:
            agent.llm = FallbackProvider(providers, on_fallback=agent.on_provider_fallback)
        trace = agent.run(
            DiscoveryRequest(
                goal=args.goal,
                start_url=start_url,
                capability_name=args.name,
                inputs=inputs,
                sensitive_inputs=frozenset(sensitive_inputs),
                max_steps=args.max_steps,
                timeout_s=args.timeout,
            )
        )
    finally:
        surface.close()
        recorder.close()

    print(f"status   : {trace.status}")
    print(f"summary  : {trace.summary}")
    print(f"steps    : {len(trace.steps)}")
    shown = {k: ("[sensitive]" if k in trace.sensitive_outputs else v) for k, v in trace.outputs.items()}
    print(f"outputs  : {json.dumps(shown)}")
    print(f"evidence : {recorder.dir}")
    if trace.status != "success":
        return 1

    conditions = DeclaredConditions()
    if args.conditions and args.conditions.exists():
        conditions = DeclaredConditions.model_validate_json(args.conditions.read_text(encoding="utf-8"))
    try:
        artifact = build_artifact(
            trace,
            base_url=base_url,
            app=args.app,
            app_version=args.app_version,
            conditions=conditions,
            version=next_version(args.artifacts_dir, args.name),
            evidence_dir=str(recorder.dir),
        )
    except ValueError as ex:
        print(f"artifact : NOT built ({ex})")
        return 2
    path = save_artifact(args.artifacts_dir, artifact, redactor=redactor)
    print(f"artifact : {path}  ({artifact.id} v{artifact.version}, {len(artifact.steps)} steps, status={artifact.status})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
