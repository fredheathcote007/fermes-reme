# FHM-007 third-pass acceptance / security review (read-only)

**Identity gate:** `MEMORY-INDEX.md` declares `project_id: fermes-hermes` (line 3). Scope matches FHM-007 / ReMe fork work — **no STOP**.

**Basis:** Working tree on `fermes/hermes-provider-stage1` (base `363e497`), files listed in the task; prior log `/tmp/fhm007/cursor-final-diff-review.log` (pre–`request_id`/Stage1 YAML); host logs cited below (not re-run). No repo writes, network, or secrets.

**Host evidence (independent, cited):** `reme-stage1-final-host-tests.log` — **47 passed**; Plugin Doctor PASS; `real-hermes-loader-smoke.log` — loader discovers provider; `offline-bm25-probe.log` — BM25 recall; `two-profile-bm25-probe.log` — `TWO_PROFILE_BM25_ISOLATION_PASS`; `real-provider-alpha.log` / `real-provider-beta.log` — loader + init/prefetch/shutdown/restart, `CROSS_LEAK False`.

---

## Executive judgment

The uncommitted delta is a **material improvement** over the first-pass review: durable JSONL spool with symlink/`O_NOFOLLOW` hardening, parent-dir `fsync`, `request_id` plumbed through to ReMe, writer **continues** with timer retry (no first-failure thread exit), `hermes_home` fail-closed, dedicated `fermes-hermes-stage1.yaml`, and broader unit coverage (malformed ack, ack-failure replay ID, Stage1 config parse).

**Release / production acceptance** is **not** asserted here (per instruction). For **draft PR readiness**: implementation and hermetic tests are in good shape for review; remaining gaps are **qualification evidence**, **config enforcement at the wizard default**, **multi-process spool**, and **end-to-end ReMe receipt idempotency** (not fake-backend-only).

---

## Reconciliation vs prior review (`cursor-final-diff-review.log`)

| Prior finding | Third-pass status |
|---------------|-------------------|
| P0-3 no `request_id` | **Resolved in code** — see chain below |
| P0-1 Dream / default config | **Mitigated by optional Stage1 YAML + test**; **not enforced** if profile still uses `reme_config: "default"` |
| P0-2 remote OpenAI default | **Mitigated in Stage1 YAML** (loopback defaults); wizard/schema still default `reme_config` / HTTP mode |
| P1-a success before ack → duplicate | **Mitigated in design** when real ReMe `auto_memory` runs (receipt before success return); **Hermes layer** tested with fake backend only |
| P1-b writer exits on first failure | **Resolved** — `_write_loop` + `_schedule_retry` |
| P1-d multiprocess spool | **Still open** — no cross-process lock |
| P1-e parent-dir fsync | **Resolved** — `_fsync_directory` after append and ack |
| P1-f `cwd` without `hermes_home` | **Resolved** — `_disable_without_profile` |
| P0-5 BM25 / cross-profile | **Host PASS** on two-profile probe; qual doc still understates this |
| FHM-006 v2 checkpoint | **Unchanged** — no Hermes hook in integration |

---

## Idempotency: `request_id` → journal → receipt

**Forwarding (confirmed):**

```686:741:integrations/hermes_agent/__init__.py
                        if not self._record_payload(
                            payload["payload"],
                            request_id=payload["id"],
                            respect_cooldown=False,
                        ) or not self._remove_pending_write(payload["id"]):
                            retry = True
...
                response = self._backend.auto_memory(
                    payload["session_id"],
                    payload["messages"],
                    timeout=self._request_timeout,
                    request_id=request_id,
                )
```

```257:261:integrations/hermes_agent/embedded_backend.py
            if request_id is not None:
                kwargs["request_id"] = request_id
            return self._submit(
                self._run_job("auto_memory", **kwargs),
```

**ReMe receipt (confirmed contract):**

```464:506:reme/steps/evolve/auto_memory.py
        request_id = self.context.get("request_id")
        ...
        async with await get_path_lock(lock_path):
            path = _checked_write_path(workspace, f"{directory}/{request_key}.json", allowed)
            if path.exists():
                record = json.loads(path.read_text(encoding="utf-8"))
                if record["fingerprint"] != fingerprint:
                    ...
                    return
                response = Response.model_validate(record["response"])
                ...
                return
            await self._execute()
            if self.context.response.success:
                ...
                temporary.replace(path)
```

**Crash after successful ReMe write, before Hermes spool ack**

