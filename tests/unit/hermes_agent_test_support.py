"""Shared fakes for Hermes provider unit tests."""


class FakeBackend:
    """Minimal backend with the operations exercised by provider tests."""

    label = "fake"

    def __init__(self):
        """Initialise isolated write and lifecycle records."""
        self.writes = []
        self.request_ids = []
        self.closed = False

    def start(self):
        """Start the fake backend without side effects."""
        return None

    def health(self, *, timeout):
        """Report an available fake backend."""
        del timeout
        return {"success": True, "metadata": {"health": {"healthy": True}}}

    def search(self, query, *, limit, timeout):
        """Return a fixed recall response."""
        del query, limit, timeout
        return {
            "success": True,
            "answer": " remembered ",
            "metadata": {"counts": {"returned": 2}},
        }

    def auto_memory(self, session_id, messages, *, timeout, request_id=None):
        """Record an idempotent write request for assertions."""
        del timeout
        self.writes.append((session_id, messages))
        self.request_ids.append(request_id)
        return {"success": True}

    def close(self, *, timeout):
        """Record that the backend was closed."""
        del timeout
        self.closed = True
