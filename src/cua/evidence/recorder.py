"""
Run evidence: one directory per run, everything redacted on the way to disk.

    <root>/<run_id>/
        run.jsonl           structured event log (what happened, and why)
        transcript.jsonl    per-step model request/response summaries (discovery only)
        trace.json / result.json
        screenshots/        PNGs, subject to the screenshot policy

Screenshot policy: "full" for discovery evidence; "on_failure" is the
production default for replay so routine runs do not accumulate images of
regulated data; "none" disables them.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from cua.policy.redaction import Redactor

ScreenshotMode = Literal["full", "on_failure", "none"]


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


class RunRecorder:
    def __init__(
        self,
        root: Path,
        kind: str,
        *,
        redactor: Redactor,
        run_id: str | None = None,
        screenshot_mode: ScreenshotMode = "full",
    ) -> None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = run_id or f"{kind}-{stamp}-{secrets.token_hex(2)}"
        self.kind = kind
        self.redactor = redactor
        self.screenshot_mode = screenshot_mode
        self.dir = root / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "screenshots").mkdir(exist_ok=True)
        self._events = (self.dir / "run.jsonl").open("a", encoding="utf-8")

    # ---------------------------------------------------------------- writes
    def event(self, kind: str, **fields: Any) -> None:
        record = {"ts": datetime.now(UTC).isoformat(), "kind": kind, **fields}
        self._events.write(json.dumps(self.redactor.redact_obj(_jsonable(record))) + "\n")
        self._events.flush()

    def append_jsonl(self, name: str, obj: Any) -> None:
        with (self.dir / name).open("a", encoding="utf-8") as f:
            f.write(json.dumps(self.redactor.redact_obj(_jsonable(obj))) + "\n")

    def write_json(self, name: str, obj: Any) -> Path:
        path = self.dir / name
        path.write_text(json.dumps(self.redactor.redact_obj(_jsonable(obj)), indent=2), encoding="utf-8")
        return path

    def screenshot(self, png: bytes, label: str, *, force: bool = False) -> str | None:
        """Save a PNG and return its path relative to the run dir, or None if policy skipped it."""
        if self.screenshot_mode == "none" and not force:
            return None
        if self.screenshot_mode == "on_failure" and not force:
            return None
        rel = f"screenshots/{label}.png"
        (self.dir / rel).write_bytes(png)
        return rel

    def close(self) -> None:
        self._events.close()
