"""Profile-local configuration for the Hermes ReMe memory provider."""

from __future__ import annotations

import json
import math
import os
import tempfile

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .client import normalize_http_endpoint

CONFIG_DIRECTORY = "reme"
CONFIG_FILENAME = "config.json"
LEGACY_CONFIG_FILENAME = "reme.json"
VALID_MODES = {"http", "embedded"}
STAGE1_CONFIG_NAME = "fermes-hermes-stage1"
_STAGE1_JOBS = {
    "index_update_loop",
    "health_check",
    "search",
    "auto_memory",
    "daily_list",
    "daily_write",
    "read",
    "write",
    "edit",
    "move",
    "frontmatter_read",
    "frontmatter_update",
    "list_tags",
}
_STAGE1_STEPS = {
    "health_check": ("health_check_step",),
    "search": ("search_step",),
    "auto_memory": ("auto_memory_step", "auto_tag_step"),
    "daily_list": ("daily_list_step",),
    "daily_write": ("daily_write_step",),
    "read": ("read_step",),
    "write": ("write_step",),
    "edit": ("edit_step",),
    "move": ("move_step",),
    "frontmatter_read": ("frontmatter_read_step",),
    "frontmatter_update": ("frontmatter_update_step",),
    "list_tags": ("list_tags_step",),
}
_STAGE1_COMPONENTS = {
    "tokenizer": {"default"},
    "as_llm": {"default"},
    "agent_wrapper": {"default"},
    "file_graph": {"default"},
    "file_chunker": {"markdown", "default"},
    "keyword_index": {"default"},
    "tag_index": {"default"},
    "file_store": {"default"},
}


class ReMeConfigError(ValueError):
    """Raised when provider configuration is invalid."""


@dataclass(frozen=True)
class ReMeConfig:
    """Validated settings shared by the provider and both backends."""

    mode: str = "http"
    endpoint: str = "http://127.0.0.1:2333"
    workspace_dir: str = ""
    reme_config: str = STAGE1_CONFIG_NAME
    allow_custom_config: bool = False
    request_timeout: float = 600.0
    recall_timeout: float = 5.0
    health_timeout: float = 2.0
    health_retry_seconds: float = 30.0
    shutdown_timeout: float = 30.0
    recall_limit: int = 5


def config_path(hermes_home: str | Path) -> Path:
    """Return the path used by Hermes' generic provider configuration UI."""
    return Path(hermes_home).expanduser() / CONFIG_DIRECTORY / CONFIG_FILENAME


def legacy_config_path(hermes_home: str | Path) -> Path:
    """Return the pre-dashboard configuration path retained for compatibility."""
    return Path(hermes_home).expanduser() / LEGACY_CONFIG_FILENAME


def _default_hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReMeConfigError(
            f"Unable to read ReMe provider config {path}: {exc}",
        ) from exc
    if not isinstance(loaded, dict):
        raise ReMeConfigError(
            f"ReMe provider config must contain a JSON object: {path}",
        )
    return loaded


def _read_config_values(home: Path) -> dict[str, Any]:
    """Merge legacy values under the current Dashboard-managed config."""
    values = _read_json_object(legacy_config_path(home))
    values.update(_read_json_object(config_path(home)))
    return values


