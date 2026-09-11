"""Artifact schema and builder tests: pure derivation rules, plus a full trace -> artifact build."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from scripted_runs import LOGIN, el, run_script, step

from cua.agent.trace import ModelSummary, ObservationSummary, StepResult, Trace, TraceStep
from cua.artifact.builder import (
    build_artifact,
    derive_target,
    infer_output_type,
    parameterise,
    split_phases,
    template_url,
)
from cua.artifact.schema import CapabilityArtifact, DeclaredConditions
from cua.artifact.store import load_artifact, next_version, save_artifact, write_json_schema
from cua.artifact.targets import BBox, RecordedElement
from cua.surface.playwright_surface import PlaywrightSurface

CONDITIONS = Path("policies/mock-portal.conditions.json")


# ---------------------------------------------------------- derivation rules


def test_control_locator_chain_is_role_then_text_then_css_then_bbox() -> None:
    element = RecordedElement(role="button", name="Search", tag="input", css="form > input:nth-of-type(2)", bbox=BBox(x=1, y=2, width=3, height=4))
    t = derive_target(element, description="Search button")
    assert [c.by for c in t.candidates] == ["role", "text", "css", "bbox"]
    assert t.candidates[0].role == "button" and t.candidates[0].name == "Search" and t.candidates[0].exact
    assert t.candidates[1].text == "Search"
    assert t.recorded == element


def test_textbox_locator_has_no_text_candidate() -> None:
    element = RecordedElement(role="textbox", name="Member ID", tag="input", css="input[name=\"memberId\"]")
    assert [c.by for c in derive_target(element, description="x").candidates] == ["role", "css"]


def test_extracted_value_uses_anchor_relative_locator_not_its_own_text() -> None:
    element = RecordedElement(
        role="cell", name="$4,210.55", tag="td", text="$4,210.55", css="td:nth-of-type(4)",
        row_cells=["Savings", "S-10001-01", "Primary Savings", "$4,210.55"], cell_index=3,
    )
    t = derive_target(element, description="balance", value_independent=True)
    first = t.candidates[0]
    assert first.by == "role" and first.role == "cell" and first.nth == 3 and first.name is None
    assert first.within and first.within.name == "Savings" and first.within.exact and first.within.ancestor_role == "row"
    assert all(c.name != "$4,210.55" for c in t.candidates)
    assert [c.by for c in t.candidates] == ["role", "css"]


def test_anchor_skips_numeric_money_and_date_cells() -> None:
    element = RecordedElement(role="cell", tag="td", row_cells=["2015-03-12", "42", "$9.00", "Rainy Day Fund", "$1.00"], cell_index=4)
    t = derive_target(element, description="x", value_independent=True)
    assert t.candidates[0].within is not None and t.candidates[0].within.name == "Rainy Day Fund"


def test_parameterise_and_template_url() -> None:
    inputs = {"member_id": "10001", "prefix": "100", "ssn": "123456789"}
    assert parameterise("10001", inputs, set()) == "{{input:member_id}}"  # longest literal wins over "100"
    assert parameterise("100", inputs, set()) == "{{input:prefix}}"
    assert parameterise("123456789", inputs, {"ssn"}) == "123456789"  # sensitive values are never matched
    assert template_url("http://h:1/members/10001/x", "http://h:1", inputs, set()) == "{{base_url}}/members/{{input:member_id}}/x"
    assert template_url("http://other/members/1", "http://h:1", inputs, set()) == "http://other/members/1"


def test_infer_output_type() -> None:
    assert infer_output_type("$4,210.55") == "money"
    assert infer_output_type("42") == "integer"
    assert infer_output_type("3.50") == "number"
    assert infer_output_type("Sample, Jane") == "string"


def _trace_step(i: int, tool: str, url: str, url_after: str, **args: object) -> TraceStep:
    return TraceStep(
        index=i,
        observation=ObservationSummary(url=url, title="t"),
        model=ModelSummary(provider="scripted", model="m"),
        tool=tool,
        arguments=dict(args),
        result=StepResult(status="ok"),
        url_after=url_after,
    )


def test_split_phases_ends_bootstrap_at_first_navigating_click_after_a_secret() -> None:
    steps = [
        _trace_step(0, "type", "http://h/login", "http://h/login", text="{{secret:U}}"),
        _trace_step(1, "type", "http://h/login", "http://h/login", text="{{secret:P}}"),
        _trace_step(2, "click", "http://h/login", "http://h/members/search"),
        _trace_step(3, "type", "http://h/members/search", "http://h/members/search", text="10001"),
    ]
    assert split_phases(steps) == 3
    assert split_phases(steps[3:]) == 0  # no secret typed: everything is main


def test_non_success_trace_cannot_become_a_capability() -> None:
    trace = Trace(run_id="r", goal="g", start_url="http://h/login", capability_name="c", inputs={}, status="stuck")
    with pytest.raises(ValueError):
        build_artifact(trace, base_url="http://h", app="a")


# ---------------------------------------------------------- full build


@pytest.fixture(scope="module")
def surface() -> Iterator[PlaywrightSurface]:
    s = PlaywrightSurface(headless=True, default_timeout_ms=5_000)
    s.open()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(scope="module")
def lookup_artifact(surface: PlaywrightSurface, mock_app_url: str, tmp_path_factory: pytest.TempPathFactory) -> CapabilityArtifact:
    script = [
        *LOGIN,
        step("type", element=el("textbox", "Member ID"), text="10001", reason="Enter the member ID to look up"),
        step("click", element=el("button", "Search"), reason="Run the search"),
        step("extract", element=el("cell", "$4,210.55"), name="savings_balance", sensitive=True, reason="Read the savings balance"),
        step("done", summary="Read the balance."),
    ]
    tmp = tmp_path_factory.mktemp("build")
    trace, run_dir = run_script(
        surface, mock_app_url, tmp, script, goal="Look up a member and read their savings balance",
        inputs={"member_id": "10001"}, capability_name="lookup_member_balance",
    )
    assert trace.status == "success"
    conditions = DeclaredConditions.model_validate_json(CONDITIONS.read_text())
    return build_artifact(
        trace, base_url=mock_app_url, app="harborview-member-console", app_version="v4.2",
        conditions=conditions, evidence_dir=str(run_dir),
    )


@pytest.mark.browser
def test_build_phases_steps_and_checkpoints(lookup_artifact: CapabilityArtifact) -> None:
    a = lookup_artifact
    assert a.id == "cap_lookup_member_balance" and a.version == "1.0.0" and a.status == "draft"
    assert [s.phase for s in a.steps] == ["bootstrap"] * 4 + ["main"] * 4
    assert [s.action for s in a.steps] == ["navigate", "type", "type", "click", "navigate", "type", "click", "extract"]
    assert a.steps[0].url == "{{base_url}}/login"
    assert a.steps[1].value == "{{secret:TARGET_USERNAME}}" and a.steps[1].description == "Enter the staff username"
    sign_in = a.steps[3]
    assert sign_in.checkpoint and sign_in.checkpoint.url_pattern == "/members/search"
    assert sign_in.checkpoint.title_contains == "Member Lookup"
    assert a.steps[4].url == "{{base_url}}/members/search"  # explicit main-phase entry
    assert a.steps[5].value == "{{input:member_id}}"  # literal 10001 was parameterised
    search = a.steps[6]
    assert search.checkpoint and search.checkpoint.url_pattern == "/members/{{input:member_id}}"
    assert search.checkpoint.title_contains == "Member Detail"
    extract = a.steps[7]
    assert extract.output == "savings_balance" and extract.target and extract.target.candidates[0].within
    assert extract.target.candidates[0].within.name == "Savings"
    assert a.success.url_pattern == "/members/{{input:member_id}}" and a.success.outputs_required == ["savings_balance"]


@pytest.mark.browser
def test_build_contract_conditions_and_policy(lookup_artifact: CapabilityArtifact) -> None:
    a = lookup_artifact
    assert [i.name for i in a.inputs] == ["member_id"] and a.inputs[0].example == "10001"
    balance = a.output("savings_balance")
    assert balance and balance.type == "money" and balance.sensitive and balance.extracted_by == "s8"
    assert a.output("found") is not None  # declared by the MEMBER_NOT_FOUND outcome's `sets`
    assert [o.code for o in a.outcomes] == ["MEMBER_NOT_FOUND", "INVALID_MEMBER_ID", "VALIDATION_ERROR"]
    assert [r.code for r in a.recoverables] == ["SESSION_EXPIRED", "SYSTEM_NOTICE"]
    assert [f.code for f in a.failures] == ["PERMISSION_DENIED", "INVALID_LOGIN", "APP_ERROR"]
    assert a.policy.allowlist.origins == [a.target.base_url]
    assert set(a.policy.allowlist.path_patterns) == {"/", "/login", "/members/search", "/members/*"}
    assert a.policy.allowlist.action_types == ["navigate", "type", "click", "extract"]
    assert a.policy.secrets.allowed == ["TARGET_USERNAME", "TARGET_PASSWORD"]
    assert a.policy.risk.mode == "confirm"
    assert a.provenance.models == ["scripted/scripted-v1"] and a.provenance.evidence_dir


@pytest.mark.browser
def test_artifact_round_trips_and_versions(lookup_artifact: CapabilityArtifact, tmp_path: Path) -> None:
    path = save_artifact(tmp_path, lookup_artifact)
    assert path == tmp_path / "lookup_member_balance" / "v1.0.0.json"
    text = path.read_text()
    assert "demo-pass-2024" not in text and "$4,210.55" not in text and "[ref=" not in text
    assert "{{secret:TARGET_PASSWORD}}" in text
    loaded = load_artifact(path)
    assert loaded == lookup_artifact
    assert next_version(tmp_path, "lookup_member_balance") == "1.1.0"
    assert next_version(tmp_path, "never_recorded") == "1.0.0"


def test_json_schema_export(tmp_path: Path) -> None:
    out = write_json_schema(tmp_path / "schema.json")
    schema = json.loads(out.read_text())
    assert schema["title"] == "CapabilityArtifact"
    assert {"inputs", "outputs", "steps", "success", "outcomes", "recoverables", "failures", "policy", "provenance"} <= set(schema["properties"])
