# Architecture

The system is one Python process with six modules behind two entry points, discovery and replay, that share everything except the decision-maker.

```
                 ┌──────────────┐      proposes action       ┌────────────┐
  goal ─────────▶│  Discovery   │───────────────────────────▶│            │
                 │  (LLM loop)  │◀── refusal / result ───────│ PolicyGate │──▶ Surface ──▶ live app
                 └──────┬───────┘                            │            │      ▲
                        │ trace                              └────────────┘      │ same window
                        ▼                                           ▲            │
                 ┌──────────────┐    artifact (JSON)   ┌────────────┴─┐          │
                 │   Builder    │─────────────────────▶│ Replay engine│──▶ escalation ──▶ human
                 └──────────────┘                      │  (no model)  │
                                                       └──────────────┘
```

**Discovery is model-driven; replay is not.** The discovery loop sends the model a stateless prompt each step: the goal, a compact action history, and the current page as an accessibility snapshot in which every node carries an ephemeral ref (plus a screenshot for vision-capable providers). The model answers with one tool call naming a ref. Replay never constructs a prompt, imports a provider, or sees a ref; it interprets the artifact.

**Perception is accessibility-tree first.** The brief says the common case has no clean DOM. Roles and accessible names are what a screen reader sees, they survive restyling and most markup rewrites, and desktop accessibility APIs expose the same vocabulary. The screenshot is supplementary grounding for the model and evidence for humans; the loop works without it, which is what made a text-only Groq fallback viable.

**One policy choke point.** Both loops build an `ActionIntent` and ask the same `PolicyGate` before touching the surface, and ask it again after every action because a click can navigate just like a navigate can. Safety therefore does not depend on the prompt.

**Boundaries chosen for extension, not scale.** `Surface` is the seam to other surfaces; the artifact is the seam to other tenants; `EscalationHandler` is the seam to a real operator console; `LLMProvider` is the seam to other models. Storage is files, execution is synchronous, and there are no queues, because the brief is explicit that infrastructure is not rewarded and none of the abstractions above would change if it were added.

Trade-offs: Python + Playwright over a CUA SDK keeps every decision inspectable and the replay path free of any model dependency, at the cost of writing the loop myself. Stateless per-step prompts cost a few more tokens than a multi-turn conversation but avoid provider-specific tool-result formats, keep each step reproducible from the transcript alone, and bound context growth.

# Artifact schema

An artifact is a capability contract with a recorded implementation, read in the order a reviewer thinks: identity and version → typed inputs and outputs → steps and success condition → declared non-happy-path conditions → policy → provenance. Condensed from `artifacts/lookup_member_balance/v1.0.0.json`:

