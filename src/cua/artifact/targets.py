"""
Element-targeting vocabulary shared by the artifact schema, the surface
adapters, and the replay engine.

A TargetDescriptor is *how the recorded flow refers to a control*. It is
expressed in surface-agnostic terms (roles, accessible names, text,
structural relationships) as a ranked list of candidate strategies, so a
different surface adapter (legacy web, desktop accessibility API) can resolve
the same descriptor in its own way. Ephemeral discovery-time refs never
appear here.

Candidate order encodes our robustness ranking:
    role+name  > label/text  > css/xpath  > bbox
Role and accessible name are what a screen reader sees; they survive
restyling, re-layout, and (unlike CSS) mostly survive markup rewrites.
CSS/XPath are markup-bound. Bounding boxes are viewport-bound and are only
ever a last resort for click targets.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

LocatorStrategy = Literal["role", "label", "text", "css", "xpath", "bbox"]


class BBox(BaseModel):
    model_config = ConfigDict(frozen=True)

    x: float
    y: float
    width: float
    height: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2, self.y + self.height / 2)


class Locator(BaseModel):
    """One strategy for finding a control. An ordered list of these is a fallback chain."""

    model_config = ConfigDict(extra="forbid")

    by: LocatorStrategy
    #: by=role: ARIA role (textbox, button, link, cell, row, ...) and optional accessible name.
    role: str | None = None
    name: str | None = None
    #: by=text / by=label: the visible text or label to match.
    text: str | None = None
    #: by=css / by=xpath.
    selector: str | None = None
    #: by=bbox: viewport coordinates recorded at discovery time (see TargetSurface.viewport).
    bbox: BBox | None = None
    #: Whole-string match for name/text instead of substring.
    exact: bool = False
    #: Pick the n-th match (0-based) instead of requiring a unique match.
    nth: int | None = None
    #: Resolve inside this scope instead of the whole page.
    within: Locator | None = None
    #: After matching, climb to the nearest ancestor with this role (e.g. "row").
    #: Lets "the cell labelled Savings" become "the row containing it" without markup knowledge.
    ancestor_role: str | None = None

    @model_validator(mode="after")
    def _require_strategy_fields(self) -> Locator:
        required: dict[str, tuple[str, ...]] = {
            "role": ("role",),
            "label": ("text",),
            "text": ("text",),
            "css": ("selector",),
            "xpath": ("selector",),
            "bbox": ("bbox",),
        }
        for attr in required[self.by]:
            if getattr(self, attr) is None:
                raise ValueError(f"locator by={self.by!r} requires {attr!r}")
        return self

    def describe(self) -> str:
        """Compact human-readable form used in logs and error messages."""
        parts: list[str] = [self.by]
        if self.role:
            parts.append(f"role={self.role}")
        if self.name is not None:
            parts.append(f'name="{self.name}"')
        if self.text is not None:
            parts.append(f'text="{self.text}"')
        if self.selector:
            parts.append(f"selector={self.selector}")
        if self.bbox:
            parts.append(f"bbox=({self.bbox.x:.0f},{self.bbox.y:.0f},{self.bbox.width:.0f}x{self.bbox.height:.0f})")
        if self.exact:
            parts.append("exact")
        if self.nth is not None:
            parts.append(f"nth={self.nth}")
        if self.ancestor_role:
            parts.append(f"^{self.ancestor_role}")
        out = " ".join(parts)
        if self.within is not None:
            out += f" within({self.within.describe()})"
        return out


Locator.model_rebuild()


class RecordedElement(BaseModel):
    """
    What the control looked like at discovery time. A review and debugging
    aid (and the raw material for drift diagnosis); never used for resolution.
    """

    model_config = ConfigDict(extra="forbid")

    role: str | None = None
    name: str | None = None
    tag: str | None = None
    text: str | None = None
    css: str | None = None
    bbox: BBox | None = None
    #: For table cells: the text of every cell in the same row, and this cell's index in it.
    row_cells: list[str] | None = None
    cell_index: int | None = None


class TargetDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Human-readable purpose, e.g. "Member ID search box". For reviewers and error messages.
    description: str
    #: Ranked fallback chain. Replay tries these in order and records which one resolved.
    candidates: list[Locator] = Field(min_length=1)
    recorded: RecordedElement | None = None
