"""
Browser-level tests for PlaywrightSurface against the mock app.

These pin down the behaviours the replay engine's determinism rests on:
ref-based action, candidate fallback order, ambiguity handling, anchor-
relative table extraction on nested legacy tables, and last-resort bbox.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from cua.artifact.targets import BBox, Locator, TargetDescriptor
from cua.surface.base import TargetNotFound
from cua.surface.playwright_surface import PlaywrightSurface

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def surface() -> Iterator[PlaywrightSurface]:
    s = PlaywrightSurface(headless=True, default_timeout_ms=5_000)
    s.open()
    try:
        yield s
    finally:
        s.close()


def role(role_: str, name: str | None = None, **kw: object) -> Locator:
    return Locator(by="role", role=role_, name=name, **kw)  # type: ignore[arg-type]


def target(*candidates: Locator, description: str = "t") -> TargetDescriptor:
    return TargetDescriptor(description=description, candidates=list(candidates))


def login(surface: PlaywrightSurface, base: str) -> None:
    surface.navigate(f"{base}/login")
    surface.type_text(surface.resolve(target(role("textbox", "Username"))), "teller1")
    surface.type_text(surface.resolve(target(role("textbox", "Password"))), "demo-pass-2024")
    surface.click(surface.resolve(target(role("button", "Sign In"))))


def open_member(surface: PlaywrightSurface, base: str, member_id: str) -> None:
    login(surface, base)
    surface.type_text(surface.resolve(target(role("textbox", "Member ID"))), member_id)
    surface.click(surface.resolve(target(role("button", "Search"))))


# --------------------------------------------------------------- perception


def test_observe_exposes_refs_with_roles_names_and_screenshot(surface: PlaywrightSurface, mock_app: str) -> None:
    surface.navigate(f"{mock_app}/login")
    obs = surface.observe()
    assert obs.title.startswith("Staff Sign In")
    by_name = {(e.role, e.name): e.ref for e in obs.elements.values()}
    assert ("textbox", "Username") in by_name
    assert ("button", "Sign In") in by_name
    assert "[ref=" in obs.snapshot
    assert obs.screenshot_png[:8] == b"\x89PNG\r\n\x1a\n"


def test_act_via_ephemeral_refs_completes_login(surface: PlaywrightSurface, mock_app: str) -> None:
    surface.navigate(f"{mock_app}/login")
    obs = surface.observe()
    ref_of = {(e.role, e.name): e.ref for e in obs.elements.values()}
    surface.type_text(surface.handle_for_ref(ref_of[("textbox", "Username")]), "teller1")
    surface.type_text(surface.handle_for_ref(ref_of[("textbox", "Password")]), "demo-pass-2024")
    surface.click(surface.handle_for_ref(ref_of[("button", "Sign In")]))
    assert surface.url().endswith("/members/search")
    # refs are renumbered per observation; the old ones must not be usable blindly
    obs2 = surface.observe()
    assert ("textbox", "Member ID") in {(e.role, e.name) for e in obs2.elements.values()}


def test_describe_captures_css_path_and_row_context(surface: PlaywrightSurface, mock_app: str) -> None:
    open_member(surface, mock_app, "10001")
    cell = surface.resolve(target(role("cell", "$4,210.55", exact=True)))
    rec = surface.describe(cell)
    assert rec.tag == "td"
    assert rec.row_cells == ["Savings", "S-10001-01", "Primary Savings", "$4,210.55"]
    assert rec.cell_index == 3
    assert rec.css and rec.css.startswith("body")
    assert rec.bbox and rec.bbox.width > 0


# --------------------------------------------------------------- resolution


def test_resolve_prefers_primary_and_reports_fallback_index(surface: PlaywrightSurface, mock_app: str) -> None:
    login(surface, mock_app)
    primary_ok = surface.resolve(target(role("textbox", "Member ID"), Locator(by="css", selector="input[name=memberId]")))
    assert primary_ok.candidate_index == 0 and not primary_ok.used_fallback

    broken_primary = surface.resolve(
        target(Locator(by="css", selector="#renamed-in-this-version"), role("textbox", "Member ID")),
        timeout_ms=1500,
    )
    assert broken_primary.candidate_index == 1 and broken_primary.used_fallback


def test_resolve_skips_ambiguous_candidates(surface: PlaywrightSurface, mock_app: str) -> None:
    login(surface, mock_app)
    h = surface.resolve(target(role("cell"), role("button", "Search")), timeout_ms=1500)
    assert h.candidate_index == 1


def test_resolve_raises_target_not_found_with_every_attempt(surface: PlaywrightSurface, mock_app: str) -> None:
    login(surface, mock_app)
    with pytest.raises(TargetNotFound) as ei:
        surface.resolve(
            target(role("button", "Approve Wire"), Locator(by="css", selector="#nope"), description="approve button"),
            timeout_ms=600,
        )
    msg = str(ei.value)
    assert "approve button" in msg and "matched 0" in msg and "#nope" in msg
    assert len(ei.value.attempts) == 2


def test_bbox_is_used_only_after_other_candidates_fail(surface: PlaywrightSurface, mock_app: str) -> None:
    login(surface, mock_app)
    box = surface.resolve(target(role("button", "Search"))).native.bounding_box()
    h = surface.resolve(
        target(Locator(by="css", selector="#gone"), Locator(by="bbox", bbox=BBox(**box))),
        timeout_ms=600,
    )
    assert h.candidate_index == 1 and h.bbox is not None and h.native is None
    surface.type_text(surface.resolve(target(role("textbox", "Member ID"))), "10001")
    surface.click(h)
    assert surface.url().endswith("/members/10001")


# ----------------------------------------------------- legacy-table extraction


def test_anchor_relative_extraction_on_nested_tables(surface: PlaywrightSurface, mock_app: str) -> None:
    open_member(surface, mock_app, "10001")
    # "the 4th cell of the row that contains the cell named exactly 'Savings'"
    balance = role("cell", nth=3, within=role("cell", "Savings", exact=True, ancestor_role="row"))
    h = surface.resolve(target(balance))
    assert surface.read(h) == "$4,210.55"
    checking = role("cell", nth=3, within=role("cell", "Checking", exact=True, ancestor_role="row"))
    assert surface.read(surface.resolve(target(checking))) == "$1,325.10"


def test_read_value_of_form_controls(surface: PlaywrightSurface, mock_app: str) -> None:
    login(surface, mock_app)
    box = surface.resolve(target(role("textbox", "Member ID")))
    surface.type_text(box, "12345")
    assert surface.read(box) == "12345"
    assert surface.read(box, "value") == "12345"


def test_exists_detects_messages_and_absence(surface: PlaywrightSurface, mock_app: str) -> None:
    open_member(surface, mock_app, "99999")
    assert surface.exists(Locator(by="text", text="No member found for ID 99999."))
    assert not surface.exists(Locator(by="text", text="Member Detail"))
    assert surface.exists(Locator(by="role", role="button", name="Search"), timeout_ms=500)
