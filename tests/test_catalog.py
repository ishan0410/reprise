"""Capability catalog: tool definitions, approval gate, invocation through the replay engine."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import admin_post
from scripted_runs import ENV, LOGIN, el, make_policy, run_script, step

from cua.artifact.builder import build_artifact
from cua.artifact.schema import DeclaredConditions
from cua.artifact.store import save_artifact
from cua.catalog import CapabilityNotFound, CapabilityNotInvocable, Catalog, result_for_agent
from cua.cli import catalog as catalog_cli
from cua.evidence.recorder import RunRecorder
from cua.policy.redaction import Redactor
from cua.policy.secrets import SecretStore
from cua.replay.engine import ReplayEngine, ReplayOptions
from cua.surface.playwright_surface import PlaywrightSurface

pytestmark = pytest.mark.browser
CONDITIONS = DeclaredConditions.model_validate_json(Path("policies/mock-portal.conditions.json").read_text())


@pytest.fixture(scope="module")
def surface() -> Iterator[PlaywrightSurface]:
    s = PlaywrightSurface(headless=True, default_timeout_ms=5_000)
    s.open()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(scope="module")
def artifacts_dir(surface: PlaywrightSurface, mock_app_url: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("artifacts")
    admin_post(mock_app_url, "/__admin/reset")
    script = [
        *LOGIN,
        step("type", element=el("textbox", "Member ID"), text="10001"),
        step("click", element=el("button", "Search")),
        step("extract", element=el("cell", "$4,210.55"), name="savings_balance", sensitive=True),
        step("done", summary="done"),
    ]
    trace, _ = run_script(surface, mock_app_url, root / "runs", script, inputs={"member_id": "10001"},
                          capability_name="lookup_member_balance", goal="Look up a member and read their savings balance")
    artifact = build_artifact(trace, base_url=mock_app_url, app="harborview-member-console", conditions=CONDITIONS)
    save_artifact(root, artifact)
    newer = artifact.model_copy(update={"version": "1.1.0"})
    save_artifact(root, newer)
    return root


def test_tool_definitions_follow_function_calling_shape(artifacts_dir: Path) -> None:
    catalog = Catalog(artifacts_dir)
    assert catalog.names() == ["lookup_member_balance"]
    [tool] = catalog.tools()
    assert tool.version == "1.1.0" and tool.status == "draft"  # latest version is listed
    t = tool.as_tool()
    assert t["name"] == "lookup_member_balance"
    assert "MEMBER_NOT_FOUND" in t["description"]
    assert t["parameters"]["required"] == ["member_id"]
    assert t["parameters"]["properties"]["member_id"] == {"type": "string", "examples": ["10001"]}
    assert tool.returns["properties"]["outputs"]["properties"]["savings_balance"] == {"type": "number", "x-sensitive": True}
    with pytest.raises(CapabilityNotFound):
        catalog.describe("nope")


def _engine(surface: PlaywrightSurface, base: str, tmp_path: Path) -> ReplayEngine:
    policy = make_policy(base)
    secrets = SecretStore(policy.secrets.allowed, env=ENV)
    redactor = Redactor(policy.redaction.patterns, secrets=secrets.known_values())
    recorder = RunRecorder(tmp_path, "invoke", redactor=redactor)
    return ReplayEngine(surface=surface, env_policy=policy, secrets=secrets, recorder=recorder, redactor=redactor, options=ReplayOptions(step_timeout_ms=6_000))


def test_unapproved_capability_cannot_be_invoked_until_approved(surface: PlaywrightSurface, mock_app_url: str, tmp_path: Path, artifacts_dir: Path) -> None:
    catalog = Catalog(artifacts_dir)
    with pytest.raises(CapabilityNotInvocable):
        catalog.invoke("lookup_member_balance", {"member_id": "10001"}, _engine(surface, mock_app_url, tmp_path))

    assert catalog_cli.main(["--artifacts-dir", str(artifacts_dir), "approve", "lookup_member_balance", "--by", "alice", "--notes", "reviewed locators"]) == 0
    latest = catalog.latest("lookup_member_balance")
    assert latest.status == "approved" and latest.version == "1.1.0" and latest.provenance.reviewed_by == "alice"

    admin_post(mock_app_url, "/__admin/reset")
    result = catalog.invoke("lookup_member_balance", {"member_id": 10003}, _engine(surface, mock_app_url, tmp_path))
    payload = result_for_agent(result, latest)
    assert payload["status"] == "success" and payload["outputs"] == {"savings_balance": 15780.42}
    assert payload["capability"] == "lookup_member_balance@1.1.0"

    result = catalog.invoke("lookup_member_balance", {"member_id": "99999"}, _engine(surface, mock_app_url, tmp_path))
    payload = result_for_agent(result, latest)
    assert payload["status"] == "business_outcome" and payload["outcome"]["code"] == "MEMBER_NOT_FOUND"
    assert payload["outputs"] == {"found": "false"}


def test_catalog_cli_list_and_describe(artifacts_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert catalog_cli.main(["--artifacts-dir", str(artifacts_dir), "list"]) == 0
    out = capsys.readouterr().out
    assert '"name": "lookup_member_balance"' in out and '"required": [' in out
    assert catalog_cli.main(["--artifacts-dir", str(artifacts_dir), "describe", "lookup_member_balance"]) == 0
    assert '"returns"' in capsys.readouterr().out
    assert catalog_cli.main(["--artifacts-dir", str(artifacts_dir), "describe", "nope"]) == 2
