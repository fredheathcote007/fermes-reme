"""HTTP implementation of the ReMe backend contract."""

from __future__ import annotations

from typing import Any

from .backend import ReMeBackendError, require_healthy
from .client import ReMeHttpClient, ReMeServiceError


class HttpReMeBackend:
    """Call an independently managed ReMe action service."""

    def __init__(self, endpoint: str, *, request_timeout: float) -> None:
        self._client = ReMeHttpClient(endpoint, timeout=request_timeout)
        self.label = self._client.endpoint

    def start(self, *, deadline: float | None = None) -> None:
        """HTTP service lifecycle is managed outside Hermes."""
        del deadline

    def _call(
        self,
        action: str,
        payload: dict[str, Any] | None,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        try:
            return self._client.call(action, payload, timeout=timeout)
        except (ReMeServiceError, ValueError) as exc:
            raise ReMeBackendError(
                str(exc),
                refusal_code=getattr(exc, "refusal_code", None),
                refusal_reason=getattr(exc, "refusal_reason", None),
            ) from exc

    def health(self, *, timeout: float) -> dict[str, Any]:
        """Call the action service health check."""
        return require_healthy(self._call("health_check", None, timeout=timeout))

    def search(self, query: str, *, limit: int, timeout: float) -> dict[str, Any]:
        """Search through the HTTP action service."""
        return self._call("search", {"query": query, "limit": limit}, timeout=timeout)

    def auto_memory(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        timeout: float,
        request_id: str | None = None,
        memory_hint: str | None = None,
        noop_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Submit a completed turn through the HTTP action service."""
        if isinstance(noop_policy, dict) and noop_policy.get("policy") == "application_authorized_noop":
            raise ReMeBackendError("HTTP backend cannot authenticate protected no-op authority")
        payload: dict[str, Any] = {"session_id": session_id, "messages": messages}
        if request_id is not None:
            payload["request_id"] = request_id
        if memory_hint is not None:
            payload["memory_hint"] = memory_hint
        if noop_policy is not None:
            payload["noop_policy"] = noop_policy
        return self._call(
            "auto_memory",
            payload,
            timeout=timeout,
        )

    def close(self, *, timeout: float) -> None:
        """Release no resources because urllib calls are request-scoped."""
        del timeout