```jsonc
{ "id": "cap_lookup_member_balance", "name": "lookup_member_balance", "version": "1.0.0", "status": "draft",
  "description": "Look up member {{input:member_id}} and read their current savings balance",
  "target": { "kind": "web", "app": "harborview-member-console", "app_version": "v4.2", "base_url": "http://127.0.0.1:4000" },
  "inputs":  [ { "name": "member_id", "type": "string", "required": true, "sensitive": false, "example": "10001" } ],
  "outputs": [ { "name": "savings_balance", "type": "money", "sensitive": true, "extracted_by": "s8" },
               { "name": "found", "type": "string", "description": "Set by outcome MEMBER_NOT_FOUND" } ],
  "steps": [
    { "id": "s1", "phase": "bootstrap", "action": "navigate", "url": "{{base_url}}/login" },
    { "id": "s2", "phase": "bootstrap", "action": "type", "value": "{{secret:TARGET_USERNAME}}",
      "target": { "description": "Username", "candidates": [
        { "by": "role", "role": "textbox", "name": "Username", "exact": true },
        { "by": "css", "selector": "body > table:nth-of-type(2) > ... > input[name=\"username\"]" },
        { "by": "bbox", "bbox": { "x": 293, "y": 213, "width": 190, "height": 21 } } ],
        "recorded": { "role": "textbox", "name": "Username", "tag": "input", "css": "…" } } },
    { "id": "s4", "phase": "bootstrap", "action": "click", "target": { "…": "button Sign In" },
      "checkpoint": { "url_pattern": "/members/search", "title_contains": "Member Lookup" } },
    { "id": "s5", "phase": "main", "action": "navigate", "url": "{{base_url}}/members/search" },
    { "id": "s6", "phase": "main", "action": "type", "value": "{{input:member_id}}", "target": { "…": "textbox Member ID" } },
    { "id": "s7", "phase": "main", "action": "click", "target": { "…": "button Search" },
      "checkpoint": { "url_pattern": "/members/{{input:member_id}}", "title_contains": "Member Detail" } },
    { "id": "s8", "phase": "main", "action": "extract", "output": "savings_balance",
      "target": { "candidates": [ { "by": "role", "role": "cell", "nth": 3,
        "within": { "by": "role", "role": "cell", "name": "Savings", "exact": true, "ancestor_role": "row" } } ] } } ],
  "success": { "url_pattern": "/members/{{input:member_id}}", "title_contains": "Member Detail", "outputs_required": ["savings_balance"] },
  "outcomes":     [ { "code": "MEMBER_NOT_FOUND", "detect": { "any_of": [ { "by": "text", "text": "No member found for ID" } ] }, "sets": { "found": "false" }, "terminal": true } ],
  "recoverables": [ { "code": "SESSION_EXPIRED", "detect": { "…" }, "recovery": { "kind": "run_phase", "phase": "bootstrap", "then": "restart_phase" }, "max_attempts": 1 } ],
  "failures":     [ { "code": "PERMISSION_DENIED", "category": "authorization", "detect": { "…" }, "retryable": false } ],
  "policy": { "allowlist": { "origins": ["http://127.0.0.1:4000"], "path_patterns": ["/", "/login", "/members/search", "/members/*"],
              "action_types": ["navigate", "type", "click", "extract"] }, "risk": { "mode": "confirm" }, "secrets": { "allowed": ["TARGET_USERNAME", "TARGET_PASSWORD"] } },
  "provenance": { "discovery_run_id": "discovery-…", "models": ["gemini/gemini-2.5-flash"], "evidence_dir": "evidence/discovery/…", "reviewed_by": null } }
```

Why it is shaped this way:

- **Targets are ranked fallback chains, not selectors.** The builder, not the model, decides how to find a control again: `role+name` (what a screen reader sees) → visible text → a CSS path → a bounding box, each less robust than the last. Extracted values get a *value-independent* locator: "the 4th cell of the row whose first label cell is exactly `Savings`", because the cell's own text is the thing that changes between runs. `recorded` keeps what the control looked like for reviewers and drift diagnosis; it is never used for resolution.
- **Phases make recovery deterministic.** Each phase opens with an explicit `navigate`, so "sign in again and restart the interrupted phase" is a well-defined operation rather than "somehow get back".
- **Checkpoints are derived from what was observed**, not assumed: the URL path (templated) and title marker after every page-changing step, plus the final state and required outputs as the success condition.
- **Conditions live in the artifact so a reviewer can read them without code.** Outcomes, recoverables and failures are a reviewed, per-application list (`policies/mock-portal.conditions.json`) merged in by the builder. Adding "no such member" handling is a JSON edit, not a code change.
- **Least-privilege policy is derived from the run**: only the origins, path patterns, action types and secrets the recording actually used. At replay it is intersected with the environment policy, so it can only narrow.
- **Decoupled from the transcript.** Refs, prompts, model reasoning, token counts and screenshots stay in `evidence/discovery/<run>/trace.json`; the artifact keeps only `provenance` pointers to them. Step `description`s are the model's one-line reasons, kept because they are useful to a reviewer, not because replay needs them.
- **Versioning**: `schema_version` for the format, semver per capability (a re-recording bumps the minor version, the store never overwrites), and `status: draft | approved | deprecated` so unattended replay can be gated on review.

# Determinism & error handling

**Determinism.** Replay has no model, no randomness and no timing-dependent branching. Each step renders `{{input}}`, `{{secret}}` and `{{base_url}}` templates, is policy-checked, resolves its target by polling the whole candidate chain until exactly one visible element matches (an ambiguous candidate is skipped, not guessed), acts, is checked against the allowlist again, has the declared conditions evaluated, and then has its checkpoint verified within a timeout. Waiting is state-based (a checkpoint or a locator becoming satisfiable), never a fixed sleep, so a slow load is absorbed rather than raced. The candidate index that resolved each target is recorded; anything above zero is reported as **drift** on an otherwise successful run, which is the signal to re-record before the last fallback stops working too.

**Result contract.** Three statuses that the caller must treat differently:

