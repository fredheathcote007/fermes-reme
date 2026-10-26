# FHM-007 — Offline capture/restart qualification

project_id: fermes-hermes
date: 2026-10-26
repository: fredheathcote007/fermes-reme
branch: fermes/hermes-provider-stage1
status: Synthetic model-boundary integration PASS; real model E2E still NOT QUALIFIED

## Scope and evidence

The test in `tests/unit/test_fermes_capture_integration.py` exercises the actual
Hermes `ReMeMemoryProvider.sync_turn`, fsync-backed profile journal, embedded
ReMe application and `auto_memory`, AgentScope tool-call execution, daily Markdown
write, session evidence, provider shutdown/reinitialisation, file-backed BM25 search
and fresh-session `prefetch` retrieval. It creates isolated workspaces and
`HERMES_HOME` under pytest's disposable `tmp_path`.

Only `OpenAIChatModel._call_api` is replaced by a deterministic model-response
double. Neither the embedded ReMe backend, AgentScope tools, file reads/writes,
nor search implementation is mocked. The fixture uses the real Stage 1 no-Dream
profile, substitutes the model identifier and a non-responsive loopback URL
(no outbound inference), and emits two known ReMe tool invocations. It does
**not** establish that a real model identifies and extracts useful lessons.

The test asserts a generated daily memory with a distinctive synthetic marker,
session-level source metadata and a persisted transcript, correctly exercised
`daily_write` and `frontmatter_update` tools, journal acknowledgement,
shutdown, fresh-session BM25 retrieval and source-path presence.

## Untrusted-memory and provenance regression — this iteration

`test_hostile_recalled_memory_is_passive_fenced_data_with_provenance` seeds an
isolated daily note and source transcript with forged `SYSTEM`/`USER` role
labels, a fake `TOOL_CALL`, a fake MEMORY system note, a false FHM-006 success
claim, and nested memory fences. It then uses the real embedded
`ReMeMemoryProvider`, ReMe BM25 search, Hermes `MemoryManager.prefetch_all`,
and Hermes `build_memory_context_block`/streaming scrubber. No security helper
or ReMe search implementation is mocked. The test verifies passive prefetch
has no `pwned.txt` side effect, recall retains the daily source path, the
provider exposes no tool schemas in this path, and Hermes fences recalled data
while stripping nested provider-supplied fences/system notes even when tags
are split across streaming chunks.

The synthetic capture assertions also confirm that current auto-memory files
provide a session pointer and transcript, but do **not** provide a
`source_hash`. That is a documented provenance gap; this iteration does not
alter the ReMe metadata format. The corrupted/partial Markdown fixture checks
that malformed source data does not suppress valid BM25 recall. These checks
establish source-level/passive boundary behavior only; they do not prove
behavioral resistance of a real LLM.

The bounded sandbox rerun did not qualify this new test: the embedded ReMe
startup timed out under the supplied Python 3.14 environment, after which the
provider remained unavailable. The full requested focused command reached
13 passing tests and then stopped at the pre-existing loopback HTTP fixture
with `PermissionError: [Errno 1] Operation not permitted` when the sandbox
denied socket creation. Host/CI rerun is therefore required before counting
this regression as a PASS.

Host qualification command (authorised development PC only):

```bash
PYTHONPATH="$HERMES_SOURCE:$REME_ROOT" HERMES_HOME=/tmp/fhm007/all-stage1-host-home \
  /tmp/fhm007/reme-fork-testvenv/bin/python -m pytest -p no:cacheprovider -q --maxfail=1 \
  tests/unit/test_hermes_agent_integration.py \
  tests/unit/test_auto_memory_idempotency.py \
  tests/unit/test_fermes_capture_integration.py
```

Result: **66 passed in 14.13s**, log `/tmp/fhm007/all-stage1-host.log`.
ReMe CI file `.github/workflows/fermes-reme-ci.yml` now includes all
three focused suites on GitHub-hosted `ubuntu-24.04` for PR validation.

## Independent challenge

Read-only Cursor review `/tmp/fhm007/cursor-capture-review.log` confirmed the
capture route genuinely exercises the provider and AgentScope file tools rather
than seeding Markdown directly. Its recommended corrections are incorporated:
CI coverage, dynamic test date, and explicit source/path assertions. The
review also correctly notes that deterministic tool output is not model
reasoning, and that this test complements (does not replace) alpha/beta
profile isolation, receipt fault injection and real LM Studio qualification.

## Safety and unfinished gates

- No new SQLite/vector backend or external memory service.
- No unattended Dream/proactive tasks; no real Hermes profiles or transcripts.
- No default-branch merge or production deployment.
- A local model served over an explicitly authorised route must still complete
  genuine conversational capture, extraction and useful subsequent recall.
  Dad-PC's LM Studio listener is currently loopback-only; this work does not
  change any network binding/firewall or credentials.
- FHM-006 precompaction fail-closed checkpoint, memory-poisoning and provenance
  trust boundaries, mid-agent crash partial writes and cross-profile shared
  workspace protections remain separate acceptance gates.
- The hostile-memory test is evidence for fencing and passive provider routing,
  not semantic filtering or real-model prompt-injection resistance. Source
  hashes remain absent from current auto-memory frontmatter.
- Future SkillHone and SkillOpt-Sleep integration remains read-only against
  untrusted memories until promotion approval/evaluation controls exist.

## Exact next action

Run the focused security/capture tests in the maintained host venv and review
the resulting source-level evidence independently. Then decide the
implementation/ownership details in `FHM006-CHECKPOINT-GAP.md`; only after
that decision should an approved local-model route be used to qualify genuine
capture. Do not change Hermes, enable checkpoint v2, merge, or deploy from
this qualification.
