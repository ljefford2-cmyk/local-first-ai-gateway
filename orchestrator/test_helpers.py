"""Shared test utilities for DRNT orchestrator tests."""

from __future__ import annotations


class MockAuditClient:
    """Mock audit client for testing. Captures emitted events."""

    def __init__(self):
        self.events: list[dict] = []

    async def emit_durable(self, event: dict) -> bool:
        self.events.append(event)
        return True

    async def emit_best_effort(self, event: dict) -> None:
        self.events.append(event)

    def get_events_by_type(self, event_type: str) -> list[dict]:
        """Convenience: filter captured events by type."""
        return [e for e in self.events if e.get("event_type") == event_type]

    def clear(self):
        """Reset captured events."""
        self.events.clear()
