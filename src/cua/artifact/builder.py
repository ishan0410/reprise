"""
Trace -> CapabilityArtifact.

This is where a successful discovery run becomes a reusable capability.
The model chose *what* to interact with; this module decides *how to find
it again*, parameterises the flow, splits it into phases, derives
checkpoints from what was observed after each step, and merges the
reviewed per-application conditions.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import urlparse

from cua.agent.trace import Trace, TraceStep
from cua.artifact.actions import ActionType
from cua.artifact.schema import (
    CapabilityArtifact,
    Checkpoint,
    DeclaredConditions,
    InputParam,
    OutputField,
    OutputType,
    Provenance,
    Step,
    SuccessCondition,
    TargetSurface,
)
from cua.artifact.targets import Locator, RecordedElement, TargetDescriptor
from cua.policy.model import AllowlistPolicy, Policy, RiskPolicy, SecretsPolicy
from cua.policy.secrets import secret_names_in

BASE_URL_REF = "{{base_url}}"
_MONEY = re.compile(r"^-?[$€£]\s?-?[\d,]+(?:\.\d{1,2})?$")
_NUMBER = re.compile(r"^-?[\d,]*\.?\d+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$|^\d{1,2}/\d{1,2}/\d{2,4}$")


# ------------------------------------------------------------------ locators
def derive_target(element: RecordedElement, *, description: str, value_independent: bool = False) -> TargetDescriptor:
    """
    Build the fallback chain for a recorded element.

    Controls: role+name (what a screen reader sees) > visible text > css path > bbox.
    Extracted values (value_independent=True): the element's own text will change
    between runs, so anchor on a stable label cell in the same table row instead.
    """
    candidates: list[Locator] = []
    if value_independent and element.row_cells is not None and element.cell_index is not None:
        anchor = _anchor_cell(element.row_cells, element.cell_index)
        if anchor is not None:
            candidates.append(
                Locator(
                    by="role",
                    role="cell",
                    nth=element.cell_index,
                    within=Locator(by="role", role="cell", name=anchor, exact=True, ancestor_role="row"),
                )
            )
    elif element.role and element.name:
        candidates.append(Locator(by="role", role=element.role, name=element.name, exact=True))
        if element.role in ("link", "button"):
            candidates.append(Locator(by="text", text=element.name, exact=True))
    elif element.role and not element.name and element.text and not value_independent:
        candidates.append(Locator(by="text", text=element.text, exact=True))
    if element.css:
        candidates.append(Locator(by="css", selector=element.css))
    if element.bbox is not None:
        candidates.append(Locator(by="bbox", bbox=element.bbox))
    if not candidates:
        raise ValueError(f"cannot derive any locator for {description!r}")
    return TargetDescriptor(description=description, candidates=candidates, recorded=element)


def _anchor_cell(row_cells: list[str], cell_index: int) -> str | None:
    """The first label-like cell in the row: non-empty and not a number, amount, or date."""
    for i, text in enumerate(row_cells):
        t = text.strip()
        if i == cell_index or not t:
            continue
        if _MONEY.match(t) or _NUMBER.match(t) or _DATE.match(t):
            continue
        return t
    return None


# ----------------------------------------------------------- parameterising
def parameterise(value: str, inputs: dict[str, str], sensitive: set[str]) -> str:
    """Replace literal input values with {{input:NAME}} references (longest values first)."""
    for name, literal in sorted(inputs.items(), key=lambda kv: len(kv[1]), reverse=True):
        if name in sensitive or len(literal) < 2:
            continue
        value = value.replace(literal, f"{{{{input:{name}}}}}")
    return value


def template_url(url: str, base_url: str, inputs: dict[str, str], sensitive: set[str]) -> str:
    """http://host/members/10001 -> {{base_url}}/members/{{input:member_id}}"""
    base = base_url.rstrip("/")
    if url.startswith(base):
        url = BASE_URL_REF + url[len(base) :]
    return parameterise(url, inputs, sensitive)


def template_path(url: str, inputs: dict[str, str], sensitive: set[str]) -> str:
    return parameterise(urlparse(url).path or "/", inputs, sensitive)


def _title_marker(title: str) -> str | None:
    marker = title.split(" - ")[0].split(" | ")[0].strip()
    return marker or None


def infer_output_type(value: str) -> OutputType:
    v = value.strip()
    if _MONEY.match(v):
        return "money"
    if _NUMBER.match(v):
        return "integer" if re.fullmatch(r"-?\d+", v.replace(",", "")) else "number"
    return "string"


