"""Unit tests for the policy layer: allowlist, risk, secrets, redaction, intersection."""

from __future__ import annotations

from pathlib import Path

import pytest

from cua.artifact.actions import ActionIntent
from cua.policy.gate import PolicyGate, PolicyViolation
from cua.policy.model import AllowlistPolicy, Policy, RiskPolicy, SecretsPolicy, glob_to_regex
from cua.policy.redaction import Redactor
from cua.policy.secrets import SecretMissing, SecretNotAllowed, SecretStore

BASE = "http://127.0.0.1:4000"


def make_policy(mode: str = "confirm", **kw: object) -> Policy:
    return Policy(
        name="test",
        allowlist=AllowlistPolicy(
            origins=[BASE],
            path_patterns=["/", "/login", "/members/**", "/notice/**"],
            denied_path_patterns=["/__admin/**"],
        ),
        risk=RiskPolicy(mode=mode, risky_url_patterns=["/**/confirm"]),  # type: ignore[arg-type]
        secrets=SecretsPolicy(allowed=["TARGET_PASSWORD"]),
        **kw,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------- allowlist


def test_glob_patterns() -> None:
    assert glob_to_regex("/members/**").match("/members/10001/sub-accounts/new")
    assert glob_to_regex("/members/*").match("/members/10001")
    assert not glob_to_regex("/members/*").match("/members/10001/x")
    assert not glob_to_regex("/members/**").match("/member")


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        (f"{BASE}/members/search", True),
        (f"{BASE}/login?next=/members/10001", True),
        (f"{BASE}/__admin/inject", False),
        (f"{BASE}/reports", False),
        ("http://evil.example.com/members/search", False),
        ("https://127.0.0.1:4000/members/search", False),
        ("about:blank", True),
    ],
)
def test_allowlist_urls(url: str, ok: bool) -> None:
    assert make_policy().allows_url(url)[0] is ok


def test_navigate_outside_allowlist_is_refused_and_enforce_raises() -> None:
    gate = PolicyGate(make_policy())
    d = gate.check_action(ActionIntent(type="navigate", url=f"{BASE}/__admin/inject"), current_url=f"{BASE}/")
    assert not d.allowed and "denied" in d.reason
    with pytest.raises(PolicyViolation):
        gate.enforce(ActionIntent(type="navigate", url="http://evil.example.com/"), current_url=f"{BASE}/")


def test_post_action_url_guard_catches_link_navigation() -> None:
    gate = PolicyGate(make_policy())
    assert gate.check_url(f"{BASE}/members/10001").allowed
    with pytest.raises(PolicyViolation):
        gate.enforce_url("http://elsewhere.example.com/phish")


def test_disallowed_action_type() -> None:
    policy = make_policy()
    policy.allowlist.action_types = ["navigate", "click", "extract"]
    d = PolicyGate(policy).check_action(ActionIntent(type="type", value="x"), current_url=f"{BASE}/login")
    assert not d.allowed and "not allowed" in d.reason


# --------------------------------------------------------------------- risk


def test_safe_click_is_allowed_without_confirmation() -> None:
    d = PolicyGate(make_policy()).check_action(
        ActionIntent(type="click", control_name="Search", control_role="button"), current_url=f"{BASE}/members/search"
    )
    assert d.allowed and d.risk == "safe" and not d.requires_confirmation


@pytest.mark.parametrize("name", ["Confirm & Open Account", "Approve", "Delete member", "Transfer funds"])
def test_risky_control_names_require_confirmation(name: str) -> None:
    d = PolicyGate(make_policy("confirm")).check_action(
        ActionIntent(type="click", control_name=name), current_url=f"{BASE}/members/10001"
    )
    assert d.allowed and d.risk == "risky" and d.requires_confirmation


def test_risky_mode_block_refuses() -> None:
    d = PolicyGate(make_policy("block")).check_action(
        ActionIntent(type="click", control_name="Confirm"), current_url=f"{BASE}/x"
    )
    assert not d.allowed and d.risk == "risky"


def test_risky_mode_allow_passes_without_confirmation() -> None:
    d = PolicyGate(make_policy("allow")).check_action(
        ActionIntent(type="click", control_name="Confirm"), current_url=f"{BASE}/x"
    )
    assert d.allowed and d.risk == "risky" and not d.requires_confirmation


def test_risky_url_pattern_applies_to_press_on_current_page() -> None:
    gate = PolicyGate(make_policy())
    d = gate.check_action(ActionIntent(type="press", key="Enter"), current_url=f"{BASE}/members/1/sub-accounts/confirm")
    assert d.risk == "risky"


def test_declared_risk_can_raise_but_not_lower() -> None:
    gate = PolicyGate(make_policy())
    raised = gate.classify_risk(
        ActionIntent(type="click", control_name="Next", declared_risk="risky"), current_url=f"{BASE}/x"
    )
    assert raised == "risky"
    not_lowered = gate.classify_risk(
        ActionIntent(type="click", control_name="Confirm", declared_risk="safe"), current_url=f"{BASE}/x"
    )
    assert not_lowered == "risky"


