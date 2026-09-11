"""
Human handoff tests.

The ledger is tested as a pure state machine. The handoff itself is tested
end to end against the real browser: the "operator" is a hook that runs in
the wait loop and acts directly on the underlying Playwright page (not via
the automation API, which is locked while a human holds the session), then
writes the resume signal exactly as `cua-resume` would.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import admin_post
from scripted_runs import ENV, LOGIN, el, make_policy, run_script, step

from cua.agent.loop import DiscoveryAgent, DiscoveryRequest
from cua.agent.providers.scripted import ScriptedProvider
from cua.artifact.builder import build_artifact
from cua.artifact.schema import CapabilityArtifact, DeclaredConditions
from cua.cli import resume as resume_cli
from cua.escalation.confirm import ConfirmViaHandoff
from cua.escalation.handoff import INTERVENTION_FILE, RESUME_FILE, HeadedBrowserHandoff, write_resume_signal
from cua.escalation.session import ControlViolation, SessionControl
from cua.evidence.recorder import RunRecorder
from cua.policy.gate import PolicyGate
from cua.policy.redaction import Redactor
from cua.policy.secrets import SecretStore
from cua.replay.engine import ReplayEngine, ReplayOptions
from cua.surface.base import ActionFailed
from cua.surface.playwright_surface import PlaywrightSurface

CONDITIONS = DeclaredConditions.model_validate_json(Path("policies/mock-portal.conditions.json").read_text())


# ------------------------------------------------------------ ledger (pure)


def test_ledger_transitions_and_guard() -> None:
    c = SessionControl()
    c.assert_automation("click")  # fine
    c.cede_to_human("stuck at s4")
    assert c.human_in_control
    with pytest.raises(ControlViolation):
        c.assert_automation("click")
    with pytest.raises(ControlViolation):
        c.cede_to_human("twice")
    c.record_human_action({"kind": "click", "role": "button", "name": "Continue"})
    c.record_human_action({"kind": "change", "role": "textbox", "name": "Member ID", "value_length": 5})
    c.record_human_action({"kind": "change", "role": "combobox", "name": "Account Type", "option": "Checking"})
    c.return_to_automation("alice", "skip_step", "dismissed the notice")
    assert not c.human_in_control
    with pytest.raises(ControlViolation):
        c.return_to_automation("alice", "abort")
    assert [e.event for e in c.events] == ["paused", "ceded", "human_action", "human_action", "human_action", "resumed"]
    assert [e.holder for e in c.events] == ["automation", "human", "human", "human", "human", "automation"]
    details = [e.detail for e in c.events]
    assert details[2] == 'click button "Continue"'
    assert details[3] == 'changed textbox "Member ID" (value not recorded, 5 chars)'
    assert details[4] == "selected 'Checking' in combobox \"Account Type\""
    assert "alice" in details[-1] and "skip_step" in details[-1]


def test_resume_signal_requires_a_pending_intervention(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        write_resume_signal(tmp_path, "abort", "alice")
    (tmp_path / INTERVENTION_FILE).write_text("{}")
    path = write_resume_signal(tmp_path, "retry_step", "alice", "fixed it")
    assert path.name == RESUME_FILE and json.loads(path.read_text())["operator"] == "alice"


def test_resume_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert resume_cli.main(["--run", str(tmp_path), "--resolution", "abort", "--operator", "bob"]) == 2
    (tmp_path / INTERVENTION_FILE).write_text(json.dumps({"reason": "x"}))
    assert resume_cli.main(["--run", str(tmp_path), "--resolution", "skip_step", "--operator", "bob", "--show"]) == 0
    out = capsys.readouterr().out
    assert '"reason": "x"' in out and "skip_step by bob" in out
    assert json.loads((tmp_path / RESUME_FILE).read_text())["resolution"] == "skip_step"


# ----------------------------------------------------------- browser-level

pytestmark_browser = pytest.mark.browser


@pytest.fixture(scope="module")
def surface() -> Iterator[PlaywrightSurface]:
    s = PlaywrightSurface(headless=True, default_timeout_ms=5_000)
    s.open()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(scope="module")
def lookup_no_notice_handling(surface: PlaywrightSurface, mock_app_url: str, tmp_path_factory: pytest.TempPathFactory) -> CapabilityArtifact:
    """The lookup capability, but WITHOUT the SYSTEM_NOTICE recoverable: an unknown interstitial must escalate."""
    admin_post(mock_app_url, "/__admin/reset")
    script = [
        *LOGIN,
        step("type", element=el("textbox", "Member ID"), text="10001"),
        step("click", element=el("button", "Search")),
        step("extract", element=el("cell", "$4,210.55"), name="savings_balance", sensitive=True),
        step("done", summary="done"),
    ]
    trace, _ = run_script(surface, mock_app_url, tmp_path_factory.mktemp("cap"), script, inputs={"member_id": "10001"}, capability_name="lookup_member_balance")
    conditions = CONDITIONS.model_copy(update={"recoverables": [r for r in CONDITIONS.recoverables if r.code != "SYSTEM_NOTICE"]})
    return build_artifact(trace, base_url=mock_app_url, app="harborview-member-console", conditions=conditions)


def _engine(surface: PlaywrightSurface, base: str, tmp_path: Path, handoff_kwargs: dict) -> tuple[ReplayEngine, RunRecorder]:
    policy = make_policy(base)
    secrets = SecretStore(policy.secrets.allowed, env=ENV)
    redactor = Redactor(policy.redaction.patterns, secrets=secrets.known_values())
    recorder = RunRecorder(tmp_path, "replay", redactor=redactor, screenshot_mode="on_failure")
    handoff = HeadedBrowserHandoff(surface=surface, control=surface.control, recorder=recorder, poll_ms=100, announce=lambda _: None, **handoff_kwargs)
    engine = ReplayEngine(surface=surface, env_policy=policy, secrets=secrets, recorder=recorder, redactor=redactor, escalate=handoff, options=ReplayOptions(step_timeout_ms=1_500))
    return engine, recorder


@pytest.mark.browser
def test_replay_escalates_human_fixes_it_in_the_same_session_and_run_completes(
    surface: PlaywrightSurface, mock_app_url: str, tmp_path: Path, lookup_no_notice_handling: CapabilityArtifact
) -> None:
    admin_post(mock_app_url, "/__admin/reset")
    admin_post(mock_app_url, "/__admin/inject", {"kind": "interstitial_notice"})
    acted = {"done": False}

    def operator(elapsed: float) -> None:
        if acted["done"]:
            return
        acted["done"] = True
        run_dir = recorder.dir
        request = json.loads((run_dir / INTERVENTION_FILE).read_text())
        assert request["error_code"] == "CHECKPOINT_FAILED" and request["step_id"] == "s4"
        assert (run_dir / request["screenshot"]).exists()
        # Automation is locked out while the human holds the session...
        with pytest.raises(ControlViolation):
            surface.navigate(f"{mock_app_url}/members/search")
        assert surface.page.evaluate("document.getElementById('__cua_banner').textContent").startswith("HUMAN CONTROL")
        # ...and the human acts in the SAME live page: dismiss the notice.
        surface.page.get_by_role("button", name="Continue").click()
        surface.page.wait_for_load_state("load")
        assert surface.page.url.endswith("/members/search")
        write_resume_signal(run_dir, "skip_step", "alice", "dismissed the maintenance notice")

    engine, recorder = _engine(surface, mock_app_url, tmp_path, {"on_wait": operator})
    result = engine.run(lookup_no_notice_handling.model_copy(deep=True), {"member_id": "10001"})
    recorder.close()

    assert result.status == "success", result.error
    assert result.outputs == {"savings_balance": 4210.55}
    statuses = [(s.step_id, s.status) for s in result.steps]
    assert ("s4", "failed") in statuses and ("s4", "skipped") in statuses
    events = [(e.holder, e.event) for e in result.control_events]
    assert events[:2] == [("automation", "paused"), ("human", "ceded")]
    assert events[-1] == ("automation", "resumed")
    human_actions = [e.detail for e in result.control_events if e.event == "human_action"]
    assert any('click button "Continue"' in d for d in human_actions)
    assert any("navigated to" in d and "/members/search" in d for d in human_actions)
    assert not surface.page.evaluate("!!document.getElementById('__cua_banner')")
    log_kinds = [json.loads(line)["kind"] for line in (recorder.dir / "run.jsonl").read_text().splitlines()]
    assert "escalation.requested" in log_kinds and "escalation.human_actions" in log_kinds and "escalation.resolved" in log_kinds


@pytest.mark.browser
def test_operator_abort_surfaces_the_original_error_with_control_history(
    surface: PlaywrightSurface, mock_app_url: str, tmp_path: Path, lookup_no_notice_handling: CapabilityArtifact
) -> None:
    admin_post(mock_app_url, "/__admin/reset")
    admin_post(mock_app_url, "/__admin/inject", {"kind": "interstitial_notice"})

    def operator(elapsed: float) -> None:
        if not (recorder.dir / RESUME_FILE).exists():
            write_resume_signal(recorder.dir, "abort", "bob", "not something I can fix")

    engine, recorder = _engine(surface, mock_app_url, tmp_path, {"on_wait": operator})
    result = engine.run(lookup_no_notice_handling.model_copy(deep=True), {"member_id": "10001"})
    recorder.close()
    assert result.status == "failure" and result.error and result.error.code == "CHECKPOINT_FAILED"
    assert [e.event for e in result.control_events] == ["paused", "ceded", "resumed"]
    assert "bob" in result.control_events[-1].detail and "abort" in result.control_events[-1].detail


@pytest.mark.browser
def test_no_operator_response_times_out_to_abort(
    surface: PlaywrightSurface, mock_app_url: str, tmp_path: Path, lookup_no_notice_handling: CapabilityArtifact
) -> None:
    admin_post(mock_app_url, "/__admin/reset")
    admin_post(mock_app_url, "/__admin/inject", {"kind": "interstitial_notice"})
    engine, recorder = _engine(surface, mock_app_url, tmp_path, {"timeout_s": 0.6})
    result = engine.run(lookup_no_notice_handling.model_copy(deep=True), {"member_id": "10001"})
    recorder.close()
    assert result.status == "failure" and result.error and result.error.code == "CHECKPOINT_FAILED"
    assert "timeout" in result.control_events[-1].detail
    assert not surface.control.human_in_control


@pytest.mark.browser
def test_discovery_risky_action_confirmed_by_operator_via_handoff(surface: PlaywrightSurface, mock_app_url: str, tmp_path: Path) -> None:
    admin_post(mock_app_url, "/__admin/reset")
    policy = make_policy(mock_app_url)
    secrets = SecretStore(policy.secrets.allowed, env=ENV)
    redactor = Redactor(policy.redaction.patterns, secrets=secrets.known_values())
    recorder = RunRecorder(tmp_path, "discovery", redactor=redactor)

    def operator(elapsed: float) -> None:
        if not (recorder.dir / RESUME_FILE).exists():
            req = json.loads((recorder.dir / INTERVENTION_FILE).read_text())
            assert req["error_code"] == "CONFIRMATION_REQUIRED" and "Confirm & Open Account" in req["step_description"]
            write_resume_signal(recorder.dir, "retry_step", "carol", "approved: task explicitly asks to open the account")

    handoff = HeadedBrowserHandoff(surface=surface, control=surface.control, recorder=recorder, poll_ms=100, announce=lambda _: None, on_wait=operator)
    confirm = ConfirmViaHandoff(handoff, recorder, capability="open_sub_account", run_label="discovery")
    script = [
        *LOGIN,
        step("type", element=el("textbox", "Member ID"), text="10002"),
        step("click", element=el("button", "Search")),
        step("click", element=el("link", "Open Sub-Account")),
        step("type", element=el("textbox", "Nickname"), text="Bills"),
        step("type", element=el("textbox", "Initial Deposit"), text="25"),
        step("click", element=el("button", "Review")),
        step("click", element=el("button", "Confirm & Open Account")),
        step("done", summary="opened"),
    ]
    agent = DiscoveryAgent(surface=surface, llm=ScriptedProvider(script), gate=PolicyGate(policy), secrets=secrets, recorder=recorder, redactor=redactor, confirm=confirm)
    trace = agent.run(DiscoveryRequest(goal="open a sub-account", start_url=f"{mock_app_url}/login", capability_name="open_sub_account", inputs={"member_id": "10002"}))
    recorder.close()
    assert trace.status == "success"
    confirm_step = trace.steps[9]
    assert confirm_step.result.status == "ok" and confirm_step.decision and confirm_step.decision.risk == "risky"
    assert surface.url().endswith("/sub-accounts/confirm")
    assert [e.event for e in trace.control_events] == ["paused", "ceded", "resumed"]
    artifact = build_artifact(trace, base_url=mock_app_url, app="a", conditions=CONDITIONS)
    assert artifact.steps[-1].risk == "risky" and artifact.steps[-1].action == "click"


@pytest.mark.browser
def test_banner_survives_navigation_while_human_holds_control(surface: PlaywrightSurface, mock_app_url: str) -> None:
    surface.navigate(f"{mock_app_url}/login")
    surface.set_banner("HUMAN CONTROL — test")
    surface.page.goto(f"{mock_app_url}/login")  # a navigation by the human
    surface.page.wait_for_function("!!document.getElementById('__cua_banner')")
    surface.set_banner(None)
    surface.page.goto(f"{mock_app_url}/login")
    assert not surface.page.evaluate("!!document.getElementById('__cua_banner')")


@pytest.mark.browser
def test_action_failed_is_raised_for_bbox_only_handles(surface: PlaywrightSurface, mock_app_url: str) -> None:
    from cua.artifact.targets import BBox, Locator
    from cua.surface.base import Handle

    surface.navigate(f"{mock_app_url}/login")
    h = Handle(native=None, candidate=Locator(by="bbox", bbox=BBox(x=1, y=1, width=1, height=1)), bbox=BBox(x=1, y=1, width=1, height=1))
    with pytest.raises(ActionFailed):
        surface.type_text(h, "x")
