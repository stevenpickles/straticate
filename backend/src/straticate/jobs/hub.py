"""WebSocket event hub: fan-out of typed events to connected browsers.

The :class:`EventHub` is the transport half of ARCHITECTURE.md §11. It owns the
set of connected clients and a single :data:`~straticate.jobs.JobEventListener`
(:meth:`EventHub.publish`) that is registered with the
:class:`~straticate.jobs.JobManager` by the application lifespan.

Design constraints that shape this module:

- **The manager's dispatcher must never be stalled.** Events are delivered by
  one ordered dispatcher task that awaits coroutine listeners, so a listener
  that waits on a socket would delay every other client *and* the job pipeline.
  :meth:`EventHub.publish` is therefore synchronous and never touches a socket:
  it serializes the event **once** and hands the resulting frame to each
  client's bounded outbound queue. A dedicated sender task per client performs
  the actual (potentially slow) socket writes.
- **Bounded memory.** Each client's queue holds at most
  ``client_queue_size`` frames. See "Backpressure" below.
- **Isolation.** A failing or slow client is torn down on its own; other
  clients and the job pipeline are unaffected.
- **Broadcast to all.** Straticate is a single-user local application, so there
  is no per-job subscription filtering: every connected client receives every
  event (ARCHITECTURE.md §11).

Backpressure / overflow policy
------------------------------

When a client's outbound queue is full, the hub inspects the **oldest** queued
frame:

- If it is a *superseded* frame — ``job_progress`` or ``runtime_metrics``, both
  of which are periodic samples whose newer sibling carries strictly fresher
  state — it is dropped and the new frame takes its place.
- Otherwise the buffer is saturated with state that may not be discarded
  (``job_created``/``job_started``/``job_stage_changed`` and the terminal
  ``job_completed``/``job_cancelled``/``job_failed``). The client is then
  disconnected with close code ``1013`` (*try again later*) rather than blocked
  or silently truncated. Per ``docs/contracts/websocket-events.md`` clients
  re-synchronise over REST on (re)connect, so a loud disconnect is recoverable
  while a silently dropped terminal event would not be.

A terminal event is consequently never dropped in silence: it is either
delivered or the client is closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, Protocol, cast

from starlette.requests import HTTPConnection

from straticate.schemas.events import WebSocketEvent

logger = logging.getLogger(__name__)

DEFAULT_CLIENT_QUEUE_SIZE = 256
"""Outbound frames buffered per client before the overflow policy kicks in."""

EVICTABLE_EVENT_TYPES = frozenset({"job_progress", "runtime_metrics"})
"""Event types that may be dropped for a slow client (periodic samples).

