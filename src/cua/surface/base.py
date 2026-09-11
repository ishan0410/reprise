"""
Surface adapter contract.

This is the seam between "how we perceive and act on a surface" and "the
recorded flow". Both the LLM-driven discovery loop and the deterministic
replay engine talk to a Surface; neither touches Playwright (or any other
driver) directly. Supporting a desktop application means writing a new
Surface, not changing the artifact schema or the replay engine.

Two ways to get a Handle on a control:
    handle_for_ref(ref)   discovery-time: an ephemeral ref from the last observe()
    resolve(descriptor)   replay-time: a surface-agnostic TargetDescriptor
Every action then takes a Handle, so the action code path is shared.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from cua.artifact.targets import BBox, Locator, RecordedElement, TargetDescriptor

ReadAttribute = Literal["text", "value"]


@dataclass(frozen=True)
class ElementInfo:
    """One node from an observation, keyed by its ephemeral ref."""

    ref: str
    role: str
    name: str | None


@dataclass
class Observation:
    url: str
    title: str
    #: Accessibility-tree text with [ref=...] annotations. The primary signal for the model.
    snapshot: str
    elements: dict[str, ElementInfo]
    #: PNG. Supplementary grounding for vision-capable models; evidence for humans.
    screenshot_png: bytes
    #: Native dialogs (alert/confirm) that fired since the previous observation; auto-dismissed.
    dialogs: list[str] = field(default_factory=list)


@dataclass
class Handle:
    """
    A resolved control. `native` is adapter-specific (a Playwright Locator
    here; a UI Automation element for a desktop adapter). Replay logs
    candidate_index: anything above 0 means the primary strategy failed and a
    fallback was used, which is our UI-drift signal.
    """

    native: Any
    candidate: Locator
    candidate_index: int = 0
    ref: str | None = None
    bbox: BBox | None = None

    @property
    def used_fallback(self) -> bool:
        return self.candidate_index > 0


class SurfaceError(Exception):
    """Base for adapter-level failures."""


class UnknownRef(SurfaceError):
    def __init__(self, ref: str) -> None:
        super().__init__(f"ref {ref!r} is not in the current observation")
        self.ref = ref


class ActionFailed(SurfaceError):
    """The driver could not perform an action (element not editable, detached, navigation blocked...)."""


class TargetNotFound(SurfaceError):
    def __init__(self, target: TargetDescriptor, attempts: list[str]) -> None:
        tried = "; ".join(attempts) or "no candidates"
        super().__init__(f"could not resolve {target.description!r}: tried {tried}")
        self.target = target
        self.attempts = attempts


class Surface(ABC):
    """Perception + action for one live session of one application surface."""

    # ---------------------------------------------------------------- session
    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def url(self) -> str: ...

    # ------------------------------------------------------------- perception
    @abstractmethod
    def observe(self) -> Observation: ...

    @abstractmethod
    def screenshot(self) -> bytes: ...

    @abstractmethod
    def exists(self, locator: Locator, timeout_ms: int = 0) -> bool:
        """True if the locator currently matches at least one visible element."""

    @abstractmethod
    def describe(self, handle: Handle) -> RecordedElement:
        """Capture what the control looks like right now (for the artifact's `recorded` field)."""

    # ------------------------------------------------------------- resolution
    @abstractmethod
    def handle_for_ref(self, ref: str) -> Handle: ...

    @abstractmethod
    def resolve(self, target: TargetDescriptor, timeout_ms: int | None = None) -> Handle:
        """Try each candidate in order until one uniquely matches; raise TargetNotFound otherwise."""

    # ----------------------------------------------------------------- action
    @abstractmethod
    def navigate(self, url: str) -> None: ...

    @abstractmethod
    def click(self, handle: Handle) -> None: ...

    @abstractmethod
    def type_text(self, handle: Handle, text: str) -> None:
        """Replace the control's content with `text`. Callers must never log `text` if it is sensitive."""

    @abstractmethod
    def select_option(self, handle: Handle, label: str) -> None: ...

    @abstractmethod
    def press(self, key: str) -> None: ...

    @abstractmethod
    def read(self, handle: Handle, attribute: ReadAttribute = "text") -> str: ...

    @abstractmethod
    def settle(self, timeout_ms: int | None = None) -> None:
        """Wait for any in-flight navigation/load to finish. No-op if the page is idle."""
