"""
Mock credit-union "Member Services Console": the stand-in for a legacy
back-office banking screen that the agent operates.

Flow exercised by the demo capabilities:
    sign in -> member lookup -> member detail (read balances)
            -> open sub-account form -> review screen -> [Confirm & Open]  (irreversible)

Runtime conditions the app can produce, all deterministically:
    business outcomes   : member not found, non-numeric ID, form validation errors
    authorization       : permission denied for a specific member ID
    injected (faults.py): session timeout, system-notice interstitial, slow load, app error
"""

from __future__ import annotations

import math
import os
import secrets
from collections.abc import Callable
from dataclasses import asdict
from functools import wraps
from typing import cast
from urllib.parse import urlencode

from flask import Flask, jsonify, redirect, request, session
from flask.typing import ResponseReturnValue

from . import views
from .data import (
    ACCOUNT_TYPES,
    DEMO_CREDENTIALS,
    PERMISSION_DENIED_MEMBER_ID,
    AccountType,
    Member,
    add_sub_account,
    find_member,
    reset_members,
)
from .faults import FAULT_KINDS, faults, take_fault


def _safe_next(raw: str | None, default: str = "/members/search") -> str:
    """Only ever redirect to a local path (no open redirects)."""
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return default


def _current_user() -> str | None:
    value = session.get("user")
    return str(value) if value else None


def _user() -> str:
    return _current_user() or ""


def _request_path_with_query() -> str:
    return request.full_path.rstrip("?")


def login_required[**P](fn: Callable[P, ResponseReturnValue]) -> Callable[P, ResponseReturnValue]:
    @wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> ResponseReturnValue:
        if not _current_user():
            return redirect("/login?" + urlencode({"next": _request_path_with_query()}))
        return fn(*args, **kwargs)

    return wrapper


def _validate_sub_account(values: dict[str, str]) -> str | None:
    if values.get("accountType") not in ACCOUNT_TYPES:
        return "Select a valid account type."
    if not values.get("nickname"):
        return "Nickname is required."
    if len(values["nickname"]) > 40:
        return "Nickname must be 40 characters or fewer."
    try:
        deposit = float(values.get("initialDeposit", ""))
    except ValueError:
        return "Initial deposit must be a number of 0.00 or more."
    if math.isnan(deposit) or math.isinf(deposit) or deposit < 0:
        return "Initial deposit must be a number of 0.00 or more."
    return None


