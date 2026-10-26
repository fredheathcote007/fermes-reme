# FHM-007 — ReMe Stage 1 model-tool contract qualification

project_id: fermes-hermes

Date: 2026-10-26 (Australia/Perth)
Status: Draft PR evidence; **NOT** production acceptance, deployment, or approval to merge to main.

## Failure and correction

- Original real Qwen 3.5 4B / AgentScope calls submitted seven `daily_write` operations with empty `{}` inputs. ReMe stored no note and incorrectly acknowledged the Hermes journal.
- Stage 1 declared `daily_write`, `read`, `write`, `edit` and `frontmatter_update` jobs without their JSON input parameters. The corrected local-only profile copies the existing upstream ReMe parameter contracts; it does not enable Dream, cron, HTTP serving, remote credentials, new vectors, or SQLite.
- AgentScope's current-turn tool calls/results are now redacted to tool name, ID and state, explicitly bounded to the current reply. The ReMe acknowledgement path fails closed on unpaired, missing, invalid, unknown, duplicate and mixed-result durable operations, including when a partial side effect has occurred. Missing AgentScope execution summaries are not accepted as no-op success.
- The provider retains its profile-local durable journal and stable request IDs. A fully observed no-tool turn can still be a legitimate no-op.

## Evidence and results

- Four focused suites, including AgentScope wrapper, idempotency, Hermes integration and synthetic file/BM25 capture: **101 passed**, exit 0; Project Coordinator private test log `/tmp/fhm007/integrated-final-tests.log`.
- Five new job-level receipt tests: missing current-turn evidence, missing result, wrong call ID, mixed success/error, and genuine no-tool no-op; **5/5 passed** in the idempotency suite.
- First actual model retest with the corrected Stage 1 schema and earlier correctness patch: **PASS**, Qwen 3.5 4B via authorised Dad-PC `10.1.1.50:1234`, Bubblewrap read-only root, two Markdown daily files, cleared journal, fresh-session BM25 retrieval containing synthetic marker and teal colour; exit 0. Evidence: `/tmp/fhm007/live-reme-capture-schema-contract.log`.
- Post-review remediation actual model retest: **PASS**, same isolated model/provider path with a newly created test home and workspace, two durable Markdown daily files, one request receipt, zero pending journal lines, restart retrieval of `FHM007-amber-kite-824` and colour `teal`; `ACCEPTANCE True`, exit **0**. Evidence: `/tmp/fhm007/live-reme-capture-final-contract.log`; isolated workspace `/tmp/fhm007/live-model-sandbox-4`.
- Written note includes `session_id` and `source_conversation` metadata linking to a synthetic session transcript. It does **not** include a cryptographically verifiable source-content hash.
- The first Cursor independent review identified a P1 missing-summary fallback and P2 missing job-level test; both were remediated. The second independent Cursor review (private log `/tmp/fhm007/fhm007-cursor-second-review.log`) found no remaining P0/P1 blockers to **draft PR update**. Remaining P2 scope: model-created note attribution for weak models, non-AgentScope fallback, partial side effects on retry, and update-path job-level regressions. This review is not a merge or deployment approval.

## Constraints and unresolved gates

- All tests use synthetic data and an isolated test environment; no active Hermes install or production profiles were modified.
- Dream, scheduler, proposed FHM-006 pre-compression checkpoint, SQLite/vector additions, Hindsight, and autonomous skill promotion remain disabled/deferred.
- Production acceptance still requires provenance/retention/poisoning and workspace ownership validation, FHM-006 fail-closed checkpoint contract, arbitrary crash/partial-write recovery, operational security of LAN API, and any additional independent reviewer findings.
- The observed successful real-model shutdown had no event-loop-closed exception, but failure-path asynchronous cleanup is not independently proven.
- First full GitHub matrix for `1a5d54e` found 16 failures in older tests that mocked AgentScope replies without the new required `tool_execution` record. These test doubles have been updated to declare explicit no-tool or successful paired tool evidence without relaxing the production fail-closed gate. **180/180** affected and focused host tests PASS after correction (`/tmp/fhm007/ci-remediation-host-tests.log`).
- The first Python quality check required trailing-comma/Black formatting and docstrings on new test fixture classes. Locally pinned `add-trailing-comma` and Black 26.5.1 with CI arguments now PASS. The baseline host `ruff` invocation remains non-authoritative (its failures also occur on `661a5ed`); full new GitHub quality and test matrix must pass before CI closure.
- Keep ReMe PR #1 a **draft** and FHM-007 Issue #7 **OPEN P1**. No main merge or deployment without owner approval.

## Rollback

No production rollback is required because these changes have not been deployed. To abandon the draft changes, retain the protected upstream baseline `661a5ed` and do not promote the isolated branch. Never force-push away other workers' commits.

## FHM-007 P2 follow-up checkpoint — 2026-10-26

- AgentScope create operations now inject server-owned session_id. The model schema hides this parameter and conflicts fail rather than overriding ownership.
- A successful daily_write without a note belonging to the requested session fails closed, preserving the queued turn and preventing a false receipt; a genuine no-tool no-op remains valid.
- An existing-note failed edit has an explicit job-level negative regression test.
- Internally owned OpenAI asynchronous clients are closed before shutdown; externally supplied clients are not closed by ReMe.
- Combined host results: 102 focused adapter tests PASS, 33 LLM/component tests PASS, and four selected injection tests PASS. Optional Claude SDK-dependent test was not run locally.
- Live model test on isolated Bubblewrap sandbox 5: two durable daily files; pending journal zero; new-session BM25 recalled FHM007-amber-kite-824 and teal; ACCEPTANCE True; exit 0. The model hit its six-iteration ceiling after writing; useful-memory acceptance still passed.
- Still draft-only, NOT deployment or main merge approved. Pending full CI and independent review; source-hash provenance, FHM-006 checkpoint, arbitrary crash/replay and profile ownership controls remain separate acceptance gates.

## Final Stage 1 patch assurance checkpoint (2026-10-26)

- Final local CI-equivalent tests: 85 memory/provider tests PASS and 26 LLM lifecycle, wrapper and attribution tests PASS, both exit 0.
- Session-ID schema hiding and rejection of a foreign model-supplied session ID is now regression-tested.
- Failure-path unit test awaits a synthetic model call that raises, then verifies the internally owned async client closes; externally injected models remain borrowed.
- Independent Cursor review: no P0 for the Stage 1 AgentScope/OpenAI path. P1 acceptance concern remains: a genuinely no-tool automatic-memory turn may acknowledge the spool without writing a note. This is compatible with the approved legitimate no-op policy but is not proof every explicit retention request will be stored.
- Keep the draft PR unmerged and production unchanged. FHM-007 is still OPEN P1 pending retention-intent/no-op contract, cryptographic provenance, FHM-006 checkpoint, crash replay and workspace security gates.
