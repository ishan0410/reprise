"""
Server-rendered HTML for the mock portal, written to look like a real legacy
back-office console: table-based layout, inline presentational attributes,
no test IDs, and inconsistent labeling. Some controls have proper accessible
names (via title attributes or real headings); others only have adjacent text
in a neighbouring table cell. That mix is deliberate: it gives the locator
fallback chain real work to do without making the happy path a coin flip.

All message strings that the replay engine's detection patterns key on live
here, so the mapping from "what the screen says" to "what it means" is easy
to review in one place.
"""

from __future__ import annotations

from html import escape as e

from .data import ACCOUNT_TYPES, Account, Member, format_money

INSTITUTION = "Harborview Federal Credit Union"

# --- Message strings the capability artifact's detection patterns reference ---
MSG_NOT_FOUND = "No member found for ID {member_id}."
MSG_INVALID_ID = "Member ID must be numeric."
MSG_PERMISSION_DENIED = "You do not have permission to view member {member_id}."
MSG_SESSION_EXPIRED = "Your session has expired. Please log in again."
MSG_INVALID_LOGIN = "Invalid username or password."
MSG_APP_ERROR = "An unexpected error occurred while processing your request."


def layout(title: str, body: str, *, user: str | None = None) -> str:
    nav = ""
    if user:
        nav = (
            f'<font size="2" color="#ffffff">Signed in as <b>{e(user)}</b> &nbsp;|&nbsp; '
            '<a href="/members/search" style="color:#ffffff">Member Search</a> &nbsp;|&nbsp; '
            '<a href="/logout" style="color:#ffffff">Log out</a></font>'
        )
    return f"""<html>
<head><title>{e(title)} - {INSTITUTION}</title>
<style>
body {{ font-family: Verdana, Arial, sans-serif; font-size: 12px; background: #e8ecf0; margin: 0; }}
.pageTitle {{ font-size: 15px; color: #1f3a5f; padding: 8px 4px; border-bottom: 2px solid #1f3a5f; }}
.err {{ color: #a40000; font-weight: bold; }}
.note {{ color: #5a4a00; background: #fff5cc; padding: 6px; border: 1px solid #c9b25b; }}
td.hdr {{ background: #1f3a5f; color: #ffffff; font-weight: bold; }}
td.lbl {{ background: #d7dee8; font-weight: bold; width: 140px; }}
</style></head>
<body>
<table width="100%" border="0" cellpadding="6" cellspacing="0" bgcolor="#1f3a5f">
<tr><td><font color="#ffffff" size="4"><b>{INSTITUTION}</b></font><br>
<font color="#c9d6e8" size="2">Member Services Console &mdash; v4.2 (Legacy)</font></td>
<td align="right" valign="top">{nav}</td></tr></table>
<table width="100%" border="0" cellpadding="10"><tr><td>
<table width="760" border="0" cellpadding="6" cellspacing="0" bgcolor="#ffffff" style="border:1px solid #9aa7b8">
<tr><td>{body}</td></tr></table>
</td></tr></table>
<table width="100%" border="0"><tr><td align="center">
<font size="1" color="#666666">{INSTITUTION} &middot; Internal Use Only &middot; Session monitored</font>
</td></tr></table>
</body></html>"""


def _title_bar(text_html: str) -> str:
    # Deliberately NOT a heading element: legacy apps commonly style a table cell instead.
    return f'<table width="100%" border="0" cellpadding="0"><tr><td class="pageTitle"><b>{text_html}</b></td></tr></table>'


def login_page(*, error: str | None = None, next_url: str = "") -> str:
    err = f'<p class="err">{e(error)}</p>' if error else ""
    body = f"""<h2>Staff Sign In</h2>{err}
<form method="post" action="/login">
<input type="hidden" name="next" value="{e(next_url)}">
<table border="0" cellpadding="4">
<tr><td>Username:</td><td><input type="text" name="username" size="20" title="Username"></td></tr>
<tr><td>Password:</td><td><input type="password" name="password" size="20" title="Password"></td></tr>
<tr><td></td><td><input type="submit" value="Sign In"></td></tr>
</table></form>"""
    return layout("Staff Sign In", body)


def search_page(user: str, *, message: str | None = None, member_id: str = "") -> str:
    msg = f'<p class="err">{e(message)}</p>' if message else ""
    body = f"""{_title_bar("Member Lookup")}
{msg}
<form method="post" action="/members/search">
<table border="0" cellpadding="4">
<tr><td>Member ID:</td>
<td><input type="text" name="memberId" size="12" value="{e(member_id)}" title="Member ID"></td>
<td><input type="submit" value="Search"></td></tr>
</table></form>
<p><font size="1" color="#666666">Enter the numeric member ID exactly as shown on the membership card.</font></p>"""
    return layout("Member Lookup", body, user=user)


def member_detail_page(user: str, m: Member) -> str:
    rows = "".join(
        f'<tr bgcolor="#ffffff"><td>{e(a.type)}</td><td>{e(a.number)}</td><td>{e(a.nickname)}</td>'
        f'<td align="right">{e(format_money(a.balance))}</td></tr>'
        for a in m.accounts
    )
    body = f"""{_title_bar("Member Detail")}
<table border="0" cellpadding="4" cellspacing="1" width="100%">
<tr><td class="lbl">Member ID</td><td>{e(m.id)}</td></tr>
<tr><td class="lbl">Name</td><td>{e(m.last_name)}, {e(m.first_name)}</td></tr>
<tr><td class="lbl">Member Since</td><td>{e(m.member_since)}</td></tr>
</table>
<br>
<table border="0" cellpadding="4" cellspacing="1" width="100%" bgcolor="#9aa7b8">
<tr><td class="hdr">Type</td><td class="hdr">Account No.</td><td class="hdr">Nickname</td>
<td class="hdr" align="right">Balance</td></tr>
{rows}
</table>
<br>
<a href="/members/{e(m.id)}/sub-accounts/new">Open Sub-Account</a> &nbsp;|&nbsp; <a href="/members/search">New Search</a>"""
    return layout("Member Detail", body, user=user)


