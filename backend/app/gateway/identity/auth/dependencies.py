"""FastAPI dependencies for reading the current ``Identity``.

These work in concert with ``IdentityMiddleware``: the middleware attaches
``request.state.identity`` to every request; these helpers expose it to
route handlers and raise 401 when a route *requires* authentication.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from app.gateway.identity.auth.identity import Identity


def get_current_identity(request: Request) -> Identity:
    """Always returns an Identity — may be anonymous."""
    ident = getattr(request.state, "identity", None)
    if ident is None:
        return Identity.anonymous()
    return ident


def require_authenticated(request: Request) -> Identity:
    """Raise 401 when the request is anonymous."""
    ident = get_current_identity(request)
    if not ident.is_authenticated:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
    return ident
