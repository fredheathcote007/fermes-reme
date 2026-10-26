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
- Baseline source lint/Black checks in the local host tool versions already report failures on unchanged `661a5ed`. Do not classify them as new regressions or claim these checks passed; the authoritative new-commit GitHub workflows must be verified.
- Keep ReMe PR #1 a **draft** and FHM-007 Issue #7 **OPEN P1**. No main merge or deployment without owner approval.

## Rollback

No production rollback is required because these changes have not been deployed. To abandon the draft changes, retain the protected upstream baseline `661a5ed` and do not promote the isolated branch. Never force-push away other workers' commits.
