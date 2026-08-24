"""A loopback HTTP server serving synthetic weights artifacts.

Feature 025's download path is only worth testing if it is exercised for real:
a stubbed transport would prove that the code calls a mock, not that it streams
a body, honours a ``Content-Length``, notices a truncated response or stops a
server that sends too much. So the tests run a real ``ThreadingHTTPServer``
bound to ``127.0.0.1`` on an **ephemeral port** and point synthetic catalogs at
it. **No test touches the network**; nothing here resolves a name or opens a
socket off the loopback interface.

The server can be told to misbehave in each of the ways a real weights host
does: an error status, a body shorter or longer than the catalog declares, a
length-less response, a body whose bytes hash to the wrong value, and a
transfer parked mid-stream until the test releases it (which is how the
concurrency, progress and event-loop tests get an exact "the download is now in
flight" moment without ever sleeping).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any, Self, cast

OCTET_STREAM = "application/octet-stream"


@dataclass
class ServedArtifact:
    """What the server answers for one path.

    Attributes:
        body: The bytes to send (all of them, unless ``status`` is not 200).
        status: HTTP status to answer with.
        declared_length: ``Content-Length`` to send; ``None`` uses ``len(body)``.
            A value larger than the body is how a *truncated* transfer is
            simulated; a value smaller than the catalog's ``size_bytes`` is how
            a short one is.
        omit_length: Send no ``Content-Length`` at all and close the connection
            when done, so the client reads until EOF. This is the case the
            per-chunk size guard exists for.
        stall_after: Bytes to send before waiting on ``gate``.
        gate: Released by the test to let the rest of the body through.
        started: Set once ``stall_after`` bytes are on the wire.
        requests: How many times this path has been requested.
    """

    body: bytes = b""
    status: int = 200
    declared_length: int | None = None
    omit_length: bool = False
    stall_after: int = 0
    gate: threading.Event | None = None
    started: threading.Event = field(default_factory=threading.Event)
    requests: int = 0


class _ArtifactServer(ThreadingHTTPServer):
    """A ``ThreadingHTTPServer`` carrying the artifact table its handler reads."""

    daemon_threads = True
    allow_reuse_address = False

    artifacts: dict[str, ServedArtifact]


class _Handler(BaseHTTPRequestHandler):
    """Serves whatever :class:`ServedArtifact` is registered for the path."""

    protocol_version = "HTTP/1.1"
    server_version = "StraticateTestWeights/1.0"

    def do_GET(self) -> None:
        """Answer one request from the artifact table."""
        artifacts = cast(_ArtifactServer, self.server).artifacts
        artifact = artifacts.get(self.path)
        if artifact is None:
            self._send_error_page(404, b"<html>no such artifact</html>")
            return
        artifact.requests += 1
        if artifact.status != 200:
            # A real weights host that has been taken down answers with an HTML
            # error page, not with weights. Installing it would be the exact
            # failure the pinned SHA-256 exists to prevent.
            self._send_error_page(artifact.status, artifact.body or b"<html>gone</html>")
            return
        self._send_artifact(artifact)

    def _send_error_page(self, status: int, page: bytes) -> None:
        """Send a non-200 response with an HTML body."""
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self._write(page)

    def _send_artifact(self, artifact: ServedArtifact) -> None:
        """Send a 200 response, possibly stalled or mis-declared."""
        self.send_response(200)
        self.send_header("Content-Type", OCTET_STREAM)
        if artifact.omit_length:
            self.send_header("Connection", "close")
            self.close_connection = True
        else:
            length = artifact.declared_length
            if length is not None and length != len(artifact.body):
                # A truncated transfer: promise N bytes, send fewer, hang up.
                # Keeping the connection alive instead would leave the client
                # waiting for bytes that are never coming.
                self.close_connection = True
            self.send_header(
                "Content-Length", str(len(artifact.body) if length is None else length)
            )
        self.end_headers()
        gate = artifact.gate
        if gate is None:
            self._write(artifact.body)
            return
        self._write(artifact.body[: artifact.stall_after])
        artifact.started.set()
        gate.wait(timeout=30.0)
        self._write(artifact.body[artifact.stall_after :])

    def _write(self, payload: bytes) -> None:
        """Write to the socket, tolerating a client that has walked away.

        The size-overrun test aborts the response mid-body on purpose; a
        ``BrokenPipeError`` in this thread is the expected consequence, not a
        failure.
        """
        try:
            self.wfile.write(payload)
            self.wfile.flush()
        except OSError:
            self.close_connection = True

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the default stderr access log."""


class WeightsServer:
    """A running loopback HTTP server the tests register artifacts with."""

    def __init__(self) -> None:
        self._server = _ArtifactServer(("127.0.0.1", 0), _Handler)
        self._server.artifacts = {}
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="weights-server", daemon=True
        )

    @property
    def base_url(self) -> str:
        """The ``http://127.0.0.1:{port}`` root this server answers on."""
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def serve(self, path: str, artifact: ServedArtifact) -> str:
        """Register ``artifact`` at ``path``; return the URL to download it from."""
        self._server.artifacts[path] = artifact
        return f"{self.base_url}{path}"

    def dead_url(self, path: str = "/gone.bin") -> str:
        """A URL on this host that no artifact is registered for (404)."""
        return f"{self.base_url}{path}"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # Release every gate first: a handler thread parked mid-body would
        # otherwise sit on its socket until its own timeout expired.
        for artifact in self._server.artifacts.values():
            if artifact.gate is not None:
                artifact.gate.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=30.0)
