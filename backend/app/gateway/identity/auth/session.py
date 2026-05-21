"""Redis-backed session store for authenticated users.

Each session keeps a hash at ``{prefix}:session:{sid}`` with these fields:

- ``user_id`` — owning user
- ``tenant_id`` — active tenant at login (may be updated via ``update_tenant``)
- ``refresh_hash`` — SHA-256 of the refresh token (we never store plaintext)
- ``created_at`` — ISO8601
- ``ip`` / ``ua`` — diagnostic metadata
- ``revoked`` — ``"1"`` when revoked

A secondary set ``{prefix}:session:by_user:{user_id}`` indexes session ids per
user so we can revoke every session for a user in O(N) and list them cheaply.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime


def _hash_refresh(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class SessionRecord:
    sid: str
    user_id: int
    tenant_id: int | None
    refresh_hash: str
    ip: str | None
    user_agent: str | None
    created_at: datetime
    revoked: bool

    @property
    def ua(self) -> str | None:
        return self.user_agent


class SessionStore:
    """Thin async wrapper around Redis for identity sessions."""

    def __init__(self, redis_client, *, refresh_ttl_sec: int, key_prefix: str = "deerflow"):
        self._redis = redis_client
        self._ttl = refresh_ttl_sec
        self._prefix = key_prefix

    # ---- key helpers ----

    def _session_key(self, sid: str) -> str:
        return f"{self._prefix}:session:{sid}"

    def _by_user_key(self, user_id: int) -> str:
        return f"{self._prefix}:session:by_user:{user_id}"

    # ---- CRUD ----

    async def create(
        self,
        user_id: int,
        tenant_id: int | None,
        refresh_token: str,
        *,
        ip: str | None,
        ua: str | None,
    ) -> SessionRecord:
        sid = uuid.uuid4().hex
        now = datetime.now(UTC)
        refresh_hash = _hash_refresh(refresh_token)
        data = {
            "user_id": str(user_id),
            "tenant_id": "" if tenant_id is None else str(tenant_id),
            "refresh_hash": refresh_hash,
            "created_at": now.isoformat(),
            "ip": ip or "",
            "ua": ua or "",
            "revoked": "0",
        }
        key = self._session_key(sid)
        await self._redis.hset(key, mapping=data)
        await self._redis.expire(key, self._ttl)
        await self._redis.sadd(self._by_user_key(user_id), sid)
        await self._redis.expire(self._by_user_key(user_id), self._ttl)
        return SessionRecord(
            sid=sid,
            user_id=user_id,
            tenant_id=tenant_id,
            refresh_hash=refresh_hash,
            ip=ip,
            user_agent=ua,
            created_at=now,
            revoked=False,
        )

    async def get(self, sid: str) -> SessionRecord | None:
        data = await self._redis.hgetall(self._session_key(sid))
        if not data:
            return None
        if data.get("revoked") == "1":
            return None
        return self._from_hash(sid, data)

    async def revoke(self, sid: str) -> None:
        key = self._session_key(sid)
        # Fetch user_id so we can clean the index set.
        user_id_raw = await self._redis.hget(key, "user_id")
        await self._redis.hset(key, "revoked", "1")
        if user_id_raw:
            await self._redis.srem(self._by_user_key(int(user_id_raw)), sid)

    async def revoke_all_for_user(self, user_id: int) -> int:
        sids = await self._redis.smembers(self._by_user_key(user_id))
        count = 0
        for sid in sids:
            await self._redis.hset(self._session_key(sid), "revoked", "1")
            count += 1
        # Empty the index atomically.
        if sids:
            await self._redis.delete(self._by_user_key(user_id))
        return count

    async def list_for_user(self, user_id: int) -> list[SessionRecord]:
        sids = await self._redis.smembers(self._by_user_key(user_id))
        out: list[SessionRecord] = []
        for sid in sids:
            rec = await self.get(sid)
            if rec is not None:
                out.append(rec)
        return out

    async def verify_refresh(self, sid: str, refresh_token: str) -> bool:
        rec = await self.get(sid)
        if rec is None:
            return False
        return secrets.compare_digest(rec.refresh_hash, _hash_refresh(refresh_token))

    async def update_tenant(self, sid: str, tenant_id: int | None) -> None:
        await self._redis.hset(
            self._session_key(sid),
            "tenant_id",
            "" if tenant_id is None else str(tenant_id),
        )

    async def count_active(self) -> int:
        """Return the number of non-revoked session records in Redis.

        Implementation uses a non-blocking ``SCAN`` over the
        ``{prefix}:session:*`` keyspace with a large ``COUNT`` hint to
        minimise round-trips. The ``by_user:`` index keys are filtered
        out by a prefix check — Redis glob cannot express "not" so the
        filter lives here. Revoked sessions are excluded by reading the
        ``revoked`` flag on each candidate.

        This is intended to be called from ``/metrics`` scrapes (a few
        times per minute); it is NOT a hot-path helper.
        """

        match = f"{self._prefix}:session:*"
        by_user_prefix = f"{self._prefix}:session:by_user:"
        count = 0
        cursor = 0
        while True:
            cursor, keys = await self._redis.scan(cursor=cursor, match=match, count=500)
            for k in keys:
                if isinstance(k, bytes):
                    k = k.decode()
                if k.startswith(by_user_prefix):
                    continue
                revoked = await self._redis.hget(k, "revoked")
                if revoked in (b"1", "1"):
                    continue
                count += 1
            if cursor == 0:
                break
        return count

    # ---- internals ----

    @staticmethod
    def _from_hash(sid: str, data: dict) -> SessionRecord:
        tid_raw = data.get("tenant_id", "")
        return SessionRecord(
            sid=sid,
            user_id=int(data["user_id"]),
            tenant_id=int(tid_raw) if tid_raw else None,
            refresh_hash=data.get("refresh_hash", ""),
            ip=data.get("ip") or None,
            user_agent=data.get("ua") or None,
            created_at=datetime.fromisoformat(data["created_at"]),
            revoked=data.get("revoked") == "1",
        )
