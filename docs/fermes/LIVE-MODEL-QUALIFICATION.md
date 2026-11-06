# FHM-007 — Real LM Studio / ReMe qualification checkpoint

project_id: fermes-hermes
date: 2026-10-26
status: LAN RESTORED; real model completion PASS; ReMe durable-learning acceptance OPEN
issue: https://github.com/fredheathcote007/hermes-agent/issues/7
branch: fermes/hermes-provider-stage1

## Approved service restoration

- Dad-PC Bionic 1.1.8 and shared LM Studio HTTP configurations were found using `networkInterface: 127.0.0.1`, after the endpoint had previously been accessible at `10.1.1.50:1234`. This matches upstream Bionic issue lmstudio-ai/lmstudio-bug-tracker#2500.
- The owner expressly approved restoration. Both config files were backed up on Dad-PC with suffix `.pre-fermes-restore-20261026-192859.bak`. After graceful GUI close, no active API clients or model workers were found. The remaining Bionic background process was stopped, configs set to `10.1.1.50`, and Bionic relaunched.
- Read-only verification: Windows `10.1.1.50:1234` LISTENING; Project Coordinator `10.1.1.40` GET `/v1/models` HTTP 200 in 5 ms; 21 models enumerated. Qwen 3.5 4B completed a synthetic text response and a direct tool-call request with four required fields. No Windows Firewall rule was added or changed.
- Do not conflate successful network reachability with a verified secure production inference endpoint. The HTTP endpoint accepted test requests without bearer authentication; review firewall/source restrictions before broad operational use.

## First actual ReMe extraction — FAIL (baseline d19ef09)

A fully isolated Bubblewrap test ran the existing Hermes loader, embedded ReMe provider, file/BM25 store, and real model `qwen_qwen3.5-4b` over the restored LAN. Only `/tmp/fhm007/live-model-sandbox` was writable; Dream was disabled. It fed a synthetic user/assistant turn requesting retention of `FHM007-amber-kite-824`.

Qwen returned seven `daily_write` tool calls with `{}` arguments; each was rejected for missing `name, description, session_id, content`. AgentScope hit its six-iteration limit. ReMe incorrectly persisted `auto_memory` receipt `success:true, changes:[]`, cleared the Hermes journal, wrote no daily note and recalled nothing in a fresh session. Source log: `/tmp/fhm007/live-reme-capture.log`; trace/receipt under synthetic workspace. This is a **confirmed accepted-but-not-learnt retention defect**, not a local-network fault.

## Bounded fail-closed correction (draft, not approved for merge)

- `AsAgentWrapper.reply()` now returns redacted current-turn tool call names and result states from the combined AgentScope context, avoiding argument/output disclosure.
- `AutoMemoryStep` now rejects a no-note response after all attempted durable tool calls have failed. Failed responses are not cached as success receipts; Hermes keeps the same stable request-ID pending.
- Deterministic tests cover all-failed tool operations, valid/no-op paths and Hermes queue retention. **69/69 focused authorised host tests PASS** after correction and Black formatting: `/tmp/fhm007/final-livefix-host-tests.log`.
- Fresh actual Qwen 4B retest on a separate readonly-root Bubblewrap test environment: repeated invalid `daily_write` attempts were reported as failures, **one journal entry remained**, no success receipt was saved, and no daily memory was created. Evidence: `/tmp/fhm007/live-reme-capture-after-fix.log`; synthetic sandbox 2.
- This is an important failure-containment improvement, **not proof of real model learning**. The Qwen+ReMe tool-argument incompatibility persists. During the failing workload, backend shutdown also raised `RuntimeError: Event loop is closed` on asynchronous HTTP client cleanup, and post-restart retrieval timed out; these remain separate lifecycle blockers.
- Independent second Codex review concluded the correction may be retained on a **draft PR only**; P1 follow-ups include pairing tool calls/results by ID, unknown/missing result states fail closed, current-turn aggregation correctness, and mixed success/error behaviour. Do not merge or deploy on this evidence.

## Exact next actions

1. Fix or work around the complex ReMe/AgentScope tool-schema incompatibility with Qwen3.5 4B; compare an explicitly qualified local model without exposing private transcripts.
2. Require durable positive evidence for successful learning rather than treating no-op or exhausted iteration as a learnt memory; preserve correct no-op semantics and idempotent retry.
3. Add bounded retry/cancellation and graceful asynchronous HTTP cleanup tests.
4. Complete FHM-006 durable pre-compression checkpoint and workspace ownership/poisoning gates through approved architecture review.
5. Independently verify real model-generated file creation, provenance and successful fresh-session recall before considering FHM-007 acceptance.

No active Hermes installation changed; no SQLite/new vector store or Dream activation; draft PR remains unmerged.