| Status | Meaning | Example | What the caller gets |
|---|---|---|---|
| `success` | success condition verified, required outputs present | balance read | typed outputs (`money` → `4210.55`) |
| `business_outcome` | the application gave a legitimate non-happy answer | `MEMBER_NOT_FOUND`, `INVALID_MEMBER_ID`, `VALIDATION_ERROR` | outcome code + outcome-declared outputs (`found: "false"`) |
| `failure` | the flow could not complete | `PERMISSION_DENIED`, `TARGET_NOT_FOUND`, `CHECKPOINT_FAILED`, `INPUT_INVALID`, `CONFIRMATION_REQUIRED` | code, category, step id, expected, observed, retryable, screenshot, locator attempts |

**Runtime conditions** are detected after every step and, importantly, *when a target cannot be found or a checkpoint fails*, because "the button is missing" is usually the app saying something. Order matters: recoverables first (an interstitial masks whatever is under it), then business outcomes, then failures. Recoveries are bounded: each pattern has `max_attempts`, a phase may restart at most three times, and a retryable failure pattern (an app error page) gets exactly one restart before it becomes a hard failure. Everything the mock app can produce is exercised in `tests/test_replay.py`: not found, invalid id, validation error, permission denied, session expiry (re-run bootstrap), a known interstitial (click through), slow load (absorbed), transient vs persistent app error, a broken primary locator (fallback + drift), a wrong checkpoint (expected vs observed), a missing input (fails before the browser opens), and a deprecated artifact.

**UI drift** is secondary, as the brief says, but the design handles it in three layers: locator fallbacks with the drift report, `TargetNotFound` failures that list every attempt, and re-recording into a new minor version with the old one deprecated.

# Heterogeneity & multi-tenant

**Surface abstraction.** `Surface` is the seam: `observe()` returns a role/name/value tree plus a screenshot; `resolve(TargetDescriptor)` turns a surface-agnostic descriptor into a handle; `click/type/select/press/read` act on handles. The artifact never mentions Playwright, CSS, or the DOM as such; `css` and `xpath` are just two candidate strategies a web adapter knows how to build. A **legacy web** adapter is the same class with frame-aware resolution (the descriptor would gain an optional `frame` scope) and heavier reliance on `text` and `within`/`ancestor_role` for table-based layouts, which the current adapter already handles. A **desktop** adapter would implement `observe()` over UI Automation or the macOS accessibility API, which expose the same role/name vocabulary, and would resolve `bbox` candidates against a screen capture; the replay engine, the result contract, the policy gate and the escalation protocol do not change. `TargetSurface.kind` selects the adapter.

**Multi-tenant reuse.** Tenants running the same vendor product share the artifact; what differs is bound at invocation. `target.base_url` is a placeholder resolved per tenant (`--base-url`), `app_version` names the vendor version the recording was made against, and `overrides[]` carry per-tenant, per-step specialisations (a different target, value or URL) applied only for `--tenant X`, so a branded label change is a two-line override rather than a re-recording. The intended structure is three layers: a **base capability** per vendor product and version, a **tenant overlay** (base URL, secrets, overrides, tenant-specific outcomes), and a **version overlay** when a vendor release moves a control. Drift is detected per tenant from the same signal replay already emits: a rising share of fallback resolutions, or `TARGET_NOT_FOUND`, on one tenant but not others points at tenant configuration; on all tenants of one `app_version` it points at a vendor release and triggers re-recording of the base. Multi-tenant *infrastructure* (a registry, per-tenant secrets vaults, drift dashboards) is deliberately not built; the artifact fields and replay options that make it possible are.

# Escalation & handoff

**Detecting "stuck".** Replay escalates on any failure a person could plausibly fix: a checkpoint not reached, a control not found, a declared hard failure, a recovery that ran out of budget, a risky step awaiting confirmation. It does not escalate on caller errors (bad inputs, a deprecated artifact) since a human at the browser cannot fix those. Discovery escalates when the model calls `stuck`, and routes risky actions to a human before performing them.

