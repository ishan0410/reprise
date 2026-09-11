"""
Replay engine tests: artifacts are built from scripted discovery runs, then
replayed deterministically under every runtime condition the mock app can
produce. These pin down the result contract.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import admin_post
from scripted_runs import ENV, LOGIN, el, make_policy, run_script, step

from cua.artifact.builder import build_artifact
from cua.artifact.schema import CapabilityArtifact, DeclaredConditions, Step
from cua.artifact.targets import Locator, TargetDescriptor
from cua.evidence.recorder import RunRecorder
from cua.policy.redaction import Redactor
from cua.policy.secrets import SecretStore
from cua.replay.engine import ReplayEngine, ReplayOptions
from cua.replay.result import ReplayResult
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


def _build(surface: PlaywrightSurface, base: str, tmp: Path, script: list[dict], name: str, inputs: dict[str, str]) -> CapabilityArtifact:
    trace, _ = run_script(surface, base, tmp, script, inputs=inputs, capability_name=name, goal=f"goal for {name}")
    assert trace.status == "success", trace.summary
    return build_artifact(trace, base_url=base, app="harborview-member-console", conditions=CONDITIONS)


@pytest.fixture(scope="module")
def lookup(surface: PlaywrightSurface, mock_app_url: str, tmp_path_factory: pytest.TempPathFactory) -> CapabilityArtifact:
    admin_post(mock_app_url, "/__admin/reset")
    script = [
        *LOGIN,
        step("type", element=el("textbox", "Member ID"), text="10001"),
        step("click", element=el("button", "Search")),
        step("extract", element=el("cell", "$4,210.55"), name="savings_balance", sensitive=True),
        step("done", summary="done"),
    ]
    return _build(surface, mock_app_url, tmp_path_factory.mktemp("lookup"), script, "lookup_member_balance", {"member_id": "10001"})


@pytest.fixture(scope="module")
def open_sub_account(surface: PlaywrightSurface, mock_app_url: str, tmp_path_factory: pytest.TempPathFactory) -> CapabilityArtifact:
    admin_post(mock_app_url, "/__admin/reset")
    script = [
        *LOGIN,
        step("type", element=el("textbox", "Member ID"), text="10002"),
        step("click", element=el("button", "Search")),
        step("click", element=el("link", "Open Sub-Account")),
        step("select", element=el("combobox", "Account Type"), option="Checking"),
        step("type", element=el("textbox", "Nickname"), text="Bills"),
        step("type", element=el("textbox", "Initial Deposit"), text="25.00"),
        step("click", element=el("button", "Review")),
        step("done", summary="reached review"),
    ]
    inputs = {"member_id": "10002", "nickname": "Bills", "initial_deposit": "25.00"}
    return _build(surface, mock_app_url, tmp_path_factory.mktemp("sub"), script, "open_sub_account_review", inputs)


def replay(
    surface: PlaywrightSurface,
    base: str,
    tmp_path: Path,
    artifact: CapabilityArtifact,
    inputs: dict[str, str],
    *,
    options: ReplayOptions | None = None,
) -> tuple[ReplayResult, Path]:
    admin_post(base, "/__admin/reset")
    policy = make_policy(base)
    secrets = SecretStore(policy.secrets.allowed, env=ENV)
    redactor = Redactor(policy.redaction.patterns, secrets=secrets.known_values())
    recorder = RunRecorder(tmp_path, "replay", redactor=redactor, screenshot_mode="on_failure")
    engine = ReplayEngine(
        surface=surface, env_policy=policy, secrets=secrets, recorder=recorder, redactor=redactor,
        options=options or ReplayOptions(step_timeout_ms=6_000),
    )
    result = engine.run(artifact.model_copy(deep=True), inputs)
    recorder.close()
    return result, recorder.dir


# ------------------------------------------------------------------ success


def test_replay_success_with_typed_outputs_and_evidence(surface: PlaywrightSurface, mock_app_url: str, tmp_path: Path, lookup: CapabilityArtifact) -> None:
    result, run_dir = replay(surface, mock_app_url, tmp_path, lookup, {"member_id": "10001"})
    assert result.status == "success", result.error
    assert result.outputs == {"savings_balance": 4210.55}
    assert result.outcome is None and result.error is None
    assert [s.status for s in result.steps] == ["ok"] * 8
    assert all(s.candidate_index == 0 for s in result.steps if s.candidate_index is not None)
    assert not result.drift.detected and result.recoveries == []
    saved = json.loads((run_dir / "result.json").read_text())
    assert saved["outputs"] == {"savings_balance": "[REDACTED:savings_balance]"}
    assert "4210.55" not in (run_dir / "run.jsonl").read_text()
    assert list((run_dir / "screenshots").iterdir()) == []  # on_failure mode, nothing failed


def test_replay_is_parameterised_not_a_recording_of_one_member(surface: PlaywrightSurface, mock_app_url: str, tmp_path: Path, lookup: CapabilityArtifact) -> None:
    result, _ = replay(surface, mock_app_url, tmp_path, lookup, {"member_id": "10003"})
    assert result.status == "success" and result.outputs == {"savings_balance": 15780.42}


def test_replay_multi_field_form_reaches_review_without_committing(surface: PlaywrightSurface, mock_app_url: str, tmp_path: Path, open_sub_account: CapabilityArtifact) -> None:
    a = open_sub_account
    assert {p.name for p in a.inputs} == {"member_id", "nickname", "initial_deposit"}
    result, _ = replay(surface, mock_app_url, tmp_path, a, {"member_id": "10003", "nickname": "Travel", "initial_deposit": "40"})
    assert result.status == "success", result.error
    assert surface.url().endswith("/members/10003/sub-accounts/review")
    assert surface.exists(Locator(by="text", text="Travel")) and surface.exists(Locator(by="text", text="$40.00"))


# --------------------------------------------------------- business outcomes


def test_member_not_found_is_a_business_outcome_not_a_failure(surface: PlaywrightSurface, mock_app_url: str, tmp_path: Path, lookup: CapabilityArtifact) -> None:
    result, run_dir = replay(surface, mock_app_url, tmp_path, lookup, {"member_id": "99999"})
    assert result.status == "business_outcome"
    assert result.outcome and result.outcome.code == "MEMBER_NOT_FOUND" and result.outcome.step_id == "s7"
    assert result.error is None
    assert result.outputs == {"found": "false"}
    assert list((run_dir / "screenshots").glob("outcome-MEMBER_NOT_FOUND.png"))


def test_invalid_member_id_format_is_a_business_outcome(surface: PlaywrightSurface, mock_app_url: str, tmp_path: Path, lookup: CapabilityArtifact) -> None:
    result, _ = replay(surface, mock_app_url, tmp_path, lookup, {"member_id": "abc"})
    assert result.status == "business_outcome" and result.outcome and result.outcome.code == "INVALID_MEMBER_ID"


# ---------------------------------------------------------------- failures


def test_missing_input_fails_before_touching_the_browser(surface: PlaywrightSurface, mock_app_url: str, tmp_path: Path, lookup: CapabilityArtifact) -> None:
    result, _ = replay(surface, mock_app_url, tmp_path, lookup, {})
    assert result.status == "failure" and result.error
    assert result.error.code == "INPUT_INVALID" and result.error.category == "input"
    assert "member_id" in result.error.message and result.steps == []


def test_permission_denied_is_a_hard_failure_with_a_precise_code(surface: PlaywrightSurface, mock_app_url: str, tmp_path: Path, lookup: CapabilityArtifact) -> None:
    result, run_dir = replay(surface, mock_app_url, tmp_path, lookup, {"member_id": "40403"})
    assert result.status == "failure" and result.error
    e = result.error
    assert e.code == "PERMISSION_DENIED" and e.category == "authorization" and not e.retryable
    assert e.step_id == "s7" and e.observed and "Access Denied" in e.observed
    assert e.screenshot and (run_dir / e.screenshot).exists()


def test_checkpoint_failure_reports_expected_vs_observed(surface: PlaywrightSurface, mock_app_url: str, tmp_path: Path, lookup: CapabilityArtifact) -> None:
    broken = lookup.model_copy(deep=True)
    cp = broken.steps[6].checkpoint
    assert cp is not None
    cp.title_contains = "Wire Transfer Approval"
    cp.timeout_ms = 800
    result, _ = replay(surface, mock_app_url, tmp_path, broken, {"member_id": "10001"})
    assert result.status == "failure" and result.error
    assert result.error.code == "CHECKPOINT_FAILED" and result.error.step_id == "s7"
    assert "Wire Transfer Approval" in (result.error.expected or "")
    assert "Member Detail" in (result.error.observed or "")


def test_target_not_found_lists_every_locator_attempt(surface: PlaywrightSurface, mock_app_url: str, tmp_path: Path, lookup: CapabilityArtifact) -> None:
    broken = lookup.model_copy(deep=True)
    broken.steps[5].target = TargetDescriptor(
        description="Member ID box (renamed)",
        candidates=[Locator(by="role", role="textbox", name="Account Holder ID"), Locator(by="css", selector="#nope")],
    )
    result, _ = replay(surface, mock_app_url, tmp_path, broken, {"member_id": "10001"}, options=ReplayOptions(step_timeout_ms=700))
    assert result.status == "failure" and result.error
    e = result.error
    assert e.code == "TARGET_NOT_FOUND" and e.category == "target" and e.retryable and e.step_id == "s6"
    assert len(e.attempts) == 2 and "#nope" in e.expected


# ------------------------------------------------------------- drift signal


def test_fallback_locator_succeeds_and_is_reported_as_drift(surface: PlaywrightSurface, mock_app_url: str, tmp_path: Path, lookup: CapabilityArtifact) -> None:
    drifted = lookup.model_copy(deep=True)
    target = drifted.steps[5].target
    assert target is not None
    target.candidates.insert(0, Locator(by="css", selector="#member-id-input-v3"))
    result, _ = replay(surface, mock_app_url, tmp_path, drifted, {"member_id": "10001"})
    assert result.status == "success"
    assert result.drift.detected and result.drift.fallback_steps[0]["step_id"] == "s6"
    assert result.drift.fallback_steps[0]["candidate_index"] == 1


# ------------------------------------------------------- recoverable conditions


def test_session_timeout_is_recovered_by_re_running_bootstrap(surface: PlaywrightSurface, mock_app_url: str, tmp_path: Path, lookup: CapabilityArtifact) -> None:
    admin_post(mock_app_url, "/__admin/reset")
    admin_post(mock_app_url, "/__admin/inject", {"kind": "session_timeout"})
    policy = make_policy(mock_app_url)
    secrets = SecretStore(policy.secrets.allowed, env=ENV)
    redactor = Redactor(policy.redaction.patterns, secrets=secrets.known_values())
    recorder = RunRecorder(tmp_path, "replay", redactor=redactor)
    engine = ReplayEngine(surface=surface, env_policy=policy, secrets=secrets, recorder=recorder, redactor=redactor, options=ReplayOptions(step_timeout_ms=6_000))
    result = engine.run(lookup.model_copy(deep=True), {"member_id": "10001"})
    recorder.close()
    assert result.status == "success", result.error
    assert [r.code for r in result.recoveries] == ["SESSION_EXPIRED"]
    rec = result.recoveries[0]
    assert rec.succeeded and "bootstrap" in rec.action and "restart phase" in rec.action
    # The sign-in phase ran twice: once interrupted by the timeout, once to recover.
    assert sum(1 for s in result.steps if s.step_id == "s4") == 2
    assert result.outputs == {"savings_balance": 4210.55}


def test_system_notice_is_dismissed_and_phase_restarted(surface: PlaywrightSurface, mock_app_url: str, tmp_path: Path, lookup: CapabilityArtifact) -> None:
    admin_post(mock_app_url, "/__admin/reset")
    admin_post(mock_app_url, "/__admin/inject", {"kind": "interstitial_notice"})
    policy = make_policy(mock_app_url)
    secrets = SecretStore(policy.secrets.allowed, env=ENV)
    redactor = Redactor(policy.redaction.patterns, secrets=secrets.known_values())
    recorder = RunRecorder(tmp_path, "replay", redactor=redactor)
    engine = ReplayEngine(surface=surface, env_policy=policy, secrets=secrets, recorder=recorder, redactor=redactor, options=ReplayOptions(step_timeout_ms=6_000))
    result = engine.run(lookup.model_copy(deep=True), {"member_id": "10001"})
    recorder.close()
    assert result.status == "success", result.error
    assert [r.code for r in result.recoveries] == ["SYSTEM_NOTICE"]
    assert result.outputs == {"savings_balance": 4210.55}


def test_slow_load_is_absorbed_by_waiting(surface: PlaywrightSurface, mock_app_url: str, tmp_path: Path, lookup: CapabilityArtifact) -> None:
    admin_post(mock_app_url, "/__admin/reset")
    admin_post(mock_app_url, "/__admin/inject", {"kind": "slow_load", "delay_ms": 2500})
    policy = make_policy(mock_app_url)
    secrets = SecretStore(policy.secrets.allowed, env=ENV)
    redactor = Redactor(policy.redaction.patterns, secrets=secrets.known_values())
    recorder = RunRecorder(tmp_path, "replay", redactor=redactor)
    engine = ReplayEngine(surface=surface, env_policy=policy, secrets=secrets, recorder=recorder, redactor=redactor, options=ReplayOptions(step_timeout_ms=8_000))
    result = engine.run(lookup.model_copy(deep=True), {"member_id": "10001"})
    recorder.close()
    assert result.status == "success" and result.recoveries == []


def test_transient_app_error_is_retried_once_then_hard(surface: PlaywrightSurface, mock_app_url: str, tmp_path: Path, lookup: CapabilityArtifact) -> None:
    policy = make_policy(mock_app_url)
    secrets = SecretStore(policy.secrets.allowed, env=ENV)
    redactor = Redactor(policy.redaction.patterns, secrets=secrets.known_values())

    admin_post(mock_app_url, "/__admin/reset")
    admin_post(mock_app_url, "/__admin/inject", {"kind": "app_error", "count": 1})
    recorder = RunRecorder(tmp_path / "once", "replay", redactor=redactor)
    engine = ReplayEngine(surface=surface, env_policy=policy, secrets=secrets, recorder=recorder, redactor=redactor, options=ReplayOptions(step_timeout_ms=6_000))
    once = engine.run(lookup.model_copy(deep=True), {"member_id": "10001"})
    recorder.close()
    assert once.status == "success" and [r.code for r in once.recoveries] == ["APP_ERROR"]

    admin_post(mock_app_url, "/__admin/reset")
    admin_post(mock_app_url, "/__admin/inject", {"kind": "app_error", "count": 3})
    recorder = RunRecorder(tmp_path / "persistent", "replay", redactor=redactor)
    engine = ReplayEngine(surface=surface, env_policy=policy, secrets=secrets, recorder=recorder, redactor=redactor, options=ReplayOptions(step_timeout_ms=6_000))
    persistent = engine.run(lookup.model_copy(deep=True), {"member_id": "10001"})
    recorder.close()
    assert persistent.status == "failure" and persistent.error
    assert persistent.error.code == "APP_ERROR" and persistent.error.category == "application" and persistent.error.retryable


# ------------------------------------------------------------ risky steps


def _with_confirm_step(artifact: CapabilityArtifact) -> CapabilityArtifact:
    a = artifact.model_copy(deep=True)
    a.steps.append(
        Step(
            id="s99", phase="main", action="click", description="Commit the new sub-account", risk="risky",
            target=TargetDescriptor(description="Confirm & Open Account", candidates=[Locator(by="role", role="button", name="Confirm & Open Account")]),
        )
    )
    a.success.url_pattern = None
    a.success.title_contains = None
    # Adding a step that reaches a new page requires widening the capability's declared
    # (least-privilege) policy too; the environment policy already permits the path.
    a.policy.allowlist.path_patterns.append("/members/*/sub-accounts/confirm")
    return a


def test_risky_step_is_blocked_without_confirmation(surface: PlaywrightSurface, mock_app_url: str, tmp_path: Path, open_sub_account: CapabilityArtifact) -> None:
    result, _ = replay(surface, mock_app_url, tmp_path, _with_confirm_step(open_sub_account), {"member_id": "10002", "nickname": "Bills", "initial_deposit": "25.00"})
    assert result.status == "failure" and result.error
    assert result.error.code == "CONFIRMATION_REQUIRED" and result.error.category == "policy" and result.error.step_id == "s99"
    assert surface.url().endswith("/sub-accounts/review")  # nothing committed


def test_risky_step_runs_with_explicit_pre_approval(surface: PlaywrightSurface, mock_app_url: str, tmp_path: Path, open_sub_account: CapabilityArtifact) -> None:
    result, run_dir = replay(
        surface, mock_app_url, tmp_path, _with_confirm_step(open_sub_account),
        {"member_id": "10002", "nickname": "Bills", "initial_deposit": "25.00"},
        options=ReplayOptions(allow_risky=True, step_timeout_ms=6_000),
    )
    assert result.status == "success", result.error
    assert surface.exists(Locator(by="text", text="Sub-Account Opened"))
    events = [json.loads(line)["kind"] for line in (run_dir / "run.jsonl").read_text().splitlines()]
    assert "policy.preapproved" in events


def test_deprecated_artifact_is_refused(surface: PlaywrightSurface, mock_app_url: str, tmp_path: Path, lookup: CapabilityArtifact) -> None:
    old = lookup.model_copy(deep=True)
    old.status = "deprecated"
    result, _ = replay(surface, mock_app_url, tmp_path, old, {"member_id": "10001"})
    assert result.status == "failure" and result.error and result.error.code == "ARTIFACT_DEPRECATED"
