"""
Build the data behind the GitHub Pages showcase from the committed evidence.

Everything the page shows about runs, artifacts and results is read here from
files under evidence/ and artifacts/, copied verbatim, and written to
docs/data.js. Screenshots are copied byte-for-byte into docs/assets/evidence/.
Nothing is typed in by hand, so the page cannot claim more than the evidence does.

    python docs/build_showcase.py          # regenerate docs/data.js and assets
    python docs/build_showcase.py --check  # exit 1 if the site is out of date

Standard library only; this script is not part of the cua package.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
EVIDENCE = ROOT / "evidence"
ASSETS = DOCS / "assets" / "evidence"
DATA_JS = DOCS / "data.js"

DISCOVERY_RUN = "discovery/discovery-20260911T023106Z-8ace"
DISCOVERY_RUN_2 = "discovery/discovery-20260911T033357Z-8e8e"
ARTIFACT = "artifacts/lookup_member_balance/v1.0.0.json"
ARTIFACT_2 = "artifacts/open_sub_account_review/v1.0.0.json"

REPLAYS = {
    "success": "replay-success/replay-20260911T025249Z-becf",
    "recovery": "replay-success/replay-20260911T025609Z-b1c0",
    "second_capability": "replay-success/replay-20260911T033455Z-b7b8",
    "catalog_invoke": "replay-success/invoke-20260911T033221Z-850b",
    "business_outcome": "replay-error/replay-20260911T025251Z-13a0",
    "failure": "replay-error/replay-20260911T025251Z-ccad",
}
ESCALATION_HUMAN = "escalation/escalation-20260911T031847Z-b328"
ESCALATION_SCRIPTED = "escalation/escalation-20260911T030649Z-21f0"

# From src/cua/cli/replay.py: EXIT = {"success": 0, "business_outcome": 3, "failure": 1}
EXIT_CODES = {"success": 0, "business_outcome": 3, "failure": 1}


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list[Any]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _snapshot(user_text: str) -> str:
    """The accessibility snapshot section of the prompt the model received, verbatim."""
    marker = "Accessibility snapshot:\n"
    return user_text.split(marker, 1)[1].rstrip() if marker in user_text else ""


def _asset(run: str, rel: str, planned: dict[Path, Path]) -> str:
    """Plan a byte-for-byte copy of an evidence screenshot and return its site path."""
    src = EVIDENCE / run / rel
    dest = ASSETS / run.replace("/", "__") / Path(rel).name
    planned[dest] = src
    return dest.relative_to(DOCS).as_posix()


def discovery(run: str, planned: dict[Path, Path], *, copy_screenshots: bool = True) -> dict[str, Any]:
    base = EVIDENCE / run
    trace = _json(base / "trace.json")
    transcript = {t["step"]: t for t in _jsonl(base / "transcript.jsonl")}
    steps = []
    for s in trace["steps"]:
        t = transcript[s["index"]]
        steps.append(
            {
                "index": s["index"],
                "url": s["observation"]["url"],
                "title": s["observation"]["title"],
                "screenshot": (
                    _asset(run, s["observation"]["screenshot"], planned)
                    if copy_screenshots
                    else f"evidence/{run}/{s['observation']['screenshot']}"
                ),
                "screenshot_sent_to_model": t["request"]["screenshot_sent_to_model"],
                "snapshot": _snapshot(t["request"]["user_text"]),
                "tool_calls": t["response"]["tool_calls"],
                "usage": t["response"]["usage"],
                "latency_ms": t["response"]["latency_ms"],
                "provider": t["provider"],
                "model": t["model"],
                "decision": s.get("decision"),
                "element": s.get("element"),
                "result": s["result"],
                "url_after": s["url_after"],
            }
        )
    return {
        "run_id": trace["run_id"],
        "path": f"evidence/{run}",
        "goal": trace["goal"],
        "status": trace["status"],
        "summary": trace["summary"],
        "started_at": trace["started_at"],
        "finished_at": trace["finished_at"],
        "provider_events": trace["provider_events"],
        "total_input_tokens": sum(x["usage"]["input_tokens"] for x in steps),
        "total_output_tokens": sum(x["usage"]["output_tokens"] for x in steps),
        "steps": steps,
        "run_log": _jsonl(base / "run.jsonl"),
    }


def replay(run: str, planned: dict[Path, Path]) -> dict[str, Any]:
    base = EVIDENCE / run
    result = _json(base / "result.json")
    shots = sorted((base / "screenshots").glob("*.png")) if (base / "screenshots").exists() else []
    return {
        "path": f"evidence/{run}",
        "exit_code": EXIT_CODES[result["status"]],
        "result": result,
        "run_log": _jsonl(base / "run.jsonl"),
        "screenshots": [_asset(run, f"screenshots/{p.name}", planned) for p in shots],
    }


def escalation(run: str, planned: dict[Path, Path]) -> dict[str, Any]:
    base = EVIDENCE / run
    data = replay(run, planned)
    data["exit_code"] = EXIT_CODES[data["result"]["status"]]
    data["intervention"] = _json(base / "intervention.json")
    data["resume"] = _json(base / "resume.json")
    return data


def build() -> tuple[str, dict[Path, Path]]:
    planned: dict[Path, Path] = {}
    data = {
        "generated_from": "evidence/ and artifacts/ (see docs/build_showcase.py)",
        "discovery": discovery(DISCOVERY_RUN, planned),
        "discovery_2": discovery(DISCOVERY_RUN_2, planned, copy_screenshots=False),
        "artifact": {"path": ARTIFACT, "json": _json(ROOT / ARTIFACT)},
        "artifact_2": {"path": ARTIFACT_2, "json": _json(ROOT / ARTIFACT_2)},
        "replays": {k: replay(v, planned) for k, v in REPLAYS.items()},
        "escalation": escalation(ESCALATION_HUMAN, planned),
        "escalation_scripted": escalation(ESCALATION_SCRIPTED, planned),
        "policy_toml": (ROOT / "policies/mock-portal.toml").read_text(encoding="utf-8"),
    }
    body = json.dumps(data, indent=1, ensure_ascii=False)
    js = "// Generated by docs/build_showcase.py from committed evidence. Do not edit by hand.\n"
    js += f"window.REPRISE = {body};\n"
    return js, planned


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="fail if docs/data.js or assets are stale")
    args = parser.parse_args()

    js, planned = build()
    if args.check:
        stale = [] if DATA_JS.exists() and DATA_JS.read_text(encoding="utf-8") == js else [DATA_JS]
        stale += [d for d, s in planned.items() if not d.exists() or d.read_bytes() != s.read_bytes()]
        extra = [p for p in ASSETS.rglob("*.png") if p not in planned] if ASSETS.exists() else []
        for p in stale:
            print(f"stale: {p.relative_to(ROOT)}")
        for p in extra:
            print(f"unexpected: {p.relative_to(ROOT)}")
        return 1 if stale or extra else 0

    if ASSETS.exists():
        shutil.rmtree(ASSETS)
    for dest, src in planned.items():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    DATA_JS.write_text(js, encoding="utf-8")
    print(f"wrote {DATA_JS.relative_to(ROOT)} ({len(js):,} bytes) and {len(planned)} screenshots")
    return 0


if __name__ == "__main__":
    sys.exit(main())
