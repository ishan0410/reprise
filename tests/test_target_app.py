"""
Behavioural tests for the mock target app.

These pin down every runtime condition the demo capabilities depend on, so a
later change to the mock app can't silently invalidate the evidence runs.
"""

from __future__ import annotations

import time

import pytest
from flask import Flask
from flask.testing import FlaskClient

from cua.target_app.data import DEMO_CREDENTIALS, PERMISSION_DENIED_MEMBER_ID, reset_members
from cua.target_app.faults import faults
from cua.target_app.server import create_app

SUB_ACCOUNT_FORM = {"accountType": "Checking", "nickname": "Bills", "initialDeposit": "25.00"}


@pytest.fixture
def app() -> Flask:
    faults.clear()
    reset_members()
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


def login(client: FlaskClient) -> None:
    r = client.post("/login", data=dict(DEMO_CREDENTIALS))
    assert r.status_code == 302


# ---------------------------------------------------------------------- auth


def test_protected_routes_redirect_to_login(client: FlaskClient) -> None:
    r = client.get("/members/search")
    assert r.status_code == 302
    assert "/login?next=" in r.headers["Location"]


def test_invalid_login_shows_error(client: FlaskClient) -> None:
    r = client.post("/login", data={"username": "teller1", "password": "wrong"})
    assert r.status_code == 401
    assert b"Invalid username or password." in r.data


# ------------------------------------------------------------- member lookup


def test_member_found_redirects_to_detail_with_balances(client: FlaskClient) -> None:
    login(client)
    r = client.post("/members/search", data={"memberId": "10001"})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/members/10001")
    detail = client.get("/members/10001")
    assert detail.status_code == 200
    assert b"Member Detail" in detail.data
    assert b"$4,210.55" in detail.data


def test_member_not_found_is_a_rendered_message_not_an_error_status(client: FlaskClient) -> None:
    login(client)
    r = client.post("/members/search", data={"memberId": "99999"})
    assert r.status_code == 200
    assert b"No member found for ID 99999." in r.data


def test_non_numeric_member_id_is_a_validation_message(client: FlaskClient) -> None:
    login(client)
    r = client.post("/members/search", data={"memberId": "abc"})
    assert r.status_code == 200
    assert b"Member ID must be numeric." in r.data


def test_permission_denied_member(client: FlaskClient) -> None:
    login(client)
    r = client.post("/members/search", data={"memberId": PERMISSION_DENIED_MEMBER_ID})
    assert r.status_code == 403
    assert b"You do not have permission to view member 40403." in r.data


# --------------------------------------------------------------- sub-account


def test_sub_account_flow_reaches_review_then_confirms(client: FlaskClient) -> None:
    login(client)
    r = client.post("/members/10002/sub-accounts/new", data=SUB_ACCOUNT_FORM)
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/sub-accounts/review")

    review = client.get("/members/10002/sub-accounts/review")
    assert b"Review New Sub-Account" in review.data
    assert b"Confirm &amp; Open Account" in review.data
    assert b"cannot be undone" in review.data

    done = client.post("/members/10002/sub-accounts/confirm")
    assert done.status_code == 200
    assert b"Sub-Account Opened" in done.data
    assert b"C-10002-02" in done.data
    assert b"C-10002-02" in client.get("/members/10002").data


def test_sub_account_validation_error(client: FlaskClient) -> None:
    login(client)
    r = client.post("/members/10002/sub-accounts/new", data={**SUB_ACCOUNT_FORM, "nickname": ""})
    assert r.status_code == 200
    assert b"Nickname is required." in r.data


def test_admin_reset_restores_seed_data(client: FlaskClient) -> None:
    login(client)
    client.post("/members/10002/sub-accounts/new", data=SUB_ACCOUNT_FORM)
    client.post("/members/10002/sub-accounts/confirm")
    assert b"C-10002-02" in client.get("/members/10002").data
    assert client.post("/__admin/reset").status_code == 200
    assert b"C-10002-02" not in client.get("/members/10002").data


# ------------------------------------------------------------ injected faults


def test_injected_session_timeout_then_relogin_recovers(client: FlaskClient) -> None:
    login(client)
    assert client.post("/__admin/inject", json={"kind": "session_timeout"}).status_code == 200
    r = client.get("/members/10001")
    assert r.status_code == 401
    assert b"Your session has expired. Please log in again." in r.data
    assert b"/login?next=/members/10001" in r.data
    # The session really is gone, not just the one response.
    assert client.get("/members/10001").status_code == 302
    login(client)
    assert client.get("/members/10001").status_code == 200


def test_injected_interstitial_notice_then_continue(client: FlaskClient) -> None:
    login(client)
    client.post("/__admin/inject", json={"kind": "interstitial_notice"})
    r = client.get("/members/10001")
    assert r.status_code == 200
    assert b"System Notice" in r.data
    assert b'value="Continue"' in r.data
    ack = client.post("/notice/ack", data={"next": "/members/10001"})
    assert ack.status_code == 302
    assert ack.headers["Location"].endswith("/members/10001")
    assert b"Member Detail" in client.get("/members/10001").data


def test_injected_app_error_affects_only_the_next_request(client: FlaskClient) -> None:
    login(client)
    client.post("/__admin/inject", json={"kind": "app_error"})
    r = client.get("/members/10001")
    assert r.status_code == 500
    assert b"Application Error" in r.data
    assert client.get("/members/10001").status_code == 200


def test_injected_slow_load_delays_but_succeeds(client: FlaskClient) -> None:
    login(client)
    client.post("/__admin/inject", json={"kind": "slow_load", "delay_ms": 300})
    t0 = time.monotonic()
    r = client.get("/members/10001")
    assert r.status_code == 200
    assert time.monotonic() - t0 >= 0.3


def test_faults_never_fire_on_exempt_routes(client: FlaskClient) -> None:
    client.post("/__admin/inject", json={"kind": "app_error"})
    assert client.get("/login").status_code == 200
    armed = client.get("/__admin/faults").get_json()["faults"]
    assert armed and armed[0]["kind"] == "app_error"  # still armed for the next real request


def test_unknown_fault_kind_rejected(client: FlaskClient) -> None:
    assert client.post("/__admin/inject", json={"kind": "nope"}).status_code == 400
