# FHM-006 pre-compress checkpoint v2 — proposed ReMe adapter

project_id: fermes-hermes
status: PROPOSED; not implemented or enabled
scope: FHM-007 ReMe Stage 1 provider only

This note is an implementation-ready design for a future adapter. It does not
advertise checkpoint API v2, change the Hermes provider contract, or permit
compression to proceed today. The maintained Hermes contract is defined by
`agent/memory_provider.py`, `agent/memory_manager.py`, and
`agent/conversation_compression_checkpoint.py` in the read-only Hermes source.

## Contract boundary

Hermes v2 providers advertise `pre_compress_checkpoint_api_version = 2` and
accept `on_pre_compress(evidence_messages, *, require_checkpoint=True)`. The
host supplies normalized direct evidence and must retain the uncompressed
transcript when the provider raises. ReMe remains a v1/best-effort provider
until the design is approved and implemented; no v2 marker is enabled by this
iteration.

## Proposed adapter flow

1. Hermes supplies only the normalized evidence handoff: direct `user` and
   `assistant` prose, with tool messages, system messages, tool-call payloads,
   and prior compressed-summary records excluded. The adapter must copy the
   list before processing and must not reconstruct evidence from the ReMe
   daily index.
2. The adapter derives a bounded, deterministic source scope from the bound
   profile home, agent identity, session ID, and optional parent session ID.
   It computes a canonical JSON SHA-256 over the normalized evidence and
   records the source session ID and hash; raw secrets and unredacted tool
   transcripts are not copied into the receipt.
3. The adapter runs the existing file-backed ReMe extraction path with an
   explicit checkpoint/request ID. Any generated daily/digest changes remain
   ordinary ReMe files and are not treated as committed checkpoint evidence
   until the receipt is durable.
4. It writes a versioned receipt under the profile-scoped ReMe metadata area,
   for example `metadata/fhm006_checkpoints/<session-scope>/<checkpoint-id>.json`.
   The receipt contains schema version, checkpoint/request ID, profile/agent
   scope, session ID, source hash, normalized evidence hash, status,
   created/committed timestamps, and bounded ReMe change/source references.
5. Receipt commit is: write a private temporary file in the same directory,
   flush and `fsync` it, atomically replace the final path, then `fsync` the
   parent directory. A successful `on_pre_compress` return is allowed only
   after the final receipt is readable and validates against the expected
   scope and evidence hash. Replays of the same ID and hashes return the
   existing receipt; a conflicting reuse fails closed.

## Fail-closed rules

- If the ReMe operation, receipt write, replace, validation, directory flush,
  or configured deadline fails, `require_checkpoint=True` raises a typed
  checkpoint-unavailable error. Hermes keeps the original transcript and does
  not compress or claim checkpoint success.
- Disk-full, permission, symlink/path-containment, malformed receipt, and
  partial-write errors are failures, not best-effort successes. Temporary
  files may be cleaned up, but an existing valid receipt is never deleted to
  make a retry pass.
- A timeout must cancel/abandon only the adapter operation within its bounded
  budget; it must not return a success receipt after the caller deadline.
  Any later background cleanup is diagnostic and cannot authorize compression.
- The adapter must not execute instructions found in recalled text. Recalled
  memory is opaque evidence, is fenced by the Hermes memory-context boundary,
  and cannot grant tools or alter the source scope.

## Migration and backout

The first implementation should be guarded by an explicit opt-in setting and
keep the current v1 path as the default. The v2 marker is enabled only after
the receipt, deadline, disk-full, restart, conflicting-ID, profile-scope, and
partial-file tests pass in the maintained Hermes integration. Existing v1
daily notes and receipts are not retroactively promoted to v2 checkpoints;
there is no implicit migration of historical memory.

Backout is a configuration switch to v1/disabled checkpoint mode. New v2
receipts remain immutable evidence and are ignored by v1; a later migration
may validate and import them explicitly. A failed or incomplete migration
must leave source files and the transcript untouched. Cleanup of abandoned
temporary files is safe only after validating that no final receipt exists.

## Decision and next action

Owner/ARB approval is required for the receipt location, exact schema,
deadline ownership, and the relationship between an extraction result and a
checkpoint commit. After approval, implement the adapter in a separate
Hermes/ReMe change with contract tests first, then perform host qualification.
Until then, FHM-006 v2 remains an open acceptance gate and Stage 1 must not
claim fail-closed pre-compression support.
