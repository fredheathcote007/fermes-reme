# FHM-007 Option B — deterministic commit and security contract

project_id: `fermes-hermes`
status: implementation candidate in this isolated worktree; not merge or deployment approval
date: 2026-10-27
scope: the explicitly opted-in Fermes Stage 1 `AutoMemoryStep` path only

This contract turns the crash-window proposal into a bounded implementation
for the local embedded AgentScope path. The proposal and the red reproduction
are on the sibling crash-gate history (`942381f`); this worktree starts from
`4463d20` plus the committed red test `0348b47`.

## Supported storage and authority

The supported durability substrate is one Linux process/profile using a local
POSIX filesystem whose `rename(2)`, file `fsync(2)`, and directory `fsync(2)`
semantics are reliable (the qualification target is local ext4 or xfs). NFS,
SMB, FUSE/cloud mounts, overlay/copy-up filesystems, and filesystems with
unknown power-loss ordering are outside this contract. The Hermes profile
writer lock and ReMe workspace-owner marker remain required; two independent
profiles or non-ReMe writers targeting one workspace are not supported.

The model, its text, tool calls, tool results, frontmatter supplied by the
model, and recalled memory are untrusted. In protected mode the model receives
only the read job and may return one complete JSON revision proposal. It never
receives `daily_write`, `write`, `edit`, `move`, or `frontmatter_update`, and
cannot write the authoritative daily note. The application owns the target
path, provenance, marker, serialization, digest, receipt, and index refresh.

The application-controlled marker is a reserved `_reme_commit` frontmatter
object. It binds:

* the Hermes/ReMe `request_id` and request fingerprint;
* the trusted session identifier and configured agent scope;
* the trusted transcript `source_hash`;
* the canonical workspace-relative note path; and
* the SHA-256 digest of the canonical UTF-8 body stored after frontmatter
  serialization and reload; the receipt separately binds the exact serialized
  note bytes.

Model metadata cannot create, replace, or modify this object. A receipt stores
the same commit evidence plus the exact committed file digest and response.

## Commit scope and linearization point

Option B commits exactly one complete daily-note revision for one request. A
revision is a server-built Markdown document containing trusted provenance,
the application marker, the validated model-proposed name/description/body,
and preserved non-reserved metadata from the existing note. An update is
compare-and-swap checked against the bytes observed before the model proposal;
if another writer changes the note, the request fails closed.

The commit linearization point is the successful sequence:

1. create a same-directory temporary file with exclusive creation;
2. write all bytes, `fsync` the temporary file, and atomically `rename` it over
   the validated final name; and
3. `fsync` the containing directory.

After step 3, a retry may treat the marker and exact body digest as committed.
The receipt is then written as a separate same-directory durable replacement
and its directory is fsynced. There is no claim of a cross-file atomic commit:
the marker is the recovery record when the process dies after step 3 and
before the receipt. Receipt replay verifies both artifacts and never invokes
the model for a matching commit.

### Rollback and indeterminate state

There is no destructive rollback after the note linearization point. A failed
temporary write, rename, or fsync leaves the prior final revision in place and
the request retryable. Once the new marker is durable, the marker/body pair is
the committed revision; a missing receipt is reconstructed from it. A marker
or receipt that is malformed, contradictory, foreign, unreadable, or paired
with different bytes is indeterminate and is never overwritten automatically.
It remains queued for bounded retry/manual reconciliation rather than asking
the model to make a guess or deleting user memory. A later request may replace
a valid prior revision only after validating its session/scope/path/body
integrity and publishing a new marker; it cannot use rollback to erase an
unrelated or foreign revision.

The rebuildable daily index is refreshed only after the note commit and before
the receipt. A failure there is not acknowledged and leaves the receipt
absent; recovery revalidates the note and may rebuild the index without model
replay. The index is not authoritative evidence.

## Request, marker, and receipt state machine

For a request with a valid `request_id`, the application takes the per-request
lock, then:

* rejects an existing receipt whose fingerprint differs;
* for a matching receipt, validates the marker, path, scope, source hash,
  body digest, and exact note bytes before replaying the stored response;
* before inference, searches for the unique matching marker. A matching,
  digest-valid marker reconstructs the receipt and acknowledges without LLM
  work; a contradictory, duplicate, malformed, or unreadable candidate fails
  closed;
* otherwise calls the AgentScope model for one proposal, validates it, commits
  one revision, refreshes the index, and durably writes the receipt.

The marker is not sufficient by itself when any bound value or digest differs.
The following are always retry-blocking failures: ambiguous or multiple model
mutations, invalid JSON/schema, tool errors or unknown tool calls, contradictory
markers, unreadable notes, symlink components or symlink swaps, paths outside
the workspace or allowed scope, foreign sessions/profiles, stale compare-and-
swap bytes, `os.replace`/`fsync`/permission/disk failures, and partial model
results. No success receipt or Hermes ACK may be returned for those states.

## Crash windows

