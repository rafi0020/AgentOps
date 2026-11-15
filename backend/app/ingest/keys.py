"""Ingestion keys: creation and the request dependency that authenticates them.

A key looks like `ak_1a2b3c4d_<32 characters>`. Only its SHA-256 is stored, so a copy of the database does
not yield usable keys; the plain key is shown once, when it is created.
"""

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from redis.exceptions import RedisError
from sqlalchemy import func, select, update

from app.db import system_session
from app.models.projects import IngestKey
from app.redis import get_redis

log = logging.getLogger("agentops.ingest")

SCOPES = ("ingest", "prompts:read")
CACHE_SECONDS = 60


@dataclass(frozen=True)
class KeyPrincipal:
    org_id: UUID
    project_id: UUID
    key_id: UUID
    scopes: tuple[str, ...]


def hash_key(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


def new_key() -> tuple[str, str, str]:
    """(plain key, prefix shown in lists, hash to store)."""
    prefix = secrets.token_hex(4)
    plain = f"ak_{prefix}_{secrets.token_urlsafe(24)}"
    return plain, f"ak_{prefix}", hash_key(plain)


def _cache_key(key_hash: str) -> str:
    return f"ikey:{key_hash}"


async def forget_key(key_hash: str) -> None:
    try:
        await get_redis().delete(_cache_key(key_hash))
    except RedisError:  # the revocation is already in the database; a cached copy expires within CACHE_SECONDS
        log.warning("could not clear a revoked key from the cache; it expires on its own")


async def key_principal(request: Request) -> KeyPrincipal:
    header = request.headers.get("Authorization", "")
    plain = header[7:].strip() if header[:7].lower() == "bearer " else ""
    if not plain.startswith("ak_"):
        raise HTTPException(401, "An ingestion key is required: Authorization: Bearer ak_...")
    key_hash = hash_key(plain)
    try:  # Redis is only a cache here: when it is away, the database answers
        cached = await get_redis().get(_cache_key(key_hash))
    except RedisError:
        log.warning("key cache unavailable; reading the key from the database")
        cached = None
    if cached:
        d = json.loads(cached)
        return KeyPrincipal(UUID(d["org"]), UUID(d["project"]), UUID(d["key"]), tuple(d["scopes"]))
    async with system_session() as db:
        key = (
            await db.execute(select(IngestKey).where(IngestKey.key_hash == key_hash, IngestKey.revoked_at.is_(None)))
        ).scalar_one_or_none()
        if key is None:
            raise HTTPException(401, "Unknown or revoked ingestion key.")
        # Written at most once per cache period, so a busy sender does not cause a write per request.
        await db.execute(update(IngestKey).where(IngestKey.id == key.id).values(last_used_at=func.now()))
    principal = KeyPrincipal(key.org_id, key.project_id, key.id, tuple(key.scopes))
    payload = {"org": str(key.org_id), "project": str(key.project_id), "key": str(key.id), "scopes": key.scopes}
    try:
        await get_redis().set(_cache_key(key_hash), json.dumps(payload), ex=CACHE_SECONDS)
    except RedisError:
        return principal  # the next request reads the database again
    # A revoke that committed after the read above cleared the cache before this write, so the copy just written is
    # stale. Looking again after the write closes that window: if the key is revoked, clear the copy and refuse.
    async with system_session() as db:
        revoked = await db.scalar(select(IngestKey.revoked_at).where(IngestKey.id == key.id))
    if revoked is not None:
        await forget_key(key_hash)
        raise HTTPException(401, "Unknown or revoked ingestion key.")
    return principal


def require_scope(scope: str):
    async def dependency(principal: KeyPrincipal = Depends(key_principal)) -> KeyPrincipal:
        if scope not in principal.scopes:
            raise HTTPException(403, f"This key does not have the '{scope}' scope.")
        return principal

    return dependency