def _positive_float(value: Any, key: str, default: float) -> float:
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ReMeConfigError(f"'{key}' must be a positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise ReMeConfigError(f"'{key}' must be a positive number")
    return number


def _positive_int(value: Any, key: str, default: int) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ReMeConfigError(f"'{key}' must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ReMeConfigError(f"'{key}' must be a positive integer") from exc
    if number <= 0 or (isinstance(value, float) and not value.is_integer()):
        raise ReMeConfigError(f"'{key}' must be a positive integer")
    return number


def _boolean(value: Any, key: str, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ReMeConfigError(f"'{key}' must be a boolean")


def _is_parent_path(path: Path) -> bool:
    return ".." in path.parts


def _normalize_workspace(raw_workspace: str) -> str:
    """Normalize a workspace without accepting traversal or symlinked roots."""
    candidate = Path(raw_workspace).expanduser()
    if _is_parent_path(candidate):
        raise ReMeConfigError("'workspace_dir' must not contain path traversal")
    absolute = Path(os.path.abspath(candidate))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            if current.is_symlink():
                raise ReMeConfigError("'workspace_dir' must not contain symlinks")
        except OSError as exc:
            raise ReMeConfigError("'workspace_dir' could not be inspected safely") from exc
    normalized = absolute.resolve(strict=False)
    if not normalized.name:
        raise ReMeConfigError("'workspace_dir' must have a non-empty name")
    return str(normalized)


def _stage1_path() -> Path | None:
    try:
        from reme.config import config_parser

        return config_parser._CONFIG_REGISTRY.get(STAGE1_CONFIG_NAME)  # pylint: disable=protected-access
    except (ImportError, AttributeError):
        return None


def _is_stage1_config(value: str) -> bool:
    if value == STAGE1_CONFIG_NAME:
        return True
    path = Path(value).expanduser()
    if path.suffix not in {".yaml", ".yml", ".json"} or _is_parent_path(path):
        return False
    stage_path = _stage1_path()
    if stage_path is None:
        return False
    try:
        return path.resolve(strict=True) == stage_path.resolve(strict=True)
    except OSError:
        return False


def _load_embedded_config(value: str) -> dict[str, Any]:
    try:
        from reme.config.config_parser import resolve_app_config

        loaded = resolve_app_config(log_config=False, config=value)
    except Exception as exc:  # Config parsing is intentionally fail-closed at this boundary.
        raise ReMeConfigError("Unable to validate the embedded Stage 1 ReMe configuration") from exc
    if not isinstance(loaded, dict):
        raise ReMeConfigError("Embedded Stage 1 ReMe configuration must be an object")
    return loaded


def _validate_stage1_config(value: str) -> None:
    """Reject changes that would turn the embedded provider into a service or scheduler."""
    loaded = _load_embedded_config(value)
    if set(loaded) - {"service", "jobs", "components"}:
        raise ReMeConfigError("Embedded Hermes requires the local-only Stage 1 configuration")
    if loaded.get("service") != {"backend": "cli"}:
        raise ReMeConfigError("Embedded Hermes Stage 1 must use the local CLI service")

    jobs = loaded.get("jobs")
    if not isinstance(jobs, dict) or set(jobs) != _STAGE1_JOBS:
        raise ReMeConfigError("Embedded Hermes Stage 1 has an unapproved job set")
    for name, job in jobs.items():
        if not isinstance(job, dict):
            raise ReMeConfigError("Embedded Hermes Stage 1 contains an invalid job")
        if name == "index_update_loop":
            if job.get("backend") != "background":
                raise ReMeConfigError("Embedded Hermes Stage 1 cannot run an unexpected scheduler")
        elif job.get("backend") != "base" or job.get("enable_serve") is not False:
            raise ReMeConfigError("Embedded Hermes Stage 1 jobs must be local and unserved")
        if any(token in name.lower() for token in ("dream", "proactive", "cron")):
            raise ReMeConfigError("Embedded Hermes Stage 1 cannot enable Dream or proactive scheduling")
        steps = job.get("steps")
        if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
            raise ReMeConfigError("Embedded Hermes Stage 1 contains invalid job steps")
        if name == "index_update_loop":
            if [step.get("backend") for step in steps] != ["init_changes_step", "watch_changes_step"]:
                raise ReMeConfigError("Embedded Hermes Stage 1 contains unapproved background steps")
            dispatches = [step.get("dispatch_steps") for step in steps]
            if dispatches[0] != ["update_index_step"] or not isinstance(dispatches[1], list):
                raise ReMeConfigError("Embedded Hermes Stage 1 contains unapproved index dispatch")
            if [item.get("backend") for item in dispatches[1] if isinstance(item, dict)] != ["update_index_step"]:
                raise ReMeConfigError("Embedded Hermes Stage 1 contains unapproved index dispatch")
            if len(dispatches[1]) != 1 or not isinstance(dispatches[1][0], dict):
                raise ReMeConfigError("Embedded Hermes Stage 1 contains unapproved index dispatch")
        elif tuple(step.get("backend") for step in steps) != _STAGE1_STEPS[name]:
            raise ReMeConfigError("Embedded Hermes Stage 1 contains unapproved job steps")

    components = loaded.get("components")
    if not isinstance(components, dict) or set(components) != set(_STAGE1_COMPONENTS):
        raise ReMeConfigError("Embedded Hermes Stage 1 has an unapproved component set")
    for component_type, names in _STAGE1_COMPONENTS.items():
        group = components.get(component_type)
        if not isinstance(group, dict) or set(group) != names:
            raise ReMeConfigError("Embedded Hermes Stage 1 has an unapproved component set")

    credential = components["as_llm"]["default"].get("credential", {})
    base_url = credential.get("base_url") if isinstance(credential, dict) else None
    parsed = urlsplit(str(base_url or ""))
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ReMeConfigError("Embedded Hermes Stage 1 may use only a loopback model endpoint")


def _validate_embedded_policy(value: str, *, allow_custom: bool) -> None:
    if allow_custom:
        return
    if not _is_stage1_config(value):
        raise ReMeConfigError(
            "Embedded Hermes requires reme_config='fermes-hermes-stage1'; "
            "set allow_custom_config=true only for an explicit advanced override",
        )
    _validate_stage1_config(value)


def parse_config(values: dict[str, Any], *, hermes_home: str | Path) -> ReMeConfig:
    """Validate and normalize an already loaded configuration mapping."""
    del hermes_home  # Reserved for future profile-relative settings.
    defaults = ReMeConfig()
    mode = str(values.get("mode", defaults.mode) or defaults.mode).strip().lower()
    if mode not in VALID_MODES:
        raise ReMeConfigError("'mode' must be either 'http' or 'embedded'")

    endpoint = str(values.get("endpoint", defaults.endpoint) or "").strip().rstrip("/")
    if mode == "http":
        try:
            endpoint = normalize_http_endpoint(endpoint)
        except ValueError as exc:
            raise ReMeConfigError(
                "ReMe endpoint must be an absolute http(s) URL",
            ) from exc

    raw_workspace = str(
        values.get("workspace_dir", defaults.workspace_dir) or "",
    ).strip()
    if mode == "embedded" and not raw_workspace:
        raise ReMeConfigError("'workspace_dir' is required in embedded mode")
    workspace_dir = _normalize_workspace(raw_workspace) if raw_workspace else ""

    reme_config = str(values.get("reme_config", defaults.reme_config) or "").strip()
    if mode == "embedded" and not reme_config:
        raise ReMeConfigError("'reme_config' cannot be empty in embedded mode")
    allow_custom_config = _boolean(
        values.get("allow_custom_config"),
        "allow_custom_config",
        defaults.allow_custom_config,
    )
    if mode == "embedded":
        _validate_embedded_policy(reme_config, allow_custom=allow_custom_config)

    return ReMeConfig(
        mode=mode,
        endpoint=endpoint or defaults.endpoint,
        workspace_dir=workspace_dir,
        reme_config=reme_config or defaults.reme_config,
        allow_custom_config=allow_custom_config,
        request_timeout=_positive_float(
            values.get("request_timeout"),
            "request_timeout",
            defaults.request_timeout,
        ),
        recall_timeout=_positive_float(
            values.get("recall_timeout"),
            "recall_timeout",
            defaults.recall_timeout,
        ),
        health_timeout=_positive_float(
            values.get("health_timeout"),
            "health_timeout",
            defaults.health_timeout,
        ),
        health_retry_seconds=_positive_float(
            values.get("health_retry_seconds"),
            "health_retry_seconds",
            defaults.health_retry_seconds,
        ),
        shutdown_timeout=_positive_float(
            values.get("shutdown_timeout"),
            "shutdown_timeout",
            defaults.shutdown_timeout,
        ),
        recall_limit=_positive_int(
            values.get("recall_limit"),
            "recall_limit",
            defaults.recall_limit,
        ),
    )


def load_config(hermes_home: str | Path | None = None) -> ReMeConfig:
    """Load current config, inheriting omitted fields from the legacy file."""
    home = Path(hermes_home).expanduser() if hermes_home is not None else _default_hermes_home()
    return parse_config(_read_config_values(home), hermes_home=home)


def save_config(values: dict[str, Any], hermes_home: str | Path) -> ReMeConfig:
    """Validate and atomically write config in the current Hermes layout."""
    home = Path(hermes_home).expanduser()
    current = config_path(home)
    merged = _read_config_values(home)
    merged.update(
        {key: value for key, value in dict(values or {}).items() if value is not None},
    )
    validated = parse_config(merged, hermes_home=home)

    current.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{current.name}.", dir=current.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                asdict(validated),
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, current)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return validated
