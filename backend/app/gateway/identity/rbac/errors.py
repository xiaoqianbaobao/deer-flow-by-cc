"""Errors raised by RBAC enforcement."""

from __future__ import annotations


class PermissionDeniedError(Exception):
    """Raised when an authenticated identity is forbidden from an action.

    Routes convert this into HTTP 403; direct calls (e.g. tenant-scope
    INSERT guard) can catch it to abort a transaction.
    """

    def __init__(self, message: str = "permission denied", *, tag: str | None = None):
        super().__init__(message)
        self.tag = tag