# ------------------------------------------------------------------- phases
def split_phases(steps: list[TraceStep]) -> int:
    """
    Number of leading trace steps that form the bootstrap (sign-in) phase.

    Heuristic: if a secret was typed, bootstrap runs up to and including the
    first click/press after it that changes the URL path. Otherwise there is
    no bootstrap phase.
    """
    first_secret = next(
        (i for i, s in enumerate(steps) if s.tool == "type" and secret_names_in(str(s.arguments.get("text", "")))),
        None,
    )
    if first_secret is None:
        return 0
    for j in range(first_secret, len(steps)):
        s = steps[j]
        if s.tool in ("click", "press") and urlparse(s.url_after).path != urlparse(s.observation.url).path:
            return j + 1
    return 0


# ------------------------------------------------------------------ builder
def build_artifact(
    trace: Trace,
    *,
    base_url: str,
    app: str,
    app_version: str | None = None,
    conditions: DeclaredConditions | None = None,
    version: str = "1.0.0",
    viewport: dict[str, int] | None = None,
    evidence_dir: str | None = None,
) -> CapabilityArtifact:
    if trace.status != "success":
        raise ValueError(f"only a successful run can become a capability (status={trace.status})")
    inputs = {n: v for n, v in trace.inputs.items()}  # sensitive ones are already {{input:NAME}}
    sensitive = set(trace.sensitive_inputs)
    literal_inputs = {n: v for n, v in inputs.items() if n not in sensitive}

    ok_steps = [s for s in trace.steps if s.result.status == "ok"]
    n_bootstrap = split_phases(ok_steps)

    steps: list[Step] = []
    counter = 0

    def next_id() -> str:
        nonlocal counter
        counter += 1
        return f"s{counter}"

    def observation_after(i: int) -> tuple[str, str]:
        nxt = next((s for s in trace.steps if s.index > ok_steps[i].index), None)
        if nxt is not None:
            return nxt.observation.url, nxt.observation.title
        return trace.final_url, trace.final_title

    def checkpoint_for(i: int) -> Checkpoint | None:
        before = urlparse(ok_steps[i].observation.url).path
        after_url, after_title = observation_after(i)
        if urlparse(after_url).path == before:
            return None
        return Checkpoint(
            description=f"Reached {(_title_marker(after_title) or urlparse(after_url).path)!r}",
            url_pattern=template_path(after_url, literal_inputs, sensitive),
            title_contains=_title_marker(after_title),
        )

    # Explicit entry navigation for each phase, so a phase can be (re)started deterministically.
    steps.append(
        Step(
            id=next_id(),
            phase="bootstrap" if n_bootstrap else "main",
            action="navigate",
            description="Open the application",
            url=template_url(trace.start_url, base_url, literal_inputs, sensitive),
        )
    )
    extracted_by: dict[str, str] = {}
    for i, ts in enumerate(ok_steps):
        phase = "bootstrap" if i < n_bootstrap else "main"
        if i == n_bootstrap and n_bootstrap:
            steps.append(
                Step(
                    id=next_id(),
                    phase="main",
                    action="navigate",
                    description="Go to the flow's entry page",
                    url=template_url(ts.observation.url, base_url, literal_inputs, sensitive),
                )
            )
        step = _step_from_trace(ts, next_id(), phase, literal_inputs, sensitive, base_url, set(trace.sensitive_outputs))
        step.checkpoint = checkpoint_for(i)
        if step.action == "extract" and step.output:
            extracted_by[step.output] = step.id
        steps.append(step)

    declared = conditions or DeclaredConditions()
    outputs = [
        OutputField(
            name=name,
            type=infer_output_type(value),
            sensitive=name in trace.sensitive_outputs,
            extracted_by=extracted_by.get(name),
        )
        for name, value in trace.outputs.items()
    ]
    for outcome in declared.outcomes:
        for name in outcome.sets:
            if all(o.name != name for o in outputs):
                outputs.append(OutputField(name=name, type="string", description=f"Set by outcome {outcome.code}"))

    success = SuccessCondition(
        description="Final state observed at the end of discovery",
        url_pattern=template_path(trace.final_url, literal_inputs, sensitive),
        title_contains=_title_marker(trace.final_title),
        outputs_required=list(trace.outputs),
    )

    models = sorted({f"{s.model.provider}/{s.model.model}" for s in trace.steps})
    return CapabilityArtifact(
        id=f"cap_{trace.capability_name}",
        name=trace.capability_name,
        version=version,
        description=trace.goal,
        target=TargetSurface(app=app, app_version=app_version, base_url=base_url, viewport=viewport or {"width": 1280, "height": 900}),
        inputs=[
            InputParam(name=n, sensitive=n in sensitive, example=None if n in sensitive else v)
            for n, v in inputs.items()
        ],
        outputs=outputs,
        steps=steps,
        success=success,
        outcomes=declared.outcomes,
        recoverables=declared.recoverables,
        failures=declared.failures,
        policy=_declared_policy(trace, steps, base_url),
        provenance=Provenance(
            discovery_run_id=trace.run_id,
            goal=trace.goal,
            models=models,
            created_at=datetime.now(UTC).isoformat(),
            evidence_dir=evidence_dir,
        ),
    )