**Transfer of control.** A `SessionControl` ledger is the single source of truth for who holds the session. On escalation the engine writes `intervention.json` (capability, step, why, URL, title, screenshot, expected/observed), the ledger records `paused` then `ceded`, and from that instant the surface refuses every automation action (`ControlViolation`); the two parties cannot act concurrently by construction. The human works in the **same headed browser window** automation was driving: same cookies, same page, same session. A red banner injected into every document says who is in control. A DOM-level recorder captures what the human does: which control (role and name), what kind of action (click, change with value length, select with option label, key, navigation), never the values typed. The operator hands back with `cua-resume --resolution retry_step | skip_step | restart_phase | abort`, written atomically as `resume.json`; the ledger records `resumed`, the human's actions become `human_action` events in the result, and the engine continues accordingly. No response within the timeout resolves as `abort`, which surfaces the original error together with the control history.

**What is mocked.** The operator console is the browser window, the printed instructions and one CLI. It is a bare surface on purpose: the mechanism it drives (pause, lock, record, resume on the same session) is the real one, and a remote console would sit behind the same `EscalationHandler` interface, streaming the page over CDP and relaying input through it, without touching the engine.

# Safety

**Model of enforcement.** The `PolicyGate` sits between every decision-maker and the surface. Discovery: a refused proposal is reported back to the model as the result of its action, so it can find another way or declare `stuck`, and an action that ends up outside the allowlist is contained by navigating back. Replay: a refused step is a `policy` failure. The gate checks (1) the action type against the allowlist, (2) the destination origin and path against origin/glob rules with explicit denies winning (the app's `/__admin` fixture is denied), (3) secret references against the allowed list and their absence from URLs, and (4) risk. Risk is classified from control-name patterns (Confirm, Approve, Delete, Transfer, …), URL patterns (`/**/confirm`), and the step's recorded `risk`; a proposer can raise an action's risk, never lower it. Mode `confirm` (the default) sends risky actions to a human; `block` refuses; `allow` is for pre-approved capabilities and is logged. `--allow-risky` is a per-invocation, logged pre-approval by the caller. The capability's declared policy is intersected with the environment's at replay, so a recorded artifact can only narrow what an environment permits.

**Data handling.** Credentials are `{{secret:NAME}}` references everywhere except the instant they are typed; the model never receives them and cannot exfiltrate them via a URL. Inputs marked sensitive are shown to the model as `{{input:NAME}}` and substituted at act time. Outputs marked sensitive (by the model, or by name heuristics such as *balance*) are added to the redactor the moment they are read, before the step's transcript entry is written. The redactor masks secret values, sensitive values and structured PII (SSN, card, e-mail, phone patterns) in every event, transcript, artifact and result file, and the artifact builder additionally scrubs a sensitive extracted value out of the recorded element metadata. Replay screenshots are on-failure only by default.

**Limits.** The allowlist is by URL and action type, not by *semantics*: it cannot tell a benign "Search" click from one that a malicious page has relabelled. Risk classification is heuristic and only ever errs towards confirmation. PII regexes catch structured identifiers, not free-text names; sensitivity of extracted values relies on declaration. Prompt injection from page content could steer the *model* during discovery, which is why discovery is treated as an untrusted proposal stream and the gate, not the prompt, is the boundary; replay is not exposed to it at all.

# Cuts

Deliberately left out, with what I would build next:

- **A second surface adapter.** The seam is real and tested through one implementation. Next: a frame-aware legacy-web adapter against a frameset variant of the mock app, then a desktop adapter over the macOS accessibility API for a small native app.
- **Tenant registry and drift dashboard.** Artifacts carry `base_url`, `app_version` and `overrides`, and replay emits the drift signal; the storage, aggregation and alerting around it are not built.
- **Remote operator console.** The handoff protocol is complete on a headed browser; a CDP screencast + input relay behind the same `EscalationHandler` would make it remote.
- **Assisted fallback.** On a `TARGET_NOT_FOUND`, a single bounded, policy-gated model call to re-locate one control would be a natural, evidence-recorded recovery; it would live behind the same gate and be marked in the result so the run is never mistaken for a fully deterministic one.
- **Confidence scoring and multi-run stability.** The approval gate exists; scoring artifacts by replay history (success rate, fallback rate) to drive it does not.
- **Canonicalisation beyond inputs.** Literal input values become `{{input}}` references in values, URLs and checkpoints; more general route canonicalisation (`/item/12345` → `/item/:id` for non-input values) is not done.
- **Depth on the mock app.** It has one irreversible action and seven runtime conditions, which is enough to exercise every branch of the result contract; iframes and framesets were cut in favour of finishing the handoff.
