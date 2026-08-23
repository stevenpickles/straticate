"""WebSocket endpoint: ``WS /api/v1/ws``.

The single real-time channel of the application (ARCHITECTURE.md §11). The
endpoint itself is deliberately thin: it accepts the connection, hands the
socket to the :class:`~straticate.jobs.hub.EventHub` — which owns all fan-out,
buffering, and backpressure — and then reads from the socket purely to notice
that the client went away.

The v1 contract is server → client push only. Inbound frames are accepted and
discarded so that a client (or a proxy) sending something unexpected can never
break the connection; a future client → server protocol can be layered on here
without touching the hub.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from straticate.jobs.hub import CLOSE_GOING_AWAY, EventHub, get_event_hub

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])

HubDep = Annotated[EventHub, Depends(get_event_hub)]
"""The application's event hub, resolved from ``app.state``."""


@router.websocket("/ws")
async def websocket_events(websocket: WebSocket, hub: HubDep) -> None:
    """Stream job and telemetry events to one connected client.

    Accepts the connection, registers it with the hub (which starts pushing
    every event immediately) and then drains inbound messages until the client
    disconnects or the server closes the socket. Teardown always unregisters
    the client, whether the connection ended by client disconnect, by hub
    shutdown, or by an error.

    A connection arriving once the hub is closed (the server is shutting down)
    is closed with ``1001``, indistinguishable from a connection that was open
    when shutdown began. Letting the ``RuntimeError`` escape instead would
    reach the application-wide ``Exception`` handler, which can only produce a
    ``JSONResponse`` — meaningless in a WebSocket scope.
    """
    await websocket.accept()
    try:
        hub.register(websocket)
    except RuntimeError:
        logger.debug("Refusing a WebSocket connection: the event hub is closed")
        await websocket.close(code=CLOSE_GOING_AWAY)
        return
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            # Server → client push only: inbound payloads (text, bytes, or
            # anything unknown) are ignored, never parsed, never fatal.
    except WebSocketDisconnect:
        pass
    finally:
        await hub.unregister(websocket)