| Layer | Verdict |
|--------|---------|
| Hermes spool retains line; replay sends **same** `payload["id"]` | **Confirmed** (`test_retried_spool_record_reuses_record_id_after_ack_failure`) |
| ReMe returns `success=True` only after `_execute()`; receipt persisted **before** step returns on success path | **Confirmed by code order** (lines 507–522 after success) |
| Duplicate **memory agent / file side effects** on replay | **Mitigated** when receipt exists (replay returns stored response, skips `_execute`) |
| Same guarantee with **real** `Application` + LLM path | **Hypothesis** — no unit test writes receipt under workspace and replays `run_job("auto_memory", request_id=…)` twice |
| Crash **during** `_execute()` before receipt | **Unproven / at-least-once** — partial workspace writes possible; not Hermes-spool-specific |

**Conclusion:** “No duplicate on ack-failure replay” is **sound at the design level** and **proven for the adapter** with mocks; **not** fully proven for embedded real `auto_memory` side effects. Host restart logs prove **BM25 recall**, not spool-idempotency.

---

## Durability, errors, concurrency

| Topic | Severity | Status | Location / action |
|--------|----------|--------|-------------------|
| Append + `fsync` + `0o600` / dir `0o700` | — | **Confirmed** | `__init__.py` 562–593 |
| Symlink / `O_NOFOLLOW` on spool | — | **Confirmed** | 567–578, 599–601, 636–638; `test_spool_rejects_symlink_and_sets_private_permissions` |
| Bad/malformed `auto_memory` response keeps spool | — | **Confirmed** | 742–753; `test_false_or_malformed_auto_memory_ack_keeps_spool_record` |
| `success: false` does not stall later turns | — | **Confirmed** | `_write_loop` + `test_failed_first_record_does_not_stall_later_record` |
| `sync_turn` on spool **OSError** (disk full, etc.) | **P2** | **Confirmed defect (UX)** | `_enqueue_write` logs `error` but `sync_turn` always warns “shutting down” (423–427) — **misleading, not silent loss** |
| In-process races | — | **Mostly OK** | `_pending_lock`, `_backend_lock`, `_write_thread_lock`; backend serialization tests |
| Multi-process same `HERMES_HOME` | **P1** | **Confirmed gap** | No `fcntl`/file lock on JSONL; interleaved append or lost ack possible — document single-writer or add lock (**next regression**) |
| `_remove_pending_write` read–rewrite–replace | **P1** | **Hypothesis under MP** | 639–665 — safe single-writer; two processes compacting → corruption/loss risk |
| Invalid JSONL lines | — | **Confirmed** | Skipped on read, retained on compact (621–627, 647–649) — no dedicated “bad line + good line” test (**next regression**) |
| Reinit while writer alive | — | **Confirmed** | 130–134 `RuntimeError` |
| Shutdown timeout | — | **Confirmed** | Spool survives; warning at 457–462 |

---

## Stage1 YAML (`reme/config/fermes-hermes-stage1.yaml`)

| Check | Result |
|--------|--------|
| No `dream_cron` / cron jobs | **Confirmed** — `test_stage1_config_is_local_only_and_has_no_unattended_jobs` |
| `service.backend: cli` | **Confirmed** (L5–6) — no HTTP listener |
| BM25-only path | **Confirmed** — `embedding_store: ""`, `vector_weight: 0.0` on search |
| LLM defaults | **Loopback** `127.0.0.1:1234/v1`, placeholder model (L107–114) — **not** cloud by default; user/env can still point elsewhere (**P2** operational) |
| `permission_mode: bypass` (L122) | **Not arbitrary shell** — `auto_memory` exposes only file jobs (`daily_write`, `read`, `edit`, …) (`auto_memory.py` 176–177, 631). `bypass` skips AgentScope permission prompts for those tools (`as_agent_wrapper.py` 327). **P2**: align with Fermes threat model vs `default.yaml`; not a new shell grant in this diff |
| Standalone file (no merge with `default.yaml`) | **Confirmed** — avoids Dream/proactive leakage |

**Gap:** `ReMeConfig.reme_config` and wizard **default remain `"default"`** (`config.py` 33, `get_config_schema` 239). Stage1 safety is **opt-in** via profile JSON — **P1** for qualification checklist, **P2** for draft PR if README/qual doc require explicit `fermes-hermes-stage1`.

---

## FHM-006 / checkpoint / poisoning

| Item | Severity | Status |
|------|----------|--------|
| Hermes pre-compress checkpoint v2 | **P0 (product gate)** | **Confirmed absent** — no matches under `integrations/hermes_agent/` |
| Recall prompt injection / memory poisoning | **P1** | **Unverified** — unchanged Hermes fencing; no semantic filter on prefetch |
| Host real-provider tests | — | Synthetic tokens; restart recall — **supporting**, not poisoning gate |

---

## Tests

**Strengths:** 47 host + focused file; spool replay (`test_accepted_write_is_replayed_after_backend_failure`); ack-failure same ID; malformed/false ack; Stage1 config; HTTP `request_id`; symlink/permissions; `hermes_home` fail-closed.

