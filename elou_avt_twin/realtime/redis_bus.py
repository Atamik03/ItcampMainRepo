"""
realtime/redis_bus.py
======================
Thin Redis wrapper shared by the realtime fan-out (chat / simulation
snapshot pub/sub) and the auth Principal cache.

Redis is a required piece of infra going forward (see docker-compose), so
this module does not implement elaborate offline fallbacks. The only
concession to robustness is that importing this module must never fail even
if the `redis` package itself is somehow missing/broken -- callers are
expected to wrap actual connection use (publish/subscribe/get/set) in
try/except and degrade gracefully (e.g. skip the cache, log-and-continue on
broadcast).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger("elou_avt.realtime.redis_bus")

try:
    import redis.asyncio as redis_asyncio
    import redis as redis_sync
except Exception:  # pragma: no cover - defensive: never crash module import
    redis_asyncio = None  # type: ignore[assignment]
    redis_sync = None  # type: ignore[assignment]
    logger.error("redis package is unavailable; realtime/cache features are disabled")


def _redis_url() -> str:
    """Resolve the connection URL, preferring `REDIS_URL_FILE` (a mounted
    secret file, e.g. /run/secrets/redis_url) over the `REDIS_URL` env var
    so the password never has to sit in the container's environment / be
    visible via `docker inspect`.
    """
    url_file = os.environ.get("REDIS_URL_FILE", "").strip()
    if url_file:
        try:
            url = open(url_file, "r", encoding="utf-8").read().strip()
            if url:
                return url
        except OSError:
            logger.error("REDIS_URL_FILE=%s could not be read", url_file, exc_info=True)
    return os.environ.get("REDIS_URL", "redis://localhost:6379/0")


_redis_client: Optional["redis_asyncio.Redis"] = None
_redis_client_sync: Optional["redis_sync.Redis"] = None


def get_redis() -> "redis_asyncio.Redis":
    """Return the lazily-created, process-wide async Redis client."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis_asyncio.from_url(_redis_url(), decode_responses=True)
    return _redis_client


def get_redis_sync() -> "redis_sync.Redis":
    """Return the lazily-created, process-wide sync Redis client.

    Used by code paths that run as plain (non-async) functions -- e.g. the
    auth dependencies, which FastAPI executes in a threadpool.
    """
    global _redis_client_sync
    if _redis_client_sync is None:
        _redis_client_sync = redis_sync.from_url(_redis_url(), decode_responses=True)
    return _redis_client_sync


# ---------------------------------------------------------------------------
# Async pub/sub helpers (chat + simulation snapshot broadcast)
# ---------------------------------------------------------------------------

async def publish_json(channel: str, payload: Dict[str, Any]) -> None:
    data = json.dumps(payload)
    await get_redis().publish(channel, data)


async def subscribe(channel: str) -> "redis_asyncio.client.PubSub":
    """Return a PubSub object already subscribed to `channel`.

    Caller owns the returned object's lifecycle (iterate via
    `pubsub.listen()` / `pubsub.get_message()`, and `await pubsub.close()`
    when done).
    """
    pubsub = get_redis().pubsub()
    await pubsub.subscribe(channel)
    return pubsub


# ---------------------------------------------------------------------------
# Sync cache helpers (auth Principal cache)
# ---------------------------------------------------------------------------

def cache_get_json(key: str) -> Optional[Any]:
    try:
        raw = get_redis_sync().get(key)
    except Exception:
        logger.warning("Redis cache_get_json failed for key=%s", key, exc_info=True)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        logger.warning("Redis cache_get_json: invalid JSON for key=%s", key, exc_info=True)
        return None


def cache_set_json(key: str, value: Any, ttl_seconds: int) -> None:
    try:
        get_redis_sync().set(key, json.dumps(value), ex=ttl_seconds)
    except Exception:
        logger.warning("Redis cache_set_json failed for key=%s", key, exc_info=True)


def cache_delete(key: str) -> None:
    try:
        get_redis_sync().delete(key)
    except Exception:
        logger.warning("Redis cache_delete failed for key=%s", key, exc_info=True)


# ---------------------------------------------------------------------------
# Username -> cached-token-hash-keys index, used to invalidate the auth
# Principal cache on role/permission/active-state changes.
# ---------------------------------------------------------------------------

def _user_tokens_key(username: str) -> str:
    return f"auth:principal:tokens:{username}"


def remember_principal_cache_key(username: str, cache_key: str, ttl_seconds: int) -> None:
    """Track that `cache_key` currently holds a cached Principal for `username`.

    Called from auth/deps.py right after populating the Principal cache, so
    invalidate_principal_cache() can later find and delete it.
    """
    try:
        client = get_redis_sync()
        index_key = _user_tokens_key(username)
        client.sadd(index_key, cache_key)
        # Keep the index from outliving its cached entries by more than a
        # little; entries are re-added on every cache miss anyway.
        client.expire(index_key, ttl_seconds + 60)
    except Exception:
        logger.warning(
            "Redis remember_principal_cache_key failed for username=%s", username, exc_info=True
        )


def invalidate_principal_cache(username: str) -> None:
    """Drop every cached Principal known to belong to `username`."""
    try:
        client = get_redis_sync()
        index_key = _user_tokens_key(username)
        cache_keys = client.smembers(index_key)
        if cache_keys:
            client.delete(*cache_keys)
        client.delete(index_key)
    except Exception:
        logger.warning(
            "Redis invalidate_principal_cache failed for username=%s", username, exc_info=True
        )