* Before note commit: no authoritative note marker exists. A retry may call
  the model once and commit one revision.
* After note commit and directory fsync, before receipt: recovery validates the
  marker/body and creates the receipt; the model is not called again.
* After receipt and before Hermes ACK/spool removal: receipt replay validates
  the note and returns the stored response; no note mutation or model call is
  performed.
* During any temporary write, rename, or fsync failure: the old final note and
  queued request remain authoritative. A temporary file is not evidence of a
  commit and success is not returned.

## Whole job versus `auto_tag`

The protected Stage 1 job is deliberately note-only: one note commit, index
refresh, and receipt. `auto_tag` is a separate mutating effect and is disabled
in the dedicated Option B Stage 1 configuration. It is not included in the
marker or receipt, and a note-only receipt must never imply that tagging ran.
Re-enabling it requires its own deterministic transaction, marker scope, crash
tests, and ACK boundary. Legacy/default jobs retain their existing step
ordering and behavior.

Session transcript persistence remains source input preparation, not part of
the note commit. Its trusted hash is captured after the request messages are
persisted and is bound into the marker. FHM-006 pre-compression v2 remains
disabled and is outside this transaction.

## Threat boundaries and residual risk

This design addresses duplicate model mutations across the three local
durability boundaries and prevents model-controlled authoritative writes. It
does not provide distributed exactly-once semantics, protect a compromised
filesystem/root user, authenticate semantic truth, make arbitrary legacy
`daily_write` calls transactional, or make HTTP/non-AgentScope/plugin variants
Option B. Those paths remain unchanged and are not covered by this contract.

Qualification must separately exercise deterministic SIGKILL before note,
after note/before receipt, and after receipt/before ACK; same/different request
IDs; receipt/body/marker tampering; multi-write and partial-result rejection;
symlink and foreign-scope rejection; injected rename/fsync/permission/disk
failures; BM25 retrieval; and the unchanged legacy path. Unit tests using a
synthetic model do not qualify live Qwen or hosted CI.

## This worktree validation record

As of 2026-10-27, the host qualification evidence supplied for this isolated
candidate is:

* 147/147 focused host regressions, including protected missing-ID/scope tests — **PASS**, log:
  `/tmp/fhm007/optionb-p1-final-focused-20261027.log`.
* 3/3 deterministic SIGKILL crash gates with portable child environment — **PASS**, log:
  `/tmp/fhm007/optionb-p1-final-sigkill-20261027.log`.
* 16/16 filesystem fsync/digest checks — **PASS**, log:
  `/tmp/fhm007/optionb-supervisor-atomic-digest-tests.log`.
* One protected embedded synthetic note-plus-receipt run and sequential A→B
  revision run — **PASS**.
* On Dad-PC, live Qwen 3.5 4B wrote the note and durable receipt, and the
  pending queue became empty. An immediate post-restart lookup was empty; a
  subsequent restarted BM25 lookup of the fact — **PASS**. The immediate
  restart lookup is not claimed as a passing gate because readiness/index
  timing tests are still required.
* The local experimental live configuration used `allow_custom_config=true`
  to reach `10.1.1.50`; it is not default Stage 1 guard validation.
* The independent Cursor review gave a conditional **GO** for a draft stacked
  PR only, log: `/tmp/fhm007/optionb-cursor-retry-short.log`.

The preceding record is historical (the original 147-test candidate). It is
superseded by the following validation; no merge, deployment, or production
approval is implied.

## Integrated candidate validation — 2026-10-27

* **163/163 combined host regressions PASS** after ownership, durability,
  empty-turn and retrieval changes:
  `/srv/project-coordinator/tmp/fhm007-tests-20261027/merged/full-final.log`.
* **3/3 SIGKILL crash windows PASS** on the combined candidate with real Hermes:
  `/srv/project-coordinator/tmp/fhm007-tests-20261027/merged/sigkill-final.log`.
* Black 120, Ruff F/I, pinned Pylint 10.00/10 and diff integrity PASS:
  `/srv/project-coordinator/tmp/fhm007-tests-20261027/merged/pylint-r2.log`.
* Three fresh, isolated live Qwen 3.5 4B runs PASS: each produced a durable
  synthetic note and receipt, emptied the Hermes write queue, restarted the
  provider and retrieved the correct synthetic codephrase and case colour
  on the **first** BM25 lookup. The three `merged/live-proof*.log` files
  are in the same test scratch directory. Explicit custom configuration
  reached LAN LM Studio; these runs do not qualify the default profile.
* The connected Cursor GitHub review of the older PR head `2f640f6`
  identified unmarked foreign-note ownership, missing directory-entry
  durability and an empty-turn receipt error. The integrated candidate
  addresses all three, but requires **new exact-head GitHub review and CI**.

This qualification is Linux/ext4 scoped. Broader supported-filesystem,
concurrency, fault and deployment acceptance remains outstanding.
FHM-006 pre-compression checkpoint v2 is separately unapproved and disabled.
**No merge or deployment is authorised.**
