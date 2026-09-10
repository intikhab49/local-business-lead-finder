"""Backwards-compatible alias for the multi-agent coordinator.

The legacy `LeadAgent` is now implemented by `CoordinatorAgent`. This module
re-exports it so existing imports, endpoints, and tests keep working unchanged.
"""

from app.agents.coordinator_agent import CoordinatorAgent as LeadAgent

__all__ = ["LeadAgent"]
