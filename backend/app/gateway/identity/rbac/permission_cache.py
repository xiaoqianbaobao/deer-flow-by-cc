"""Redis-backed cache for the permission set of API-token callers.

**Why it exists:** JWT access tokens carry permissions in their claims,
so the middleware never needs to flatten for JWT callers — the set is
valid until ``exp``. API tokens (``dft_*``) are verified every request
against the database; without a cache we'd re-run the permission
flattening join on every hit. Spec §6.5 requires a 300s cache.

**Keys:**
- ``{prefix}:perms:{user_id}:{tenant_id|"platform"}`` — set of tags
  (stored as newline-separated string for simplicity).
- ``{prefix}:perms:stale_users`` — set of user ids whose cached
  permissions are known to be stale (triggered by admin role edits).

**Invalidation model:** producers (role edits, membership changes) call
``invalidate`` for the affected (user, tenant) or ``mark_stale`` for the
user; on the next request the middleware notices the stale flag,
forces a fresh flatten, writes a new cache entry, and clears the flag.

M6 wires producers + audit events; M3 provides the primitives.
"""

from __future__ import annotations

from collections.abc import Iterable

DEFAULT_TTL_SEC = 300
_PLATFORM_BUCKET = "platform"
_STALE_SET_SUFFIX = "perms:stale_users"


class PermissionCache:
    """Thin async wrapper over a Redis client."""

    def __init__(self, redis_client, *, key_prefix: str = "identity"):
        self._redis = redis_client
        self._prefix = key_prefix

    # --- key helpers --------------------------------------------------

    def _perm_key(self, user_id: int, tenant_id: int | None) -> str:
        bucket = str(tenant_id) if tenant_id is not None else _PLATFORM_BUCKET
        return f"{self._prefix}:perms:{user_id}:{bucket}"

    def _perm_scan_prefix(self, user_id: int) -> str:
        return f"{self._prefix}:perms:{user_id}:*"

    def _stale_set_key(self) -> str:
        return f"{self._prefix}:{_STALE_SET_SUFFIX}"

    # --- permission cache --------------------------------------------

    async def get(self, user_id: int, tenant_id: int | None) -> set[str] | None:
        """Return the cached permission set or ``None`` on miss."""
        raw = await self._redis.get(self._perm_key(user_id, tenant_id))
        if raw is None:
            return None
        if raw == "":
            return set()
        return set(raw.split("\n"))

    async def set(
        self,
        user_id: int,
        tenant_id: int | None,
        perms: Iterable[str],
        *,
        ttl_sec: int = DEFAULT_TTL_SEC,
    ) -> None:
        """Cache ``perms`` for ``(user_id, tenant_id)`` for ``ttl_sec``."""
        payload = "\n".join(sorted(set(perms)))
        await self._redis.set(self._perm_key(user_id, tenant_id), payload, ex=ttl_sec)

    async def invalidate(self, user_id: int, *, tenant_id: int | None = None) -> None:
        """Clear cache for one (user, tenant) or every tenant bucket of a user."""
        if tenant_id is not None:
            await self._redis.delete(self._perm_key(user_id, tenant_id))
            return
        # Clear every bucket for this user.
        pattern = self._perm_scan_prefix(user_id)
        async for key in self._redis.scan_iter(pattern):
            await self._redis.delete(key)

    # --- stale-user signalling ---------------------------------------

    async def mark_stale(self, user_id: int) -> None:
        """Mark ``user_id``'s cached permissions as stale.

        The next request from that user triggers a fresh flatten; the
        response carries ``X-Deerflow-Session-Stale: 1`` so the UI can
        prompt a reload.
        """
        await self._redis.sadd(self._stale_set_key(), str(user_id))

    async def is_stale(self, user_id: int) -> bool:
        return bool(await self._redis.sismember(self._stale_set_key(), str(user_id)))

    async def clear_stale(self, user_id: int) -> None:
        await self._redis.srem(self._stale_set_key(), str(user_id))
