"""Redis-backed login-failure rate limiter.

Two keys per (ip, email) pair:

- ``{prefix}:login_fail:{ip}:{email}`` — monotonic counter. INCR on each
  failure; EXPIRE to ``window_sec`` on the first failure only. Once the
  window passes with no further failures, the counter self-destructs so
  the next attempt starts fresh.
- ``{prefix}:login_block:{ip}:{email}`` — block flag. SET with
  EXPIRE(``block_sec``) once the counter reaches ``max_attempts``.

Different (ip, email) pairs keep independent counters — blocking one does
not affect another. Pass ``ip=None`` when the caller doesn't know the IP
(e.g. server-to-server flows) and wants to key on email only.
"""

from __future__ import annotations


class LoginLockout:
    def __init__(
        self,
        redis_client,
        *,
        max_attempts: int,
        window_sec: int,
        block_sec: int,
        key_prefix: str = "deerflow",
    ):
        self._redis = redis_client
        self._max = max_attempts
        self._window = window_sec
        self._block = block_sec
        self._prefix = key_prefix

    def _fail_key(self, ip: str | None, email: str) -> str:
        return f"{self._prefix}:login_fail:{ip or '_'}:{email}"

    def _block_key(self, ip: str | None, email: str) -> str:
        return f"{self._prefix}:login_block:{ip or '_'}:{email}"

    async def record_failure(self, *, ip: str | None, email: str) -> bool:
        """Increment the counter; return True if this call triggers a block."""
        fk = self._fail_key(ip, email)
        count = await self._redis.incr(fk)
        if count == 1:
            await self._redis.expire(fk, self._window)
        if count >= self._max:
            bk = self._block_key(ip, email)
            await self._redis.set(bk, "1", ex=self._block)
            return True
        return False

    async def is_blocked(self, *, ip: str | None, email: str) -> bool:
        return bool(await self._redis.exists(self._block_key(ip, email)))

    async def clear(self, *, ip: str | None, email: str) -> None:
        await self._redis.delete(self._fail_key(ip, email), self._block_key(ip, email))
