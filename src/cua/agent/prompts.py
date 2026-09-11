"""Prompts for the discovery loop."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from cua.surface.base import Observation

SYSTEM_PROMPT = """\
You are operating a legacy back-office business application on behalf of a bank operator, \
through a browser, to accomplish one task. You are in a discovery run: a successful run is \
recorded and turned into a reusable, deterministic automation, so act the way a careful human \
operator would and take the most direct route.

Each turn you receive the current page as an accessibility snapshot (every element has a \
[ref=...] id) and, when available, a screenshot. Respond with exactly ONE tool call per turn.

Rules:
- Interact only with elements from the current snapshot, by their ref.
- Never invent values. Use only the inputs and secret references the task provides. When a \
value is given as {{secret:NAME}} or {{input:NAME}}, pass that reference text verbatim; the \
system substitutes the real value and never shows it to you.
- Use `extract` to capture every value the task asks for before calling `done`. Mark \
balances, account numbers, and personal information as sensitive.
- If the application shows an error, a notice, or an unexpected screen, deal with it \
(dismiss a notice, sign in again if the session expired) and continue.
- If the task's request turns out to be impossible for a legitimate business reason \
(for example the record does not exist), that is a valid result: call `done` and say so in the summary.
- Do not perform destructive or irreversible actions (confirming, approving, deleting, \
transferring) unless the task explicitly asks for that exact action. The policy layer will \
refuse anything outside its allowlist; if it refuses an action, do not repeat it, find another \
way or call `stuck`.
- Stay inside the application; never navigate to other sites.
- If you cannot make safe progress, call `stuck` with the reason.
"""


def build_user_message(
    *,
    goal: str,
    inputs: Mapping[str, str],
    sensitive_inputs: Iterable[str],
    secret_names: Iterable[str],
    history: list[str],
    observation: Observation,
    step_index: int,
    max_steps: int,
) -> str:
    sensitive = set(sensitive_inputs)
    input_lines = [
        f"  - {name} = " + (f"{{{{input:{name}}}}}  (sensitive: pass the reference verbatim)" if name in sensitive else value)
        for name, value in inputs.items()
    ] or ["  (none)"]
    secret_lines = [f"  - {{{{secret:{n}}}}}" for n in secret_names] or ["  (none)"]
    history_block = "\n".join(history) if history else "  (no actions yet)"
    dialogs = ""
    if observation.dialogs:
        dialogs = "\nNative dialogs since last turn (auto-dismissed): " + "; ".join(observation.dialogs) + "\n"
    return (
        f"TASK: {goal}\n\n"
        f"Inputs provided:\n" + "\n".join(input_lines) + "\n\n"
        "Secret references available (use verbatim where a credential is required):\n"
        + "\n".join(secret_lines)
        + "\n\n"
        f"Actions so far (step {step_index + 1} of at most {max_steps}):\n{history_block}\n"
        f"{dialogs}\n"
        f"Current page: {observation.title}\nURL: {observation.url}\n\n"
        f"Accessibility snapshot:\n{observation.snapshot}\n"
    )
