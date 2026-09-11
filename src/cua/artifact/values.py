"""Input references: `{{input:NAME}}` placeholders resolved from per-invocation parameters."""

from __future__ import annotations

import re
from collections.abc import Mapping

INPUT_REF = re.compile(r"\{\{\s*input:([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class UnknownInput(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"input {name!r} was referenced but not provided")
        self.name = name


def input_names_in(text: str) -> list[str]:
    return INPUT_REF.findall(text)


def render_inputs(text: str, inputs: Mapping[str, str]) -> str:
    def substitute(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in inputs:
            raise UnknownInput(name)
        return inputs[name]

    return INPUT_REF.sub(substitute, text)