# ------------------------------------------------------------------ secrets


def test_secret_store_resolves_only_allowed_and_present() -> None:
    store = SecretStore(allowed=["TARGET_PASSWORD"], env={"TARGET_PASSWORD": "pw-123", "AWS_KEY": "nope"})
    assert store.resolve("{{secret:TARGET_PASSWORD}}") == ("pw-123", True)
    assert store.resolve("plain") == ("plain", False)
    with pytest.raises(SecretNotAllowed):
        store.resolve("{{ secret:AWS_KEY }}")
    with pytest.raises(SecretMissing):
        SecretStore(allowed=["X"], env={}).resolve("{{secret:X}}")


def test_gate_refuses_unlisted_secret_and_secrets_in_urls() -> None:
    gate = PolicyGate(make_policy())
    d = gate.check_action(ActionIntent(type="type", value="{{secret:AWS_KEY}}"), current_url=f"{BASE}/login")
    assert not d.allowed and "AWS_KEY" in d.reason
    ok = gate.check_action(ActionIntent(type="type", value="{{secret:TARGET_PASSWORD}}"), current_url=f"{BASE}/login")
    assert ok.allowed
    d2 = gate.check_action(
        ActionIntent(type="navigate", url=f"{BASE}/login?p={{{{secret:TARGET_PASSWORD}}}}"), current_url=f"{BASE}/"
    )
    assert not d2.allowed and "URL" in d2.reason


# ---------------------------------------------------------------- redaction


def test_redactor_layers() -> None:
    r = Redactor(
        [r"\b\d{3}-\d{2}-\d{4}\b"], secrets={"TARGET_PASSWORD": "demo-pass-2024"}, sensitive={"savingsBalance": "$4,210.55"}
    )
    text = "typed demo-pass-2024; balance $4,210.55; ssn 123-45-6789; short: ab"
    out = r.redact(text)
    assert "demo-pass-2024" not in out and "[REDACTED:secret:TARGET_PASSWORD]" in out
    assert "$4,210.55" not in out and "[REDACTED:savingsBalance]" in out
    assert "123-45-6789" not in out and "[REDACTED:pii]" in out


def test_redactor_recurses_into_objects_and_skips_tiny_values() -> None:
    r = Redactor(secrets={"K": "ab"}, sensitive={"n": "9"})
    obj = {"a": ["ab is tiny", {"b": ("9 too",)}], "n": 5}
    assert r.redact_obj(obj) == obj  # nothing substituted: values under the minimum length
    r.add_sensitive("memberName", "Sample, Jane")
    assert r.redact_obj({"seen": "Name: Sample, Jane"}) == {"seen": "Name: [REDACTED:memberName]"}


def test_default_pii_patterns_cover_card_email_phone() -> None:
    r = Redactor(Policy(name="p", allowlist=AllowlistPolicy(origins=[BASE])).redaction.patterns)
    out = r.redact("card 4111 1111 1111 1111 mail a.b@example.com tel (555) 123-4567 id 10001 date 2015-03-12")
    assert "4111" not in out and "example.com" not in out and "123-4567" not in out
    assert "10001" in out and "2015-03-12" in out


def test_default_pii_patterns_leave_decimal_numbers_alone() -> None:
    r = Redactor(Policy(name="p", allowlist=AllowlistPolicy(origins=[BASE])).redaction.patterns)
    out = r.redact('"y": 123.4140625, "x": 154.9140625, pi 3.1415926535, tel 555.123.4567')
    assert "123.4140625" in out and "154.9140625" in out and "3.1415926535" in out
    assert "555.123.4567" not in out and "[REDACTED:pii]" in out


# ------------------------------------------------------------- intersection


def test_intersection_only_narrows() -> None:
    env = make_policy("confirm")
    declared = Policy(
        name="cap",
        allowlist=AllowlistPolicy(origins=[BASE, "http://other:1"], path_patterns=["/members/**"], action_types=["navigate", "click"]),
        risk=RiskPolicy(mode="allow"),
        secrets=SecretsPolicy(allowed=["TARGET_PASSWORD", "OTHER"]),
    )
    merged = env.intersect(declared)
    assert merged.allowlist.origins == [BASE]
    assert merged.allowlist.action_types == ["navigate", "click"]
    assert merged.risk.mode == "confirm"  # stricter wins
    assert merged.secrets.allowed == ["TARGET_PASSWORD"]
    assert merged.allows_url(f"{BASE}/members/1")[0]
    assert not merged.allows_url(f"{BASE}/login")[0]  # allowed by env, not by the capability
    assert not merged.allows_url(f"{BASE}/__admin/x")[0]


def test_default_policy_file_loads_with_env_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TARGET_APP_URL", "http://127.0.0.1:4999")
    policy = Policy.load(Path("policies/mock-portal.toml"))
    assert policy.name == "mock-portal"
    assert "http://127.0.0.1:4999" in policy.allowlist.origins
    assert policy.allows_url("http://127.0.0.1:4999/members/search")[0]
    assert not policy.allows_url("http://127.0.0.1:4999/__admin/inject")[0]
    assert policy.risk.mode == "confirm"
