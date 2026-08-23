"""
lms/chat_hub.py
================
Тонкий хаб для многооператорского чата практики. Держит набор подключённых
WebSocket-клиентов `/ws/chat` и рассылает сообщения, опубликованные через REST
`POST /lms/chat/send`. Вызов `broadcast()` безопасен из любого потока
(в т.ч. из sync-обработчика FastAPI, выполняющегося в threadpool).

Local fan-out (same-instance delivery) stays exactly as before -- instant,
in-process. In addition, every broadcast is best-effort published to the
Redis channel "lms:chat" so other backend instances (horizontal scale) can
forward it to their own locally-connected clients via `redis_listener()`.
Each published message is tagged with this process's instance id so that
`redis_listener()` can skip messages that originated from this very process
(they're already delivered via the local fan-out path) and avoid duplicate
delivery / rebroadcast loops.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, Optional, Set

from realtime import redis_bus

logger = logging.getLogger("elou_avt.lms")

_clients: Set[Any] = set()
_loop: Optional[asyncio.AbstractEventLoop] = None

CHANNEL = "lms:chat"
INSTANCE_ID = uuid.uuid4().hex


def register(websocket: Any) -> None:
    global _loop
    try:
        _loop = asyncio.get_event_loop()
    except RuntimeError:
        _loop = None
    _clients.add(websocket)


def unregister(websocket: Any) -> None:
    _clients.discard(websocket)


def broadcast(payload: Dict[str, Any]) -> None:
    loop = _loop
    if loop is None or loop.is_closed():
        return
    if _clients:
        asyncio.run_coroutine_threadsafe(_send_all(payload), loop)
    asyncio.run_coroutine_threadsafe(_publish_remote(payload), loop)


async def _send_all(payload: Dict[str, Any]) -> None:
    dead = []
    for ws in list(_clients):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


async def _publish_remote(payload: Dict[str, Any]) -> None:
    try:
        envelope = {"_origin": INSTANCE_ID, "payload": payload}
        await redis_bus.publish_json(CHANNEL, envelope)
    except Exception:
        logger.warning("Redis publish to %s failed", CHANNEL, exc_info=True)


async def redis_listener() -> None:
    """Forward chat messages published by OTHER instances to local clients.

    Runs for the lifetime of the app (started/cancelled from `_lifespan` in
    api_server.py). Messages this same process published are skipped -- they
    were already delivered to local clients synchronously by `broadcast()`.
    """
    global _loop
    try:
        _loop = asyncio.get_event_loop()
    except RuntimeError:
        pass
    try:
        pubsub = await redis_bus.subscribe(CHANNEL)
    except Exception:
        logger.error("Could not subscribe to Redis channel %s", CHANNEL, exc_info=True)
        return
    try:
        async for message in pubsub.listen():
            if message is None or message.get("type") != "message":
                continue
            try:
                import json

                envelope = json.loads(message["data"])
            except Exception:
                logger.warning("Invalid message on %s", CHANNEL, exc_info=True)
                continue
            if envelope.get("_origin") == INSTANCE_ID:
                continue
            payload = envelope.get("payload")
            if payload is None:
                continue
            await _send_all(payload)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("redis_listener for %s crashed", CHANNEL)
    finally:
        try:
            await pubsub.unsubscribe(CHANNEL)
            await pubsub.close()
        except Exception:
            pass