def sub_account_form_page(
    user: str, m: Member, *, error: str | None = None, values: dict[str, str] | None = None
) -> str:
    v = values or {}
    opts = "".join(
        f'<option value="{e(t)}"{" selected" if v.get("accountType") == t else ""}>{e(t)}</option>'
        for t in ACCOUNT_TYPES
    )
    err = f'<p class="err">{e(error)}</p>' if error else ""
    body = f"""{_title_bar(f"Open Sub-Account &mdash; Member {e(m.id)} ({e(m.last_name)}, {e(m.first_name)})")}
{err}
<form method="post" action="/members/{e(m.id)}/sub-accounts/new">
<table border="0" cellpadding="4">
<tr><td>Account Type:</td><td><select name="accountType" title="Account Type">{opts}</select></td></tr>
<tr><td>Nickname:</td>
<td><input type="text" name="nickname" size="30" value="{e(v.get("nickname", ""))}" title="Nickname"></td></tr>
<tr><td>Initial Deposit:</td>
<td><input type="text" name="initialDeposit" size="12" value="{e(v.get("initialDeposit", ""))}" title="Initial Deposit"></td></tr>
<tr><td></td><td><input type="submit" value="Review"> &nbsp; <a href="/members/{e(m.id)}">Cancel</a></td></tr>
</table></form>"""
    return layout("Open Sub-Account", body, user=user)


def sub_account_review_page(user: str, m: Member, pending: dict[str, str]) -> str:
    deposit = float(pending.get("initialDeposit", "0") or 0)
    body = f"""{_title_bar("Review New Sub-Account")}
<table border="0" cellpadding="4" cellspacing="1" width="100%">
<tr><td class="lbl">Member</td><td>{e(m.id)} &mdash; {e(m.last_name)}, {e(m.first_name)}</td></tr>
<tr><td class="lbl">Account Type</td><td>{e(pending.get("accountType", ""))}</td></tr>
<tr><td class="lbl">Nickname</td><td>{e(pending.get("nickname", ""))}</td></tr>
<tr><td class="lbl">Initial Deposit</td><td>{e(format_money(deposit))}</td></tr>
</table>
<p class="note"><b>Please review carefully.</b> Opening a sub-account posts a new account to the
member's record and cannot be undone from this console.</p>
<form method="post" action="/members/{e(m.id)}/sub-accounts/confirm" style="display:inline">
<input type="submit" value="Confirm &amp; Open Account"></form>
&nbsp; <a href="/members/{e(m.id)}">Cancel</a>"""
    return layout("Review New Sub-Account", body, user=user)


def sub_account_success_page(user: str, m: Member, account: Account) -> str:
    body = f"""{_title_bar("Sub-Account Opened")}
<p class="note">Sub-account <b>{e(account.number)}</b> ({e(account.type)}, &quot;{e(account.nickname)}&quot;)
has been opened for member {e(m.id)} with an initial deposit of {e(format_money(account.balance))}.</p>
<a href="/members/{e(m.id)}">Return to Member Detail</a>"""
    return layout("Sub-Account Opened", body, user=user)


def permission_denied_page(user: str, member_id: str) -> str:
    body = f"""{_title_bar("Access Denied")}
<p class="err">{e(MSG_PERMISSION_DENIED.format(member_id=member_id))}</p>
<p>Contact your supervisor to request access. (Policy ref: ACL-MEMBER-VIEW)</p>
<a href="/members/search">Back to Member Lookup</a>"""
    return layout("Access Denied", body, user=user)


def not_found_page(user: str, member_id: str) -> str:
    body = f"""{_title_bar("Member Not Found")}
<p class="err">{e(MSG_NOT_FOUND.format(member_id=member_id))}</p>
<a href="/members/search">Back to Member Lookup</a>"""
    return layout("Member Not Found", body, user=user)


def session_expired_page(next_url: str) -> str:
    body = f"""{_title_bar("Session Expired")}
<p class="err">{e(MSG_SESSION_EXPIRED)}</p>
<a href="/login?next={e(next_url)}">Log in again</a>"""
    return layout("Session Expired", body)


def system_notice_page(next_url: str, *, user: str | None) -> str:
    body = f"""{_title_bar("System Notice")}
<p class="note"><b>Scheduled Maintenance Notice:</b> The core system will be unavailable
Sunday 02:00&ndash;04:00 ET for scheduled maintenance. No action is required.</p>
<form method="post" action="/notice/ack">
<input type="hidden" name="next" value="{e(next_url)}">
<input type="submit" value="Continue"></form>"""
    return layout("System Notice", body, user=user)


def app_error_page(reference: str) -> str:
    body = f"""{_title_bar("Application Error")}
<p class="err">{e(MSG_APP_ERROR)}</p>
<p>Reference: <b>ERR-{e(reference)}</b>. Please try again or contact the help desk.</p>"""
    return layout("Application Error", body)
