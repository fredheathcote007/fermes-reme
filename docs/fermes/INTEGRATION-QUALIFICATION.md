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
| Embedded lifecycle | Fake `reme.Application` tests cover start, health, jobs, close, and bounded failure cleanup. | PASS in the recorded host suite; rerun this second pass in the disposable qualification environment. |
| Recall timeout | Backend lock, startup, health, and search share the configured recall deadline. | PASS in focused tests |
| Accepted-write semantics | Completed turns are JSONL-spooled and `fsync`ed before enqueue. The spool UUID is forwarded as ReMe's existing `auto_memory(request_id)`. Successful responses remove one record atomically; false/malformed responses, backend errors, shutdown timeout, or process loss retain it. | PASS in focused regression coverage; at-least-once and not multi-process safe |
| Restart recovery | A failed first backend leaves one record; a fresh provider using the same profile replays and clears it. | PASS |
| Full Hermes Plugin Doctor | `/tmp/fhm007/reme-plugin-doctor-host.log` records real Hermes Plugin Doctor manifest, import, discovery, and registration success. | PASS in host evidence |
| Real Hermes loader | `/tmp/fhm007/real-hermes-loader-smoke.log` records `load_memory_provider` returning `ReMeMemoryProvider`, `available=True`, in a disposable profile. | PASS in host evidence |
| HTTP transport | Existing tests require a loopback fixture. The worker sandbox denies socket creation (`EPERM`). | UNVERIFIED here |
| Real ReMe BM25 recall | `/tmp/fhm007/offline-bm25-probe.log` records a real embedded Application with seeded Markdown, BM25-only evidence for the expected copper reference, and clean close; no model/cloud was used. | PASS for seeded single-workspace smoke; fresh-session/cross-profile gate remains |

## Durability boundary

The spool is profile-local under `$HERMES_HOME/reme/pending-writes.jsonl`, is
not indexed as ReMe memory, and is included in Hermes profile backup paths by
being inside the profile. Stage 1 provides at-least-once delivery after a
successful local `fsync`; a crash after ReMe accepts a write but before the
spool record is removed can replay the same turn. The stable spool UUID is
passed to ReMe's existing `auto_memory` receipt/idempotency implementation,
which fingerprints the request and replays its stored response for the same
ID/input. A reuse with different input is rejected. This does not claim
exactly-once effects beyond that existing ReMe behavior or multi-process
safety. If the spool cannot be written, `sync_turn` logs an error and does not
report the turn as accepted by the provider.

The background writer is bounded by Hermes/provider shutdown timeouts. A failed
record is retained and scheduled for delayed retry without preventing later
accepted records from being attempted. Unattended Dream is absent from the
dedicated Stage 1 config; the upstream `default` config remains unchanged.

## Review-needed issues and next step

Independent review should rerun the focused suite in the disposable
environment, then seed isolated workspaces and verify fresh-session retrieval
and no cross-profile leak through real BM25 without a live model. The reviewer
should also test crash windows, same-ID update semantics, FHM-006 checkpoint
compatibility, and prompt-injection/poisoning gates before deployment or merge.
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

- Fresh-session retrieval and cross-profile isolation have not both been
  demonstrated against the real provider with seeded workspaces.
- Crash-window replay has deterministic unit coverage, but no claim is made
  that arbitrary backend side effects are exactly once; shared-profile
  multi-process spool operation is not qualified.
- FHM-006 pre-compression checkpoint v2 compatibility and the prompt-injection/
  memory-poisoning gate remain unverified.
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
