# Fermes ReMe fork — FHM-007 Stage 1

project_id: fermes-hermes
component_id: fermes-reme
parent_issue: https://github.com/fredheathcote007/hermes-agent/issues/7
upstream: https://github.com/agentscope-ai/ReMe
fork: https://github.com/fredheathcote007/fermes-reme
initial_upstream_sha: 363e497ae96733271414ea4224e274e5de5e5253
development_branch: fermes/hermes-provider-stage1
status: Stage 1 adapter fixes and host qualification evidence recorded; acceptance remains NO GO; NOT DEPLOYED
date: 2026-10-26

## Approved direction

Fork the *complete* ReMe repository (Apache-2.0) and qualify its existing
`integrations/hermes_agent` MemoryProvider plugin first. Retain native
file-backed memory and BM25 search as the Stage 1 baseline. Do not port to
SQLite or change/add vector stores until the fork plugin works and is
benchmarked. Preserve ReMe's existing vector capabilities; defer *new* vector
integration. Use embedded mode to avoid a mandatory daemon/listener, and
only approved local LM Studio/OpenAI-compatible model endpoints; no Hindsight,
Project Coordinator or mandatory cloud dependence.

Keep upstream provider identity `reme` until the Hermes plugin loader and
actual v0.22.5 compatibility are proven; then brand the Fermes plugin with
minimal, reviewed changes. Do not alter live Hermes profiles or settings.

## Phase 1 acceptance gates

1. Real Hermes v0.22.5 MemoryProvider contract, plugin doctor, prefetch,
   sync_turn, shutdown and profile configuration work in a disposable profile.
2. Synthetic memories survive restart; a fresh session retrieves the right
   fact with source provenance, bounded latency and no cross-profile leak.
3. No accepted writes vanish silently on crash/shutdown; bounded timeouts,
   retries, interruption recovery and error logs are demonstrated.
4. Prompt injection and memory poisoning cannot convert recalled content
   into privileged instructions or trusted skill modifications.
5. Reproduce or exclude upstream ReMe issues #660 (interrupted dream batch
   loss), #661 (dream lock hangs), #666 (corrected transcript record lost),
   #673 (zero float16 vector NaN); fix proven blockers with tests.
6. FHM-006 pre-compaction checkpoint safety is preserved, and FHM-008
   SkillHone / FHM-009 SkillOpt-Sleep can later consume evidence, candidates
   and failure/lesson records without direct privileged memory writes.
7. Independent review, focused CI and retrieval-quality comparisons pass
   before deployment, main merge or migration approvals.

## Immediate work

- Run `tests/unit/test_hermes_agent_integration.py` with dependencies in
  an isolated Python environment against the maintained Hermes provider.
- Run the Hermes plugin validator on a temporary HERMES_HOME and demonstrate
  local BM25 search and session-to-session capture with synthetic data.
- Pin fork CI to GitHub-hosted Ubuntu; keep release/publish workflows out of
  normal fork development until explicitly approved.
- Fix bounded blockers; do not rewrite the engine, add SQLite or change the
  vector storage in Stage 1.

## 2026-10-26 implementation evidence

- Added a profile-local, `fsync`ed JSONL pending-write spool at
  `$HERMES_HOME/reme/pending-writes.jsonl`. A completed Hermes turn is only
  enqueued after the spool append succeeds; backend failure, `success: false`,
  shutdown timeout, and process loss retain the record for later replay.
- The spool UUID now flows through the backend protocol and embedded/HTTP
  transports as ReMe's existing `auto_memory(request_id=...)` field. Failed
  records are retried with backoff while later accepted records continue; this
  is at-least-once transport with ReMe receipt/idempotency semantics, not a
  second idempotency engine. A profile-local OS advisory lock now enforces an
  explicit single-writer invariant; a competing same-profile provider fails
  closed for recall and capture, and the lock is automatically released when
  its process dies.
- Added `reme/config/fermes-hermes-stage1.yaml`, a dedicated embedded profile
  with local file/BM25 search, no Dream or proactive jobs, CLI/no-listener
  service configuration, and an explicit loopback model default.
- Host evidence recorded in `/tmp/fhm007`: focused Hermes adapter **38 passed**
  (`reme-fork-stage1-host-tests.log`), real Hermes Plugin Doctor **PASS**
  (`reme-plugin-doctor-host.log`), real loader smoke found
  `ReMeMemoryProvider` available (`real-hermes-loader-smoke.log`), and seeded
  model-free embedded BM25 returned the expected copper-reference evidence and
  closed cleanly (`offline-bm25-probe.log`).
- The host evidence predates this second implementation pass; rerun the
  focused suite in the disposable qualification environment before review.

## Historical work

The unwired Hermes SQLite ledger remains in draft PR #19 as a deferred option
and regression reference, not the current implementation path. Never merge
or deploy it merely because its six focused tests pass.

## Stage 1 profile directions

Use a disposable Hermes profile and an isolated ReMe workspace. Do not put
secrets in the profile JSON; the only model defaults are loopback. Replace the
absolute paths with local paths and set model name/base URL only when using a
local OpenAI-compatible server:

```json
{
  "mode": "embedded",
  "workspace_dir": "/tmp/fhm007/reme-stage1-workspace",
  "reme_config": "/path/to/ReMe/reme/config/fermes-hermes-stage1.yaml",
  "request_timeout": 600.0,
  "recall_timeout": 5.0,
  "health_timeout": 2.0,
  "health_retry_seconds": 30.0,
  "shutdown_timeout": 30.0,
  "recall_limit": 5
}
```

The Hermes runtime must pass an explicit disposable `hermes_home`; missing
`hermes_home` now fails closed rather than placing profile state in the current
directory. Keep each profile on its own workspace to prevent cross-profile
recall. No active Hermes installation, production profile, merge, or deployment
is authorized by this work.

## Next action

Rerun the focused suite after this pass, including the model-free real
`Application.run_job` and embedded receipt tests. The bounded reliability
gates are currently intended to be recorded as:

- **PASS:** same-profile writer ownership, fail-closed contention behavior,
  malformed/poison queue handling, and disk-append failure handling. The
  receipt/replay tests are present but execution is **UNKNOWN** in the supplied
  sandbox because its existing `write_file_safe` thread helper does not
  complete.
- **UNKNOWN:** Windows-specific lock CI and real conversational auto-memory
  capture through an approved local model. The local-model e2e remains
  explicitly outstanding; do not use cloud credentials or change LM Studio
  bind/firewall settings.
- **OPEN / UNKNOWN:** FHM-006 pre-compression checkpoint v2, prompt-injection
  and memory-poisoning controls, and independent review.

Keep issue #7 OPEN P1; do not deploy or merge until those checks pass.

## Supervisor host acceptance update

The earlier 38-test count is from the first implementation pass. The post-fix host suite now passes **47/47**, and the isolated alpha/beta Hermes provider recall and restart probes passed. Evidence and remaining gates are recorded in `INTEGRATION-QUALIFICATION.md`. Production integration remains unapproved.
