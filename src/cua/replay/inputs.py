"""Input validation against the artifact's contract, and output typing."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from cua.artifact.schema import CapabilityArtifact, OutputType


class InputValidationError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def validate_inputs(artifact: CapabilityArtifact, inputs: Mapping[str, str]) -> dict[str, str]:
    problems: list[str] = []
    declared = {p.name: p for p in artifact.inputs}
    for name in inputs:
        if name not in declared:
            problems.append(f"unknown input {name!r}")
    for param in artifact.inputs:
        value = inputs.get(param.name)
        if value is None or value == "":
            if param.required:
                problems.append(f"missing required input {param.name!r}")
            continue
        if param.pattern and not re.fullmatch(param.pattern, value):
            problems.append(f"input {param.name!r} does not match {param.pattern!r}")
        if param.enum and value not in param.enum:
            problems.append(f"input {param.name!r} must be one of {param.enum}")
        if param.type == "integer" and not re.fullmatch(r"-?\d+", value):
            problems.append(f"input {param.name!r} must be an integer")
        if param.type == "number":
            try:
                float(value)
            except ValueError:
                problems.append(f"input {param.name!r} must be a number")
        if param.type == "boolean" and value.lower() not in ("true", "false"):
            problems.append(f"input {param.name!r} must be true or false")
    if problems:
        raise InputValidationError(problems)
    return {k: v for k, v in inputs.items() if k in declared}


def parse_output(value: str, type_: OutputType) -> Any:
    v = value.strip()
    if type_ == "money":
        cleaned = re.sub(r"[^\d.\-]", "", v)
        return float(cleaned) if cleaned not in ("", "-", ".") else None
    if type_ == "number":
        try:
            return float(v.replace(",", ""))
        except ValueError:
            return None
    if type_ == "integer":
        try:
            return int(v.replace(",", ""))
        except ValueError:
            return None
    if type_ == "boolean":
        return v.lower() in ("true", "yes", "1")
    return v
