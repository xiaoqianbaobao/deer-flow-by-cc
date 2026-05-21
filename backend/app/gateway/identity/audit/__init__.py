"""Audit pipeline (M6): event dataclass, batch writer, middleware, API.

All components are only instantiated when ``ENABLE_IDENTITY=true``; with the
flag off nothing in this package is imported by the gateway lifespan.
"""

from app.gateway.identity.audit.events import (
    KEY_CRITICAL_ACTIONS,
    KNOWN_ACTIONS,
    AuditEvent,
)

__all__ = ["AuditEvent", "KEY_CRITICAL_ACTIONS", "KNOWN_ACTIONS"]
