"""
Synthetic member dataset for the mock credit-union portal.

Every value here is fabricated. No real people, accounts, or balances.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Literal

AccountType = Literal["Savings", "Checking", "Money Market"]
ACCOUNT_TYPES: tuple[AccountType, ...] = ("Savings", "Checking", "Money Market")

#: Looking up this member ID renders a permission-denied page (simulates an authorization failure).
PERMISSION_DENIED_MEMBER_ID = "40403"

#: Demo-only fixture credentials for the mock app. Not a real secret; documented in README.
DEMO_CREDENTIALS = {"username": "teller1", "password": "demo-pass-2024"}


@dataclass
class Account:
    type: AccountType
    number: str
    nickname: str
    balance: float


@dataclass
class Member:
    id: str
    first_name: str
    last_name: str
    member_since: str
    accounts: list[Account] = field(default_factory=list)


_SEED: list[Member] = [
    Member(
        id="10001",
        first_name="Jane",
        last_name="Sample",
        member_since="2015-03-12",
        accounts=[
            Account("Savings", "S-10001-01", "Primary Savings", 4210.55),
            Account("Checking", "C-10001-01", "Everyday Checking", 1325.10),
        ],
    ),
    Member(
        id="10002",
        first_name="John",
        last_name="Example",
        member_since="2019-07-30",
        accounts=[Account("Savings", "S-10002-01", "Primary Savings", 980.00)],
    ),
    Member(
        id="10003",
        first_name="Maria",
        last_name="Testcase",
        member_since="2021-11-02",
        accounts=[
            Account("Savings", "S-10003-01", "Primary Savings", 15780.42),
            Account("Checking", "C-10003-01", "Everyday Checking", 2200.00),
            Account("Money Market", "M-10003-01", "Rainy Day Fund", 50000.00),
        ],
    ),
]

# Mutable in-memory store. reset_members() restores the seed so demos and tests are repeatable.
_members: list[Member] = copy.deepcopy(_SEED)


def reset_members() -> None:
    global _members
    _members = copy.deepcopy(_SEED)


def find_member(member_id: str) -> Member | None:
    return next((m for m in _members if m.id == member_id), None)


def add_sub_account(member_id: str, account_type: AccountType, nickname: str, deposit: float) -> Account:
    member = find_member(member_id)
    if member is None:
        raise KeyError(f"member {member_id} not found")
    prefix = {"Savings": "S", "Checking": "C", "Money Market": "M"}[account_type]
    seq = f"{len(member.accounts) + 1:02d}"
    account = Account(account_type, f"{prefix}-{member_id}-{seq}", nickname, deposit)
    member.accounts.append(account)
    return account


def format_money(amount: float) -> str:
    return f"${amount:,.2f}"