def _scrub_recorded(element: RecordedElement, placeholder: str) -> RecordedElement:
    """An extracted value's own text is the sensitive datum; keep the structure, drop the value."""
    row_cells = list(element.row_cells) if element.row_cells is not None else None
    if row_cells is not None and element.cell_index is not None and 0 <= element.cell_index < len(row_cells):
        row_cells[element.cell_index] = placeholder
    return element.model_copy(update={"name": placeholder, "text": placeholder, "row_cells": row_cells})


def _step_from_trace(
    ts: TraceStep,
    step_id: str,
    phase: str,
    inputs: dict[str, str],
    sensitive: set[str],
    base_url: str,
    sensitive_outputs: set[str],
) -> Step:
    action: ActionType = ts.tool  # type: ignore[assignment]
    description = ts.reason.strip() or f"{ts.tool} {ts.intent.control_name if ts.intent and ts.intent.control_name else ''}".strip()
    risk = ts.decision.risk if ts.decision else "safe"
    control = ts.intent.control_name if ts.intent else None
    if action == "navigate":
        return Step(
            id=step_id,
            phase=phase,  # type: ignore[arg-type]
            action=action,
            description=description,
            url=template_url(str(ts.arguments.get("url", "")), base_url, inputs, sensitive),
            risk=risk,
        )
    if action == "press":
        return Step(id=step_id, phase=phase, action=action, description=description, key=str(ts.arguments.get("key", "")), risk=risk)  # type: ignore[arg-type]
    if ts.element is None and ts.intent is not None and ts.intent.control_role:
        # Performed by a human during a handoff: only what the observation told us survives.
        ts.element = RecordedElement(role=ts.intent.control_role, name=ts.intent.control_name)
    if ts.element is None:
        raise ValueError(f"trace step {ts.index} ({ts.tool}) has no recorded element")
    if action == "extract":
        name = str(ts.arguments.get("name", ""))
        element = _scrub_recorded(ts.element, f"[REDACTED:{name}]") if name in sensitive_outputs else ts.element
        target = derive_target(element, description=f"Value of {name}", value_independent=True)
        return Step(id=step_id, phase=phase, action=action, description=description, target=target, output=name, risk=risk)  # type: ignore[arg-type]
    target = derive_target(ts.element, description=control or description)
    value: str | None = None
    if action == "type":
        value = parameterise(str(ts.arguments.get("text", "")), inputs, sensitive)
    elif action == "select":
        value = parameterise(str(ts.arguments.get("option", "")), inputs, sensitive)
    return Step(id=step_id, phase=phase, action=action, description=description, target=target, value=value, risk=risk)  # type: ignore[arg-type]


def _declared_policy(trace: Trace, steps: list[Step], base_url: str) -> Policy:
    """Least privilege, derived from what the run actually used."""
    paths: list[str] = ["/"]
    for ts in trace.steps:
        for url in (ts.observation.url, ts.url_after):
            p = urlparse(url).path or "/"
            pattern = re.sub(r"/\d+(?=/|$)", "/*", p)
            if pattern not in paths:
                paths.append(pattern)
    secrets: list[str] = []
    for s in steps:
        for name in secret_names_in(s.value or ""):
            if name not in secrets:
                secrets.append(name)
    actions = list(dict.fromkeys(s.action for s in steps))
    return Policy(
        name=f"cap_{trace.capability_name}",
        allowlist=AllowlistPolicy(origins=[base_url], path_patterns=paths, action_types=actions),
        risk=RiskPolicy(mode="confirm"),
        secrets=SecretsPolicy(allowed=secrets),
    )
