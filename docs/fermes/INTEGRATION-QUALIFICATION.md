# FHM-007 Stage 1 integration qualification

project_id: fermes-hermes
source_sha_at_start: 363e497ae96733271414ea4224e274e5de5e5253
branch: fermes/hermes-provider-stage1
status: draft review evidence; not deployed

## Scope

This qualification covers ReMe's existing `integrations/hermes_agent` provider in
embedded-first Stage 1. It keeps ReMe's file-backed store and BM25 path. It does
not add SQLite, a new vector backend, Hindsight, Project Coordinator, a model
endpoint, or changes to the Hermes checkout.

## Regression matrix

| Area | Evidence in this fork | Result |
|---|---|---|
| Provider contract | Focused unit suite imports the Hermes `MemoryProvider` when the Hermes checkout is on `PYTHONPATH`; registration callback accepts and registers a fresh `ReMeMemoryProvider`. | PASS |
| Profile isolation | `hermes_home` is used for provider configuration and the pending-write spool; scoped session IDs include profile and session identity. | PASS in focused tests |
| Same-profile writer ownership | A profile-local OS advisory lock is held for the provider lifetime; a second provider in the same process or another process fails closed. The lock file is not used as a stale marker. | PASS in focused tests; Windows-specific CI evidence UNKNOWN until rerun |
| Embedded lifecycle | Fake `reme.Application` tests cover start, health, jobs, close, and bounded failure cleanup. | PASS in the recorded host suite; rerun this second pass in the disposable qualification environment. |
| Recall timeout | Backend lock, startup, health, and search share the configured recall deadline. | PASS in focused tests |
| Accepted-write semantics | Completed turns are JSONL-spooled and `fsync`ed before enqueue. The spool UUID is forwarded as ReMe's existing `auto_memory(request_id)`. Successful responses remove one record atomically; false/malformed responses, backend errors, shutdown timeout, or process loss retain it. | PASS in focused regression coverage; shared-profile writes are fail-closed |
| ReMe receipt idempotency | Model-free tests now exercise the actual `Application.run_job` path and an actual embedded provider path with synthetic `_execute` instrumentation. They assert same ID/input side effects once, cached replay, different-input rejection, and stable retry ID after a successful backend response followed by failed spool acknowledgement. | UNKNOWN in this sandbox: the supplied Python 3.14 environment hangs in the existing `write_file_safe` thread helper before the receipt assertions; rerun in CI/host venv |
| Queue poisoning and disk failure | Malformed/poison records do not hide a later valid record or get silently discarded; append failure does not enqueue or acknowledge a turn. | PASS in focused tests |
| Restart recovery | A failed first backend leaves one record; a fresh provider using the same profile replays and clears it. | PASS |
| Full Hermes Plugin Doctor | `/tmp/fhm007/reme-plugin-doctor-host.log` records real Hermes Plugin Doctor manifest, import, discovery, and registration success. | PASS in host evidence |
| Real Hermes loader | `/tmp/fhm007/real-hermes-loader-smoke.log` records `load_memory_provider` returning `ReMeMemoryProvider`, `available=True`, in a disposable profile. | PASS in host evidence |
| HTTP transport | Existing tests require a loopback fixture. The worker sandbox denies socket creation (`EPERM`). | UNVERIFIED here |
| Real ReMe BM25 recall | `/tmp/fhm007/offline-bm25-probe.log` records a real embedded Application with seeded Markdown, BM25-only evidence for the expected copper reference, and clean close; no model/cloud was used. | PASS for seeded single-workspace smoke; fresh-session/cross-profile gate remains |
| Untrusted memory/passive prefetch | `test_hostile_recalled_memory_is_passive_fenced_data_with_provenance` uses a real ReMe provider/BM25 path plus maintained Hermes `MemoryManager` and memory-context fencing against forged roles, tool directives, fake system notes, nested fences, and a false FHM-006 claim. | Source-level PASS when host Hermes source is available; not real-LLM behavioral proof |
| Auto-memory provenance | Synthetic capture checks `session_id`, `source_conversation`, transcript presence, and actual recall source path. Current auto-memory frontmatter has no `source_hash`. | PASS for available pointers; source-hash gap documented, not changed |
| Corrupted/partial source | The same isolated security fixture includes invalid/truncated Markdown and verifies valid BM25 recall remains available. | PASS when the embedded host run completes; warning/log portability remains to be rerun |

