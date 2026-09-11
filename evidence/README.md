# Recorded runs

Every run below was produced on 2026-09-11 (UTC) against the local mock app, with the real
Gemini provider (`gemini-3.6-flash`) driving both discovery runs. No fallback to Groq fired.
Everything here passed the redactor on the way to disk: credentials appear only as
`{{secret:NAME}}` references and extracted balances only as `[REDACTED:savings_balance]`.

## discovery/

| Run | Capability | What happened |
|---|---|---|
| `discovery-20260911T023106Z-8ace` | `lookup_member_balance` | 7 model steps compiled into the 8-step artifact `artifacts/lookup_member_balance/v1.0.0.json`. `transcript.jsonl` has the per-step model request and response; `screenshots/` has one image per step. |
| `discovery-20260911T033357Z-8e8e` | `open_sub_account_review` | 11 model steps: search, open member, open the sub-account form, select, type, type, reach the review screen. The model never attempted the irreversible confirm; the artifact's allowlist does not include it. |

## replay-success/

| Run | Input | What happened |
|---|---|---|
| `replay-20260911T025249Z-becf` | `member_id=10003` | Plain deterministic replay: 8 of 8 steps ok, no drift, balance redacted in `result.json`. |
| `replay-20260911T025609Z-b1c0` | `member_id=10001`, `session_timeout` injected | Session expiry surfaced at the s4 checkpoint; the engine re-ran the bootstrap phase and completed (12 steps). `recoveries` shows `SESSION_EXPIRED@s4`. |
| `replay-20260911T033455Z-b7b8` | `member_id=10003 account_type=Savings nickname=Travel initial_deposit=40` | The second capability replayed with different inputs than it was recorded with; ends on the review page without confirming. |
| `invoke-20260911T033221Z-850b` | `member_id=10001` via `cua-catalog invoke` | Agent-facing invocation of the approved capability. An earlier invoke of the same draft artifact was refused (exit 4) before `cua-catalog approve`. |

## replay-error/

| Run | Input | What happened |
|---|---|---|
| `replay-20260911T025251Z-13a0` | `member_id=99999` | `business_outcome` / `MEMBER_NOT_FOUND`, exit 3, outputs `{"found": "false"}`, `screenshots/outcome-MEMBER_NOT_FOUND.png`. |
| `replay-20260911T025251Z-ccad` | `member_id=40403` | `failure` / `PERMISSION_DENIED [authorization]` at s7, exit 1, expected vs observed in `result.json`, `screenshots/failure-s7.png` shows the Access Denied page. |

## escalation/

Both runs inject an undeclared "password expires in 3 days" interstitial, so replay cannot classify
it and hands the live browser to a person. `intervention.json` is the request, `resume.json` the
operator's answer, and `result.json` carries the `control_events` ledger.

| Run | Operator | What happened |
|---|---|---|
| `escalation-20260911T030649Z-21f0` | scripted (`--simulate-operator`) | Paused at s4 on the Security Notice page, the script clicked "Remind Me Later" on the same page, wrote `skip_step`, replay resumed and succeeded. |
| `escalation-20260911T031847Z-b328` | a person (`ishan`) | Same condition; the operator dismissed the dialog in the headed window and handed control back with `cua-resume --resolution skip_step`. The ledger records the click, the form submit, the navigation, and the resume. |
