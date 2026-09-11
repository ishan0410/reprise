"""Parse Playwright's AI-mode accessibility snapshot into ref -> ElementInfo."""

from __future__ import annotations

import re

from .base import ElementInfo

# Matches lines such as:
#   - textbox "Member ID" [ref=e23]
#   - heading "Staff Sign In" [level=2] [ref=e16]
#   - link "Log out" [ref=f2e10] [cursor=pointer]:
#   - cell [ref=e7]
_REF_LINE = re.compile(
    r'^\s*-\s+(?P<role>[a-z]+)(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?[^\n]*?\[ref=(?P<ref>[a-z0-9]+)\]'
)


def parse_refs(snapshot: str) -> dict[str, ElementInfo]:
    elements: dict[str, ElementInfo] = {}
    for line in snapshot.splitlines():
        m = _REF_LINE.match(line)
        if not m:
            continue
        raw_name = m.group("name")
        name = raw_name.replace('\\"', '"') if raw_name is not None else None
        ref = m.group("ref")
        elements[ref] = ElementInfo(ref=ref, role=m.group("role"), name=name)
    return elements
