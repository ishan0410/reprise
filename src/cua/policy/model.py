"""
Policy configuration: what the automation is permitted to do.

Loaded from TOML for an environment (see policies/), and embedded in
JSON form inside each capability artifact as the capability's declared
needs. At replay the two are intersected, so a capability can only ever
narrow what the environment allows.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from cua.artifact.actions import ACTION_TYPES, ActionType

RiskMode = Literal["block", "confirm", "allow"]
_MODE_STRICTNESS: dict[RiskMode, int] = {"allow": 0, "confirm": 1, "block": 2}

#: Built-in structured-PII patterns applied to every persisted log line. Tenants add their own.
DEFAULT_REDACTION_PATTERNS: tuple[str, ...] = (
    r"\b\d{3}-\d{2}-\d{4}\b",  # US SSN
    r"\b(?:\d[ -]?){13,19}\b",  # payment card numbers
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",  # email
    r"\b(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}\b",  # US phone
)

#: Control names that usually mean "commit something". A heuristic, so it only ever raises risk.
DEFAULT_RISKY_CONTROL_PATTERNS: tuple[str, ...] = (
    r"\bconfirm\b",
    r"\bapprove\b",
    r"\bdelete\b",
    r"\bremove\b",
    r"\btransfer\b",
    r"\bopen account\b",
    r"\bclose account\b",
    r"\bwire\b",
    r"\bpost\b",
    r"\bsubmit (?:transaction|payment|transfer)\b",
)


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """`**` matches across segments, `*` within one segment; everything else is literal."""
    out = "^"
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + "$")


class AllowlistPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: scheme://host[:port], compared case-insensitively. `${ENV_VAR}` is expanded at load time.
    origins: list[str]
    path_patterns: list[str] = Field(default_factory=lambda: ["/**"])
    #: Explicit denies win over allows (e.g. an app's admin or test-fixture routes).
    denied_path_patterns: list[str] = Field(default_factory=list)
    action_types: list[ActionType] = Field(default_factory=lambda: list(ACTION_TYPES))

    def allows_url(self, url: str) -> tuple[bool, str]:
        if url in ("", "about:blank"):
            return True, "blank page"
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}".lower()
        if origin not in {o.lower() for o in self.origins}:
            return False, f"origin {origin} is not in the allowlist"
        path = parsed.path or "/"
        for pat in self.denied_path_patterns:
            if glob_to_regex(pat).match(path):
                return False, f"path {path} is explicitly denied by {pat!r}"
        for pat in self.path_patterns:
            if glob_to_regex(pat).match(path):
                return True, f"path {path} allowed by {pat!r}"
        return False, f"path {path} matches no allowed path pattern"

    def intersect(self, other: AllowlistPolicy) -> AllowlistPolicy:
        mine = {o.lower() for o in self.origins}
        return AllowlistPolicy(
            origins=[o for o in other.origins if o.lower() in mine],
            # A URL must satisfy both allow-lists; keeping both sets is checked in Policy.allows_url.
            path_patterns=list(dict.fromkeys(self.path_patterns + other.path_patterns)),
            denied_path_patterns=list(dict.fromkeys(self.denied_path_patterns + other.denied_path_patterns)),
            action_types=[a for a in self.action_types if a in other.action_types],
        )


class RiskPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: What to do with a risky action: refuse it, require confirmation (human or pre-approval), or allow.
    mode: RiskMode = "confirm"
    risky_control_patterns: list[str] = Field(default_factory=lambda: list(DEFAULT_RISKY_CONTROL_PATTERNS))
    #: Glob patterns on the current path (click/press) or destination path (navigate).
    risky_url_patterns: list[str] = Field(default_factory=list)

    def intersect(self, other: RiskPolicy) -> RiskPolicy:
        stricter = max((self.mode, other.mode), key=lambda m: _MODE_STRICTNESS[m])
        return RiskPolicy(
            mode=stricter,
            risky_control_patterns=list(dict.fromkeys(self.risky_control_patterns + other.risky_control_patterns)),
            risky_url_patterns=list(dict.fromkeys(self.risky_url_patterns + other.risky_url_patterns)),
        )


class SecretsPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Names resolvable through {{secret:NAME}}. Anything else is refused, not silently empty.
    allowed: list[str] = Field(default_factory=list)


class RedactionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Added to DEFAULT_REDACTION_PATTERNS; never replaces them.
    extra_patterns: list[str] = Field(default_factory=list)

    @property
    def patterns(self) -> list[str]:
        return [*DEFAULT_REDACTION_PATTERNS, *self.extra_patterns]


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    allowlist: AllowlistPolicy
    risk: RiskPolicy = Field(default_factory=RiskPolicy)
    secrets: SecretsPolicy = Field(default_factory=SecretsPolicy)
    redaction: RedactionPolicy = Field(default_factory=RedactionPolicy)
    #: Present only on an intersected policy: both allow-lists must pass.
    _extra_allowlists: list[AllowlistPolicy] = []

    @classmethod
    def load(cls, path: Path) -> Policy:
        with path.open("rb") as f:
            raw = tomllib.load(f)
        allowlist = raw.get("allowlist", {})
        if isinstance(allowlist, dict) and isinstance(allowlist.get("origins"), list):
            allowlist["origins"] = [os.path.expandvars(str(o)) for o in allowlist["origins"]]
        return cls.model_validate(raw)

    def allows_url(self, url: str) -> tuple[bool, str]:
        for allowlist in (self.allowlist, *self._extra_allowlists):
            ok, reason = allowlist.allows_url(url)
            if not ok:
                return False, reason
        return True, "allowed"

    def intersect(self, other: Policy) -> Policy:
        """Both policies must permit an action for the result to permit it."""
        merged = Policy(
            name=f"{self.name} ∩ {other.name}",
            allowlist=self.allowlist.intersect(other.allowlist),
            risk=self.risk.intersect(other.risk),
            secrets=SecretsPolicy(allowed=[s for s in self.secrets.allowed if s in other.secrets.allowed]),
            redaction=RedactionPolicy(
                extra_patterns=list(dict.fromkeys(self.redaction.extra_patterns + other.redaction.extra_patterns))
            ),
        )
        merged._extra_allowlists = [self.allowlist, other.allowlist]
        return merged
