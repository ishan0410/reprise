"""Artifact persistence: artifacts/<name>/v<version>.json, plus the JSON Schema of the format."""

from __future__ import annotations

import json
import re
from pathlib import Path

from cua.artifact.schema import CapabilityArtifact
from cua.policy.redaction import Redactor

_VERSION_FILE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)\.json$")


def artifact_dir(root: Path, name: str) -> Path:
    return root / name


def existing_versions(root: Path, name: str) -> list[tuple[int, int, int]]:
    d = artifact_dir(root, name)
    if not d.exists():
        return []
    out: list[tuple[int, int, int]] = []
    for p in d.iterdir():
        m = _VERSION_FILE.match(p.name)
        if m:
            out.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    return sorted(out)


def next_version(root: Path, name: str) -> str:
    """1.0.0 for a new capability; otherwise bump the minor version of the latest recording."""
    versions = existing_versions(root, name)
    if not versions:
        return "1.0.0"
    major, minor, _ = versions[-1]
    return f"{major}.{minor + 1}.0"


def save_artifact(root: Path, artifact: CapabilityArtifact, *, redactor: Redactor | None = None) -> Path:
    """
    Persist the artifact. The builder already keeps values out of the artifact by
    construction; the optional redactor is a safety net so a secret or sensitive
    value can never reach disk even if a future builder change regresses that.
    """
    d = artifact_dir(root, artifact.name)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"v{artifact.version}.json"
    data = artifact.model_dump(mode="json", exclude_none=True)
    if redactor is not None:
        # Redact string values only. Running the patterns over the serialised text can
        # rewrite a number (a phone-shaped float such as a bbox y of 123.4140625) and
        # leave invalid JSON behind.
        data = redactor.redact_obj(data)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load_artifact(path: Path) -> CapabilityArtifact:
    return CapabilityArtifact.model_validate_json(path.read_text(encoding="utf-8"))


def write_json_schema(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    schema = CapabilityArtifact.model_json_schema()
    path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    return path
