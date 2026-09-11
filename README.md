# Rote

*Learn it once, do it by rote.* A computer-use automation system for legacy back-office applications.

An LLM figures out how to accomplish a task in a legacy back-office web app **once**. The successful run is compiled into a typed, versioned **capability artifact**. From then on, AI agents invoke that capability through a **deterministic replay engine** that never consults a model, classifies what the application says into *business outcomes*, *recoverable conditions*, and *hard failures*, and hands the live browser to a **human operator** when it cannot safely proceed.

Built for the interface.ai take-home. The design write-up is in [REPORT.md](REPORT.md); recorded runs are in [evidence/](evidence/). The Python package and CLI prefix are `cua` (computer-use automation).

```
goal ──▶ DISCOVERY (LLM: observe → decide → act) ──▶ trace ──▶ BUILDER ──▶ capability artifact (JSON)
                    │                                                              │
                    │   PolicyGate (allowlist, risk, secrets)  ◀───────────────────┤
                    ▼                                                              ▼
              Surface (Playwright, accessibility-tree first)  ◀──── REPLAY (no LLM) ──▶ success | business_outcome | failure
                    │                                                              │
                    └──── human takes over the SAME browser window ◀── escalation ─┘
```

## Contents

- [What is in the box](#what-is-in-the-box)
- [Setup](#setup)
- [Configuration](#configuration)
- [Demo path](#demo-path)
- [Running without live services](#running-without-live-services)
- [Human handoff](#human-handoff)
- [Agent-facing catalog](#agent-facing-catalog)
- [Evidence](#evidence)
- [Tests](#tests)
- [Safety notes](#safety-notes)
- [What is mocked, and limitations](#what-is-mocked-and-limitations)
- [Repository layout](#repository-layout)

## What is in the box

| Piece | Where | What it does |
|---|---|---|
| Target app | `src/cua/target_app/` | A local mock "Member Services Console" for a credit union: server-rendered, table-based, no test IDs. Sign in → member lookup → detail (balances) → open sub-account → review → irreversible confirm. Produces not-found, validation, permission-denied, session-timeout, interstitial, slow-load, and app-error conditions on demand. |
| Surface | `src/cua/surface/` | The seam between "perceive/act on an application" and everything else. `Surface` is the contract; `PlaywrightSurface` is the one implementation. Perception is accessibility-tree first (roles, names, values) plus a screenshot. |
| Discovery | `src/cua/agent/` | The observe → decide → act loop. Gemini first, Groq on rate-limit, a scripted provider for offline runs. Every proposed action goes through the policy gate before it touches the surface. |
| Artifact | `src/cua/artifact/` | The capability schema (Pydantic), the trace → artifact builder that derives robust locator chains and parameterises the flow, and the versioned store. |
| Replay | `src/cua/replay/` | Deterministic execution of an artifact with typed inputs and outputs, checkpoints, declared-condition detection, bounded recovery, and a three-way result contract. |
| Policy | `src/cua/policy/` | Allowlist (origins, path globs, action types), risk classification, `{{secret:NAME}}` references, redaction of everything persisted. |
| Escalation | `src/cua/escalation/` | Session-control ledger, headed-browser handoff protocol, `cua-resume`. |
| Evidence | `src/cua/evidence/` | Per-run directory with a structured event log, model transcript, screenshots, and result. |
| Catalog | `src/cua/catalog.py` | Stretch goal: artifacts as function-calling tool definitions with an approval gate. |

## Setup

Requires Python 3.12+ and a machine that can run Chromium.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
cp .env.example .env          # then fill in keys (only needed for a real LLM discovery run)
```

## Configuration

Everything is read from the environment (a `.env` file is loaded automatically). See [.env.example](.env.example).

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Primary discovery model (default `gemini-2.5-flash`; needs vision + function calling). |
| `GROQ_API_KEY`, `GROQ_MODEL`, `GROQ_VISION` | Fallback used automatically when Gemini returns 429/503. Set `GROQ_VISION=1` only for a vision-capable Groq model; otherwise the model reasons over the accessibility tree alone. |
| `TARGET_APP_PORT`, `TARGET_APP_URL` | Where the mock app listens / where the automation points. |
| `TARGET_USERNAME`, `TARGET_PASSWORD` | Demo-only fixture credentials for the mock app (`teller1` / `demo-pass-2024`). The model, the artifact and the logs only ever see `{{secret:TARGET_USERNAME}}` / `{{secret:TARGET_PASSWORD}}`. |

Policy for the mock environment lives in [policies/mock-portal.toml](policies/mock-portal.toml) (allowlist, risk mode, allowed secrets) and [policies/mock-portal.conditions.json](policies/mock-portal.conditions.json) (the reviewed list of business outcomes, recoverable patterns and failure patterns that get merged into every artifact recorded against this app).

## Demo path

Terminal 1, the target application:

```bash
cua-target-app                      # http://127.0.0.1:4000
```

Terminal 2, the full thread. Every command prints where its evidence went.

```bash
# 1. Discovery: a real LLM drives the app to the goal, then the run is compiled into an artifact.
cua-discover --goal "Look up member 10001 and read their current savings balance" \
             --name lookup_member_balance --input member_id=10001
#    -> artifacts/lookup_member_balance/v1.0.0.json   evidence/discovery/<run>/

# 2. Deterministic replay with a different input (no LLM involved). Exit 0.
cua-replay --artifact artifacts/lookup_member_balance/v1.0.0.json --input member_id=10003

# 3. A legitimate business outcome, not an error. Exit 3.
cua-replay --artifact artifacts/lookup_member_balance/v1.0.0.json --input member_id=99999
#    status: business_outcome   outcome: MEMBER_NOT_FOUND   outputs: {"found": "false"}

# 4. A hard failure with a precise, debuggable error and a screenshot. Exit 1.
cua-replay --artifact artifacts/lookup_member_balance/v1.0.0.json --input member_id=40403
#    error: PERMISSION_DENIED [authorization] at s7 ... expected / observed / screenshot

# 5. A runtime condition replay recovers from by itself (session expiry, injected via the app's test fixture).
curl -s -X POST -H 'Content-Type: application/json' -d '{"kind":"session_timeout"}' http://127.0.0.1:4000/__admin/inject
cua-replay --artifact artifacts/lookup_member_balance/v1.0.0.json --input member_id=10001
#    recoveries: SESSION_EXPIRED@s4 (... restart phase)   status: success

# 6. Escalation: an undeclared dialog appears, replay pauses, a human fixes it in the same window, replay resumes.
python demo/escalation_demo.py --artifact artifacts/lookup_member_balance/v1.0.0.json            # you are the operator
python demo/escalation_demo.py --artifact artifacts/lookup_member_balance/v1.0.0.json --simulate-operator

# 7. Agent-facing catalog: approve the capability, then invoke it as a tool.
cua-catalog approve lookup_member_balance --by <you> --notes "reviewed locators and outcomes"
cua-catalog invoke  lookup_member_balance --arg member_id=10001
```

A second, multi-field capability (search → detail → form → review screen, stopping short of the irreversible confirm):

```bash
cua-discover --goal "Open a new Checking sub-account nicknamed Bills with a 25.00 initial deposit for member 10002 and reach the review screen without confirming" \
             --name open_sub_account_review --input member_id=10002 --input account_type=Checking --input nickname=Bills --input initial_deposit=25.00
cua-replay --artifact artifacts/open_sub_account_review/v1.0.0.json \
           --input member_id=10003 --input account_type=Savings --input nickname=Travel --input initial_deposit=40
```

Useful flags: `--headed` shows the browser; `--json` prints the full result contract; `--show-sensitive` prints redacted outputs in the clear; `--allow-risky` pre-approves a risky step for one invocation; `--require-approved` refuses draft artifacts; `--base-url` / `--tenant` bind an artifact to another environment or apply per-tenant overrides.

## Running without live services

No API keys are needed for anything except step 1 above. The discovery loop accepts a **scripted provider** that plays a fixed sequence of tool calls, resolving elements from the live accessibility snapshot exactly as a model would. It is what the test suite uses, and it produces a real artifact you can replay:

```bash
cua-discover --goal "Look up member 10001 and read their current savings balance" \
             --name lookup_member_balance --input member_id=10001 \
             --provider scripted --script demo/scripts/lookup_member_balance.json
```

The scripted run is *not* the evidence of an LLM-driven run; `evidence/discovery/` holds the real one.

## Human handoff

When replay hits something it cannot classify (a checkpoint not reached, a control not found, a declared hard failure a person might fix) and `--escalate` is set, it:

1. writes `intervention.json` into the run's evidence directory (capability, step, why it stopped, current URL and title, screenshot),
2. records that automation has **paused** and **ceded** the session; from this moment the surface refuses automation actions,
3. injects a red "HUMAN CONTROL" banner into the page and starts recording the operator's actions (which control, what kind of action; never typed values),
4. prints instructions and waits for `resume.json`.

The operator works in the **same browser window**, then hands control back:

```bash
cua-resume --run evidence/replay/<run_id> --resolution skip_step --operator alice --notes "dismissed the dialog"
#   resolutions: retry_step | skip_step | restart_phase | abort
```

Replay records the human's actions and the control transfer in the result (`control_events`) and continues. If nobody responds within `--escalation-timeout` seconds the intervention resolves as `abort` and the original error is reported. During discovery, `--escalate` routes **risky-action confirmations** through the same mechanism (`retry_step` = approve, `skip_step` = the operator did it by hand, anything else = deny).

## Agent-facing catalog

```bash
cua-catalog list                                 # every capability as a function-calling tool definition
cua-catalog describe lookup_member_balance       # + return schema
cua-catalog approve  lookup_member_balance --by alice
cua-catalog invoke   lookup_member_balance --arg member_id=10001
```

`invoke` refuses capabilities that are still `draft`, runs the replay engine, and returns `{status, outputs, outcome?, error?}`. `cua-schema` exports the artifact format as JSON Schema.

## Evidence

Each run gets its own directory. Everything written there passes through the redactor first.

```
evidence/
  discovery/<run>/      run.jsonl (events), transcript.jsonl (per-step model request/response, usage, response ids),
                        trace.json (typed record of the run), screenshots/step-NN.png
  replay-success/<run>/ result.json, run.jsonl
  replay-error/<run>/   result.json, run.jsonl, screenshots/outcome-*.png | failure-*.png
  escalation/<run>/     intervention.json, resume.json, result.json (control_events), screenshots/escalation-*.png
```

Replay defaults to screenshots **on failure only**, so routine production runs do not accumulate images of regulated data. `evidence/README.md` indexes the committed runs.

## Tests

```bash
pytest                  # ~110 tests, ~1 min; launches headless Chromium against an in-process mock app
pytest -m "not browser" # the pure unit tests only
mypy && ruff check src tests
```

Coverage is aimed where the brief says it matters: artifact schema and locator derivation, policy (allowlist, risk, secrets, redaction, intersection), replay classification of every runtime condition the mock app can produce, checkpoints and drift, the session-control state machine, and the handoff end to end.

## Safety notes

- **Nothing reaches the browser without a policy decision.** The same `PolicyGate` gates the model's proposals during discovery and the artifact's steps during replay. Prompts ask the model to behave; the gate makes it irrelevant whether it does.
- **Allowlist**: origins, path globs with explicit denies (`/__admin/**` is denied), action types. Checked before a navigation and *after every action*, because a click can navigate too. The capability's own declared policy is intersected with the environment's, so an artifact can only narrow what an environment allows.
- **Risky actions** (control names like Confirm/Approve/Delete/Transfer, URL patterns like `/**/confirm`, or a step marked `risky`) are never executed unattended: mode `confirm` routes them to a human; `--allow-risky` is an explicit, logged per-invocation pre-approval.
- **Secrets** exist only as `{{secret:NAME}}` references outside the moment they are typed. They are refused in URLs and for names the policy does not list.
- **Redaction** of secret values, declared-sensitive inputs/outputs and structured PII applies to every log line, transcript entry, artifact, and result file.

## What is mocked, and limitations

- **The target application** is a local mock. Its `/__admin` fault-injection endpoints are a test fixture: the artifact and the replay engine never see them, and the allowlist denies them.
- **The operator console** is the headed browser plus `cua-resume` and a printed banner. Control transfer, session locking, action recording, and resume are real; there is no remote co-browsing.
- **Human-action recording** captures DOM-level events (clicks, field changes with lengths, selections, key presses, navigations), not raw OS input and never typed values.
- **One surface** is implemented (Chromium via Playwright). Legacy-web and desktop adapters, and multi-tenant overlays beyond `--base-url` / `--tenant` overrides, are designed (see REPORT.md) but not built.
- **Provider fallback** was implemented and unit-tested against simulated 429s; whether it fired during the recorded discovery run is visible in `evidence/discovery/<run>/run.jsonl` (`llm.fallback` events).

## Repository layout

```
README.md  REPORT.md  pyproject.toml  .env.example
policies/           environment policy (TOML) and reviewed per-app conditions (JSON)
demo/               scripted-provider scripts, escalation demo
artifacts/          saved capability artifacts:  <name>/v<semver>.json
evidence/           recorded runs (see above)
src/cua/
  target_app/       mock credit-union console (Flask) + fault injection fixture
  surface/          Surface contract, Playwright implementation, snapshot parser
  agent/            discovery loop, prompts, tools, trace, providers/ (gemini, groq, scripted)
  artifact/         schema, targets (locators), builder, store, actions, values
  replay/           engine, result contract, input validation
  policy/           model, gate, secrets, redaction
  escalation/       session control ledger, handoff, confirmation adapter
  evidence/         run recorder
  catalog.py        agent-facing capability interface
  cli/              cua-target-app, cua-discover, cua-replay, cua-resume, cua-catalog, cua-schema
tests/
```