## Durability boundary

The spool is profile-local under `$HERMES_HOME/reme/pending-writes.jsonl`, is
not indexed as ReMe memory, and is included in Hermes profile backup paths by
being inside the profile. One provider process owns a profile's spool through
an OS advisory lock at `$HERMES_HOME/reme/.writer.lock`; the file may remain
after shutdown, but only the OS lock controls ownership. A second provider
fails closed for both recall and capture, and callers can inspect
`unavailable_reason()`. This is an explicit single-writer invariant for one
Hermes profile; distinct profiles/workspaces remain required.

Stage 1 provides at-least-once delivery after a successful local `fsync`; a
crash after ReMe accepts a write but before the spool record is removed can
replay the same turn. The stable spool UUID is passed to ReMe's existing
`auto_memory` receipt/idempotency implementation, which fingerprints the
request and replays its stored response for the same ID/input. A reuse with
different input is rejected. This does not claim exactly-once effects for
arbitrary backend side effects. If the spool cannot be written, `sync_turn`
logs an error and does not report the turn as accepted by the provider.

The background writer is bounded by Hermes/provider shutdown timeouts. A failed
record is retained and scheduled for delayed retry without preventing later
accepted records from being attempted. Unattended Dream is absent from the
dedicated Stage 1 config; the upstream `default` config remains unchanged.

## Review-needed issues and next step

Independent review should rerun the focused suite in the disposable
environment, then seed isolated workspaces and verify fresh-session retrieval
and no cross-profile leak through real BM25 without a live model. The reviewer
should also test Windows lock behavior, crash windows, same-ID update
semantics, FHM-006 checkpoint compatibility, and prompt-injection/poisoning
gates before deployment or merge.
No Hermes installation, default `~/.hermes`, Qwen/LM Studio worktree, or Hermes
PR #19 was modified.

## Disposable Stage 1 profile

Use this profile JSON with a unique temporary `hermes_home`; do not add API
keys or other secrets:

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

The Stage 1 YAML defaults `FERMES_STAGE1_LLM_BASE_URL` to
`http://127.0.0.1:1234/v1` and `FERMES_STAGE1_LLM_MODEL` to
`local-placeholder`; configure a local server explicitly if the auto-memory
writer is exercised. Embedded mode uses `service.backend: cli` and does not
open an HTTP listener.

## Unresolved acceptance blockers

- Real conversational auto-memory capture through an approved local-model
  endpoint is **UNKNOWN / OUTSTANDING**. No live model or cloud credential was
  used by this pass.
- Fresh-session retrieval and cross-profile isolation are **PASS** in the
  recorded seeded BM25/provider evidence, while arbitrary backend side effects
  beyond ReMe's receipt remain **UNKNOWN**.
- Same-profile ownership is **PASS** in bounded focused tests. ReMe receipt
  replay/crash-window assertions are **UNKNOWN** until the model-free tests can
  complete in a host/CI environment; Windows-specific lock CI is also
  **UNKNOWN** until rerun.
- FHM-006 pre-compression checkpoint v2 compatibility and the
  prompt-injection/memory-poisoning gate remain **OPEN / UNKNOWN**.
- This iteration adds source-level evidence for passive recall fencing, but not
  semantic filtering, real-LLM resistance, or a trusted-memory promotion path.
- Current auto-memory source provenance is a session pointer/path, not a
  content hash. `FHM006-CHECKPOINT-GAP.md` is a proposed adapter design only;
  it does not enable the v2 marker or alter the provider contract.
- No production deployment, active Hermes profile change, merge, or release
  approval is authorized by this qualification draft.

## Supervisor-verified host evidence — 2026-10-26 (supersedes earlier pending checks)

