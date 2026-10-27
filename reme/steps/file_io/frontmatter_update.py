"""``frontmatter_update_step`` — set frontmatter keys on a markdown file.

Read-modify-write the YAML frontmatter; body content is untouched.
The watcher / parser pick up the change asynchronously.

Input shape: ``frontmatter_update_step path=foo.md metadata={"x": "y", "z": "w"}``
— ``metadata`` is an explicit dict whose entries are merged into the
file's frontmatter (existing keys overwritten, missing keys inserted).

``path`` is a path relative to the workspace. Non-markdown targets return
``error="not markdown"``. An empty or missing ``metadata`` returns
``error="no fields to update"``.
"""

from pathlib import Path

import frontmatter

from ._file_io import get_path_lock, write_file_safe
from ._path import _check_path_permission, display_path, gate_md, resolve_path
from ._provenance import (
    PROVENANCE_SESSION_KEY,
    ProvenanceError,
    note_owned_by_session,
    strip_model_provenance,
    trusted_provenance,
)
from ..base_step import BaseStep
from ...components import R


@R.register("frontmatter_update_step")
class FrontmatterUpdateStep(BaseStep):
    """Set frontmatter keys on a markdown file from a ``metadata`` dict.

    Permission: honors the request-scoped ``_allowed_paths`` constraint
    injected by the server into the RuntimeContext;
    without it, restricting read/edit/write alone would still leave
    frontmatter of arbitrary workspace Markdown files mutable.

    ``_allowed_frontmatter_keys`` optionally limits updates to an injected
    list of top-level keys.  Omitting it (or setting it to ``None``) preserves
    the unrestricted historical behavior.
    """

    async def execute(self):
        assert self.context is not None
        path: str = self.context.get("path") or ""
        assert path, "path is required"
        provenance_session_id = self.context.get(PROVENANCE_SESSION_KEY)
        raw_metadata = self.context.get("metadata") or {}
        metadata = strip_model_provenance(raw_metadata) if provenance_session_id is not None else raw_metadata
        assert isinstance(metadata, dict), "metadata must be a dict"

        allowed_keys = self.context.get("_allowed_frontmatter_keys")
        key_error: str | None = None
        if allowed_keys is not None:
            if not isinstance(allowed_keys, list) or any(not isinstance(key, str) for key in allowed_keys):
                key_error = "_allowed_frontmatter_keys must be a list of strings"
            else:
                allowed = set(allowed_keys)
                denied = [key for key in metadata if not isinstance(key, str) or key not in allowed]
                if denied:
                    names = ", ".join(sorted(repr(key) for key in denied))
                    key_error = f"frontmatter key(s) not allowed: {names}"

        workspace_dir = Path(self.file_store.workspace_path or ".").resolve()
        target, err = resolve_path(workspace_dir, path)
        if err or target is None:
            payload: dict = {"path": path, "error": err or "invalid path"}
        else:
            original_target = target
            target, is_md = gate_md(target)
            if not _check_path_permission(workspace_dir, target, self.context.get("_allowed_paths")):
                payload = {"path": path, "error": "no permission to update this file"}
            else:
                lock = await get_path_lock(target)
                async with lock:
                    if not target.is_file():
                        payload = {"path": path, "error": f"{display_path(workspace_dir, target)} not found"}
                    elif not is_md:
                        payload = {"path": path, "error": "not markdown"}
                    elif not metadata and provenance_session_id is None:
                        payload = {"path": path, "error": "no fields to update"}
                    elif key_error:
                        payload = {"path": path, "error": key_error}
                    else:
                        payload = {}
                        if provenance_session_id is not None:
                            try:
                                metadata.update(
                                    trusted_provenance(
                                        workspace_dir,
                                        str(self.config_value("session_dir")),
                                        str(provenance_session_id),
                                    ),
                                )
                            except ProvenanceError as exc:
                                payload = {"path": path, "error": f"source provenance unavailable: {exc}"}
                        if "error" not in payload:
                            post = frontmatter.loads(target.read_text(encoding="utf-8"))
                            if provenance_session_id is not None and not note_owned_by_session(post.metadata, metadata):
                                payload = {"path": path, "error": "existing note belongs to a different source session"}
                            else:
                                post.metadata.update(metadata)
                                await write_file_safe(target, frontmatter.dumps(post))
                                payload = {"path": path, "updated": metadata}
            if target != original_target:
                payload["resolved_path"] = display_path(workspace_dir, target)

        if "error" in payload:
            self.context.response.success = False
            self.context.response.answer = f"Error: {payload['error']}"
            self.logger.info(f"[{self.name}] update failed path={path} error={payload['error']!r}")
        else:
            self.context.response.success = True
            self.context.response.answer = f"Updated {len(metadata)} key(s) on {path}"
            self.logger.info(f"[{self.name}] path={path} keys={list(metadata.keys())}")
        self.context.response.metadata.update(payload)