**No duplicate-assertion bugs found** — e.g. `test_failed_first_record` uses correlated `calls` vs `request_ids` checks intentionally.

**Recommended next regressions (ordered):**

1. **P1** — `Application` + real `auto_memory` step: two `run_job` calls with same `request_id` → one receipt file, second returns cached response (workspace fingerprint).
2. **P1** — Two processes/threads append+ack on same spool path (expect defined behavior or explicit failure).
3. **P2** — Corrupt line + valid line → valid still delivered.
4. **P2** — `sync_turn` after simulated `OSError` on append → assert **not** “shutting down” message (fix message in same PR).
5. **P2** — Wizard/docs: default `reme_config` for Fermes points at `fermes-hermes-stage1` or validation warning in `save_config` for embedded Fermes profiles.

---

## Findings matrix (defect vs hypothesis)

### P0 — Stage 1 / product acceptance (not draft-PR blockers by themselves)

| ID | Type | Finding | Next action |
|----|------|---------|-------------|
| P0-A | **Confirmed** | FHM-006 v2 checkpoint not implemented in provider | Document in acceptance; Stage 2 hook if required |
| P0-B | **Confirmed** | Stage1 config not enforced when profile uses upstream `default` | Qual checklist + optional `save_config` guard; doc INTEGRATION-QUALIFICATION profile JSON |
| P0-C | **Hypothesis** | End-to-end idempotency of **file/memory side effects** under real `auto_memory` + LLM | Test (1) above; until then treat as at-least-once beyond receipt replay |

### P1 — Integration / security

| ID | Type | Finding | Next action |
|----|------|---------|-------------|
| P1-1 | **Confirmed** | Multi-process spool unsafe | Document invariant or file locking |
| P1-2 | **Hypothesis** | Concurrent compact of spool | Same as P1-1 |
| P1-3 | **Confirmed** | `INTEGRATION-QUALIFICATION.md` understates host BM25/cross-profile/restart evidence (L86–87 vs host PASS) | Update draft qual doc before PR narrative |
| P1-4 | **Unverified** | Prompt-injection / poisoning gate | Fork-plan gate 4; out of this diff |

### P2 — Operational / polish

| ID | Type | Finding | Next action |
|----|------|---------|-------------|
| P2-1 | **Confirmed** | Wrong `sync_turn` warning on non-shutdown enqueue failure | Fix message + test |
| P2-2 | **Confirmed** | HTTP mode remains default in `ReMeConfig` | Fermes Stage1 embedded-only in qual |
| P2-3 | **Confirmed** | `index_update_loop` still background in Stage1 YAML | Intended for BM25 freshness; document CPU/wake behavior |
| P2-4 | **Hypothesis** | User sets `FERMES_STAGE1_LLM_BASE_URL` to cloud | Env discipline in docs |

---

## Draft PR readiness (not merge / not production)

| Criterion | Assessment |
|-----------|------------|
| Identity / DEC-004 (fork, file/BM25, no SQLite/new vector in diff) | **Ready** |
| Hermetic unit suite (47 host) + Plugin Doctor + loader smoke | **Ready** as CI/evidence attachments |
| Code review themes (spool, `request_id`, retry, fail-closed home) | **Ready** for review |
| Qual doc accuracy vs host logs | **Needs sync** (test count, cross-profile, doctor) |
| Acceptance gates (FHM-006, poisoning, real idempotency, MP spool) | **Explicitly open** in PR description |

**Required before calling the PR “qualification-complete”:** items P0-C test, P1-1 decision, P1-3 doc sync, P0-B enforcement strategy.

**Not re-run here:** `pre-commit`, full `pytest tests/unit`, HTTP tests in sandbox (`INTEGRATION-QUALIFICATION.md` L27).

---

## Code anchors (quick reference)

- Spool accept / reject: `integrations/hermes_agent/__init__.py` 396–427, 507–517, 562–675, 677–786
- Backends: `embedded_backend.py` 243–261; `http_backend.py` 42–57; `backend.py` 29–41
- Stage1 config: `reme/config/fermes-hermes-stage1.yaml`
- Tests: `tests/unit/test_hermes_agent_integration.py` 291–324, 813–943
- Qual draft: `docs/fermes/INTEGRATION-QUALIFICATION.md`

This review is read-only; no files, branches, issues, network, or models were used.

## Supervisor clarification after independent review

The outstanding draft-document stale-evidence finding was superseded by the append-only supervisor verification update in `INTEGRATION-QUALIFICATION.md`, which records 47/47 host tests, real loader, Plugin Doctor, standalone BM25, two-profile isolation and restart evidence. The review's remaining P0/P1 findings (real ReMe request-id side-effect fault injection, shared-profile multi-process operation, FHM-006 fail-closed checkpoint, and poisoning) are not resolved by those tests. A draft PR is not a production acceptance or permission to merge.