Every other event type carries non-recoverable state and is never evicted; a
client that cannot keep up with those is disconnected instead.
"""

CLOSE_GOING_AWAY = 1001
"""RFC 6455 close code used when the server shuts down."""

CLOSE_INTERNAL_ERROR = 1011
"""RFC 6455 close code used when sending to a client failed."""

CLOSE_TRY_AGAIN_LATER = 1013
"""RFC 6455 close code used when a client cannot keep up (buffer overflow)."""


class EventSocket(Protocol):
    """The slice of Starlette's ``WebSocket`` the hub actually uses.

    Declaring the dependency structurally keeps the hub testable with simple
    doubles; a real :class:`starlette.websockets.WebSocket` satisfies it.
    """

    async def send_text(self, data: str) -> None:
        """Send one text frame to the client."""
        ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        """Close the connection with an RFC 6455 close code."""
        ...


@dataclass(frozen=True, slots=True)
class _Frame:
    """One serialized event, ready to be written to any number of sockets."""

    event_type: str
    payload: str


@dataclass(slots=True)
class _Client:
    """Hub-side bookkeeping for one connected websocket."""

    socket: EventSocket
    queue: asyncio.Queue[_Frame]
    task: asyncio.Task[None] | None = None
    dropped: int = 0


class EventHub:
    """Broadcasts typed events to every connected WebSocket client.

    Lifecycle: construct, :meth:`register` clients as they connect,
    :meth:`unregister` them as they disconnect, :meth:`aclose` on shutdown. The
    application lifespan creates a fresh hub per cycle and registers
    :meth:`publish` with the :class:`~straticate.jobs.JobManager`; a closed hub
    refuses new registrations and publishes nothing.

    Args:
        client_queue_size: Maximum outbound frames buffered per client before
            the overflow policy (see the module docstring) applies.

    Raises:
        ValueError: If ``client_queue_size`` is smaller than 1.
    """

    def __init__(self, *, client_queue_size: int = DEFAULT_CLIENT_QUEUE_SIZE) -> None:
        if client_queue_size < 1:
            raise ValueError("client_queue_size must be at least 1")
        self._client_queue_size = client_queue_size
        self._clients: dict[EventSocket, _Client] = {}
        self._background: set[asyncio.Task[None]] = set()
        self._closed = False

    # -- introspection -----------------------------------------------------

    @property
    def connection_count(self) -> int:
        """Number of currently registered clients."""
        return len(self._clients)

    @property
    def is_closed(self) -> bool:
        """Whether :meth:`aclose` has been called."""
        return self._closed

    # -- connection lifecycle ----------------------------------------------

    def register(self, websocket: EventSocket) -> None:
        """Start broadcasting to ``websocket``.

        Synchronous by design — it only allocates the client's outbound queue
        and starts its sender task — so an endpoint can register a freshly
        accepted socket without an extra await. Registering the same socket
        twice is a no-op. The caller must have accepted the connection first.

        Raises:
            RuntimeError: If the hub is closed, or if there is no running event
                loop.
        """
        if self._closed:
            raise RuntimeError("EventHub is closed")
        if websocket in self._clients:
            return
        client = _Client(socket=websocket, queue=asyncio.Queue(maxsize=self._client_queue_size))
        self._clients[websocket] = client
        client.task = asyncio.get_running_loop().create_task(
            self._sender_loop(client), name="straticate-ws-sender"
        )

    async def unregister(self, websocket: EventSocket) -> None:
        """Stop broadcasting to ``websocket`` and tear its sender task down.

        Idempotent, and safe for an unknown socket. The socket itself is *not*
        closed: the endpoint owns the connection and unregisters after the
        client already went away. Any frames still buffered for the client are
        discarded — it is disconnecting anyway.
        """
        client = self._clients.pop(websocket, None)
        if client is None:
            return
        await self._stop_sender(client)

    async def aclose(self) -> None:
        """Disconnect every client and release resources. Idempotent.

        Each connection is closed with code ``1001`` (*going away*) so browsers
        can distinguish an orderly server shutdown from a crash. Sockets that
        already died are ignored. A closed hub cannot be reused — the
        application lifespan creates a fresh one per cycle.
        """
        self._closed = True
        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            await self._stop_sender(client)
            await self._close_socket(client.socket, CLOSE_GOING_AWAY)
        pending = list(self._background)
        self._background.clear()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # -- publishing --------------------------------------------------------

    def publish(self, event: WebSocketEvent) -> None:
        """Broadcast ``event`` to every connected client.

        Registered with :meth:`~straticate.jobs.JobManager.add_listener`; also
        the entry point for events the job manager does not produce (feature
        019's ``runtime_metrics``) — the hub forwards any
        :data:`~straticate.schemas.events.WebSocketEvent` without
        special-casing.

        The event is serialized exactly once and the resulting frame is handed
        to each client's bounded outbound queue. This method never awaits a
        socket, so the job manager's ordered dispatcher is never stalled by a
        slow client. Delivery order per client matches publication order.

        Must be called on the application's event loop (the job manager's
        dispatcher already runs there). Publishing on a closed hub, or with no
        clients connected, is a no-op.
        """
        if self._closed or not self._clients:
            return
        frame = _Frame(
            event_type=event.type,
            payload=json.dumps(event.model_dump(mode="json")),
        )
        for client in list(self._clients.values()):
            self._enqueue(client, frame)

    # -- internal ----------------------------------------------------------

    def _enqueue(self, client: _Client, frame: _Frame) -> None:
        """Buffer ``frame`` for ``client``, applying the overflow policy."""
        try:
            client.queue.put_nowait(frame)
            return
        except asyncio.QueueFull:
            pass
        # The queue is full: the oldest frame is the only one we can evict
        # without reordering the stream.
        try:
            oldest = client.queue.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover - maxsize >= 1, just full
            client.queue.put_nowait(frame)
            return
        if oldest.event_type in EVICTABLE_EVENT_TYPES:
            client.dropped += 1
            logger.debug(
                "Slow WebSocket client: dropped a %s frame (%d dropped so far)",
                oldest.event_type,
                client.dropped,
            )
            client.queue.put_nowait(frame)
            return
        logger.warning(
            "WebSocket client cannot keep up (oldest buffered frame is %s); disconnecting",
            oldest.event_type,
        )
        self._clients.pop(client.socket, None)
        self._schedule(self._terminate(client, CLOSE_TRY_AGAIN_LATER))

    async def _sender_loop(self, client: _Client) -> None:
        """Write buffered frames to one client's socket, forever.

        Cancelled by :meth:`_stop_sender` at teardown. A send failure drops
        only this client: it is unregistered and its socket closed, while every
        other client keeps receiving events.
        """
        try:
            while True:
                frame = await client.queue.get()
                await client.socket.send_text(frame.payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.info("Dropping a WebSocket client after a send failure", exc_info=True)
            self._clients.pop(client.socket, None)
            client.task = None
            await self._close_socket(client.socket, CLOSE_INTERNAL_ERROR)

    async def _terminate(self, client: _Client, code: int) -> None:
        """Stop a client's sender task and close its socket."""
        await self._stop_sender(client)
        await self._close_socket(client.socket, code)

    async def _stop_sender(self, client: _Client) -> None:
        """Cancel and await a client's sender task (never awaits itself)."""
        task = client.task
        client.task = None
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    @staticmethod
    async def _close_socket(socket: EventSocket, code: int) -> None:
        """Close ``socket``, ignoring an already-dead connection."""
        try:
            await socket.close(code=code)
        except Exception:  # a dying socket must not break teardown
            logger.debug("Ignoring error while closing a WebSocket", exc_info=True)

    def _schedule(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run ``coro`` as a tracked background task (awaited by :meth:`aclose`)."""
        task = asyncio.get_running_loop().create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)


def get_event_hub(connection: HTTPConnection) -> EventHub:
    """FastAPI dependency: the application's :class:`EventHub`.

    Works for both WebSocket and HTTP endpoints (``HTTPConnection`` is the
    common base of ``WebSocket`` and ``Request``)::

        @router.websocket("/ws")
        async def events(websocket: WebSocket, hub: Annotated[EventHub, Depends(get_event_hub)]):
            ...

    The instance is created, wired to the job manager, and closed by the
    application lifespan (a fresh instance per lifespan cycle) and lives on
    ``app.state.event_hub``.
    """
    return cast(EventHub, connection.app.state.event_hub)