- **47/47** focused Hermes adapter tests PASS using disposable ReMe environment and real maintained Hermes source. Log: `/tmp/fhm007/reme-stage1-final-host-tests.log`. The initial rerun found a missing `auto_memory` mock-server response; the fixture was corrected and the full suite rerun.
- Real Hermes Plugin Doctor PASS: `/tmp/fhm007/reme-plugin-doctor-host.log`.
- Real Hermes plugin loader PASS with ReMe provider available: `/tmp/fhm007/real-hermes-loader-smoke.log`.
- Real embedded, model-free BM25 recalled a synthetic Markdown memory and closed cleanly: `/tmp/fhm007/offline-bm25-probe.log`.
- Two isolated profile workspaces (alpha/beta) each returned their own synthetic token and not the other's: `/tmp/fhm007/two-profile-bm25-probe.log`.
- Actual Hermes loader plus ReMe provider initialize/prefetch/shutdown/restart: both alpha and beta recalled their own persisted evidence after restart, with no cross-token recall. Logs: `/tmp/fhm007/real-provider-alpha.log` and `/tmp/fhm007/real-provider-beta.log`.
- ReMe Stage1 config resolved successfully to CLI service, no Dream/proactive cron, and loopback-only model defaults; no live inference call was made. LM Studio on Dad-PC is currently bound to `127.0.0.1:1234`, not remotely accessible from Project Coordinator.
- **Still unproven:** real conversational auto-memory capture with a local model; forced mid-write crash replay against the actual ReMe engine; simultaneous multi-process writers, shared-workspace authorisation, prompt-injection controls, FHM-006 checkpoint v2, and skill-learning promotion. Do not merge or deploy.

## Reliability qualification update — 2026-10-26 (supervisor host verification)

- **65/65 tests PASS** in authorised Project Coordinator host Python 3.14 environment using real maintained Hermes source on PYTHONPATH: `tests/unit/test_hermes_agent_integration.py` plus `tests/unit/test_auto_memory_idempotency.py`. Log: `/tmp/fhm007/reliability-final-combined-host.log`.
- The real `Application.run_job` synthetic receipt replay test now uses the necessary file graph/keyword index fixture and PASSes. Previous sandbox timeouts must not be counted as runtime failures; real host proof does not use a live LLM. Duplicate request with same ID and content reuses the response; conflicting payload rejects. This does not establish exactly-once side effects if a process dies midway through an uncommitted ReMe auto-memory execution.
- Exclusive per-profile writer lock added via OS lock (`fcntl.flock` on Linux, `msvcrt.locking` on Windows) plus process-local ownership guard. Linux competing-process smoke: second process fails while lock held, then succeeds after release; log `/tmp/fhm007/multiprocess-lock-host.log`. Windows branch still requires its latest CI run.
- Journal tests now cover lock contention, poisoned/invalid record retention while processing valid entries, and disk-append failure without accepting a turn. ReMe remains file-backed/BM25-only Stage 1, Dream disabled.
- New independent Cursor source review at `/tmp/fhm007/cursor-reliability-final-review.log`: draft-PR suitable, production NO GO. Residual scope: same *ReMe workspace* shared across different Hermes homes remains forbidden; Windows lock needs CI; arbitrary mid-operation LLM crash idempotency not proven; FHM-006 fail-closed compaction, provenance/poisoning and real LLM-backed conversational learning remain open.

## This iteration exact next action

Run `tests/unit/test_fermes_capture_integration.py` and the maintained Hermes
provider suite in `/tmp/fhm007/reme-fork-testvenv` with the stated `PYTHONPATH`
and disposable `HERMES_HOME`, then obtain independent review of the new
source-level security evidence. Treat the local Python 3.14 embedded-startup
timeout as an environment qualification issue unless it reproduces in the
host venv. Do not modify Hermes, enable FHM-006 v2, use a live model, merge,
or deploy.

Bounded sandbox result for this iteration: `compileall`, Black (120-column
repository settings), Ruff, and `git diff --check` passed. The focused pytest
command reached **13 passed** before the existing HTTP fixture failed on
socket permission (`EPERM`); the new hostile-memory test separately reached
the real Hermes import path but could not get past embedded ReMe startup
timeout. These are unresolved qualification results, not claims of host
security-test success.
