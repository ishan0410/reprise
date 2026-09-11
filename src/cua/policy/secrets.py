"""
Secret references.

Values typed into the surface may contain `{{secret:NAME}}`. The reference,
never the value, is what the model sees, what the artifact stores, and what
the logs record. Resolution happens in the executor immediately before the
surface types the value, and only for names the policy allows.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping

SECRET_REF = re.compile(r"\{\{\s*secret:([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class SecretError(Exception):
    pass


class SecretNotAllowed(SecretError):
    def __init__(self, name: str) -> None:
        super().__init__(f"secret {name!r} is not in the policy's allowed list")
        self.name = name


class SecretMissing(SecretError):
    def __init__(self, name: str) -> None:
        super().__init__(f"secret {name!r} is allowed but not set in the environment")
        self.name = name


def secret_names_in(text: str) -> list[str]:
    return SECRET_REF.findall(text)


class SecretStore:
    def __init__(self, allowed: Iterable[str], env: Mapping[str, str] | None = None) -> None:
        self._allowed = set(allowed)
        self._env = env if env is not None else os.environ

    def resolve(self, text: str) -> tuple[str, bool]:
        """Return (text with references substituted, whether any secret was involved)."""
        involved = False

        def substitute(m: re.Match[str]) -> str:
            nonlocal involved
            name = m.group(1)
            if name not in self._allowed:
                raise SecretNotAllowed(name)
            value = self._env.get(name)
            if value is None or value == "":
                raise SecretMissing(name)
            involved = True
            return value

        return SECRET_REF.sub(substitute, text), involved

    def known_values(self) -> dict[str, str]:
        """name -> value for every allowed secret that is set. Feeds the redactor."""
        return {n: self._env[n] for n in self._allowed if self._env.get(n)}
