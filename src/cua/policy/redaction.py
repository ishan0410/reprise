"""
Redaction for everything that gets persisted: logs, transcripts, artifacts.

Three layers, applied in order of specificity:
  1. known secret values         -> [REDACTED:secret:NAME]
  2. declared-sensitive values   -> [REDACTED:field]   (e.g. an extracted balance)
  3. structured-PII regexes      -> [REDACTED:pii]
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

_MIN_VALUE_LEN = 3  # never substitute tiny values; they would shred unrelated text


class Redactor:
    def __init__(
        self,
        patterns: Iterable[str] = (),
        *,
        secrets: Mapping[str, str] | None = None,
        sensitive: Mapping[str, str] | None = None,
    ) -> None:
        self._patterns = [re.compile(p) for p in patterns]
        self._secrets: dict[str, str] = dict(secrets or {})
        self._sensitive: dict[str, str] = dict(sensitive or {})

    def add_sensitive(self, name: str, value: str) -> None:
        """Register a value learned at runtime (e.g. an extracted sensitive output)."""
        if value:
            self._sensitive[name] = value

    def redact(self, text: str) -> str:
        for name, value in self._by_length(self._secrets):
            text = text.replace(value, f"[REDACTED:secret:{name}]")
        for name, value in self._by_length(self._sensitive):
            text = text.replace(value, f"[REDACTED:{name}]")
        for pat in self._patterns:
            text = pat.sub("[REDACTED:pii]", text)
        return text

    def redact_obj(self, obj: Any) -> Any:
        """Recursively redact every string inside dicts/lists/tuples (keys are left alone)."""
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact_obj(v) for v in obj]
        if isinstance(obj, tuple):
            return tuple(self.redact_obj(v) for v in obj)
        return obj

    @staticmethod
    def _by_length(values: Mapping[str, str]) -> list[tuple[str, str]]:
        usable = [(n, v) for n, v in values.items() if len(v) >= _MIN_VALUE_LEN]
        return sorted(usable, key=lambda nv: len(nv[1]), reverse=True)
