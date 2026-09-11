"""
Fault injection for the mock target app.

This is a TEST FIXTURE, not part of the automated surface. It lets a demo or
test arm the *next* matching request to exhibit a runtime condition, so the
replay engine and the capability artifact stay completely oblivious to how
the failure was triggered. That mirrors production, where these conditions
arise on their own.

The /__admin routes that control it are deliberately excluded from the
agent's allowlist, which also gives us a concrete route-allowlisting demo.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Literal, cast

FaultKind = Literal["slow_load", "app_error", "session_timeout", "interstitial_notice"]
FAULT_KINDS: tuple[FaultKind, ...] = ("slow_load", "app_error", "session_timeout", "interstitial_notice")

#: Paths the injector never touches, so recovery flows (re-login, dismissing a notice) stay deterministic.
_EXEMPT_PREFIXES = ("/__admin", "/notice", "/login", "/logout")


@dataclass
class ArmedFault:
    kind: FaultKind
    #: How many more matching requests this fault applies to.
    remaining: int
    #: Only used by slow_load.
    delay_ms: int


class FaultInjector:
    def __init__(self) -> None:
        self._queue: list[ArmedFault] = []

    def arm(self, kind: FaultKind, count: int = 1, delay_ms: int = 3000) -> ArmedFault:
        fault = ArmedFault(kind=kind, remaining=max(1, count), delay_ms=delay_ms)
        self._queue.append(fault)
        return fault

    def consume(self) -> ArmedFault | None:
        """Return the fault to apply to the current request, decrementing its budget."""
        if not self._queue:
            return None
        head = self._queue[0]
        head.remaining -= 1
        if head.remaining <= 0:
            self._queue.pop(0)
        return head

    def clear(self) -> None:
        self._queue.clear()

    def status(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], asdict(f)) for f in self._queue]


faults = FaultInjector()


def is_exempt_path(path: str) -> bool:
    return any(path == p or path.startswith(f"{p}/") for p in _EXEMPT_PREFIXES)


def take_fault(path: str) -> ArmedFault | None:
    """
    Decide whether a fault fires for this request.

    slow_load is applied inline (it has no page to render). Every other kind is
    returned so the server can render the matching page; this module stays free
    of view and session concerns.
    """
    if is_exempt_path(path):
        return None
    fault = faults.consume()
    if fault is None:
        return None
    if fault.kind == "slow_load":
        time.sleep(fault.delay_ms / 1000)
        return None
    return fault