def create_app() -> Flask:
    app = Flask(__name__)
    # Restarting the server invalidates every session, which is realistic for a legacy console.
    app.secret_key = os.environ.get("TARGET_APP_SECRET") or secrets.token_hex(16)

    # ------------------------------------------------------------------ faults
    @app.before_request
    def inject_faults() -> ResponseReturnValue | None:
        fault = take_fault(request.path)
        if fault is None:
            return None
        next_url = _request_path_with_query()
        if fault.kind == "session_timeout":
            session.clear()
            return views.session_expired_page(next_url), 401
        if fault.kind == "interstitial_notice":
            return views.system_notice_page(next_url, user=_current_user()), 200
        if fault.kind == "unexpected_dialog":
            return views.password_expiry_page(next_url, user=_current_user()), 200
        if fault.kind == "app_error":
            return views.app_error_page(secrets.token_hex(3).upper()), 500
        return None

    # -------------------------------------------------------------------- auth
    @app.get("/")
    def index() -> ResponseReturnValue:
        return redirect("/members/search" if _current_user() else "/login")

    @app.get("/login")
    def login_form() -> ResponseReturnValue:
        return views.login_page(next_url=_safe_next(request.args.get("next")))

    @app.post("/login")
    def login_submit() -> ResponseReturnValue:
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        next_url = _safe_next(request.form.get("next"))
        if username == DEMO_CREDENTIALS["username"] and password == DEMO_CREDENTIALS["password"]:
            session.clear()
            session["user"] = username
            return redirect(next_url)
        return views.login_page(error=views.MSG_INVALID_LOGIN, next_url=next_url), 401

    @app.get("/logout")
    def logout() -> ResponseReturnValue:
        session.clear()
        return redirect("/login")

    # ----------------------------------------------------------------- members
    @app.get("/members/search")
    @login_required
    def search_form() -> ResponseReturnValue:
        return views.search_page(_user())

    @app.post("/members/search")
    @login_required
    def search_submit() -> ResponseReturnValue:
        member_id = request.form.get("memberId", "").strip()
        if not member_id.isdigit():
            return views.search_page(_user(), message=views.MSG_INVALID_ID, member_id=member_id)
        if member_id == PERMISSION_DENIED_MEMBER_ID:
            return views.permission_denied_page(_user(), member_id), 403
        if find_member(member_id) is None:
            msg = views.MSG_NOT_FOUND.format(member_id=member_id)
            return views.search_page(_user(), message=msg, member_id=member_id)
        return redirect(f"/members/{member_id}")

    def _load_member(member_id: str) -> Member | ResponseReturnValue:
        if member_id == PERMISSION_DENIED_MEMBER_ID:
            return views.permission_denied_page(_user(), member_id), 403
        member = find_member(member_id)
        if member is None:
            return views.not_found_page(_user(), member_id), 404
        return member

    @app.get("/members/<member_id>")
    @login_required
    def member_detail(member_id: str) -> ResponseReturnValue:
        loaded = _load_member(member_id)
        if not isinstance(loaded, Member):
            return loaded
        return views.member_detail_page(_user(), loaded)

    # ------------------------------------------------------------ sub-accounts
    @app.get("/members/<member_id>/sub-accounts/new")
    @login_required
    def sub_account_form(member_id: str) -> ResponseReturnValue:
        loaded = _load_member(member_id)
        if not isinstance(loaded, Member):
            return loaded
        return views.sub_account_form_page(_user(), loaded)

    @app.post("/members/<member_id>/sub-accounts/new")
    @login_required
    def sub_account_submit(member_id: str) -> ResponseReturnValue:
        loaded = _load_member(member_id)
        if not isinstance(loaded, Member):
            return loaded
        values = {k: request.form.get(k, "").strip() for k in ("accountType", "nickname", "initialDeposit")}
        error = _validate_sub_account(values)
        if error:
            return views.sub_account_form_page(_user(), loaded, error=error, values=values)
        session["pending_sub_account"] = values
        return redirect(f"/members/{member_id}/sub-accounts/review")

    @app.get("/members/<member_id>/sub-accounts/review")
    @login_required
    def sub_account_review(member_id: str) -> ResponseReturnValue:
        loaded = _load_member(member_id)
        if not isinstance(loaded, Member):
            return loaded
        pending = session.get("pending_sub_account")
        if not isinstance(pending, dict):
            return redirect(f"/members/{member_id}/sub-accounts/new")
        return views.sub_account_review_page(_user(), loaded, cast(dict[str, str], pending))

    @app.post("/members/<member_id>/sub-accounts/confirm")
    @login_required
    def sub_account_confirm(member_id: str) -> ResponseReturnValue:
        loaded = _load_member(member_id)
        if not isinstance(loaded, Member):
            return loaded
        pending = session.pop("pending_sub_account", None)
        if not isinstance(pending, dict):
            return redirect(f"/members/{member_id}/sub-accounts/new")
        values = cast(dict[str, str], pending)
        account = add_sub_account(
            member_id,
            cast(AccountType, values["accountType"]),
            values["nickname"],
            float(values["initialDeposit"]),
        )
        return views.sub_account_success_page(_user(), loaded, account)

    # -------------------------------------------------------------- interstitial
    @app.post("/notice/ack")
    def notice_ack() -> ResponseReturnValue:
        return redirect(_safe_next(request.form.get("next")))

    # ---------------------------------------- test fixture (never allowlisted)
    @app.post("/__admin/inject")
    def admin_inject() -> ResponseReturnValue:
        payload = request.get_json(silent=True)
        body = cast(dict[str, object], payload) if isinstance(payload, dict) else {}
        kind = body.get("kind")
        if kind not in FAULT_KINDS:
            return jsonify(error=f"kind must be one of {list(FAULT_KINDS)}"), 400
        fault = faults.arm(
            kind,
            count=int(str(body.get("count", 1))),
            delay_ms=int(str(body.get("delay_ms", 3000))),
        )
        return jsonify(armed=asdict(fault))

    @app.get("/__admin/faults")
    def admin_faults() -> ResponseReturnValue:
        return jsonify(faults=faults.status())

    @app.post("/__admin/reset")
    def admin_reset() -> ResponseReturnValue:
        faults.clear()
        reset_members()
        return jsonify(ok=True)

    return app


def main() -> None:
    port = int(os.environ.get("TARGET_APP_PORT", "4000"))
    app = create_app()
    print(f"Mock target app listening on http://127.0.0.1:{port}  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
