"""Serving the built single-page app from the same process as the API.

``uvicorn straticate.main:app`` is one command and one URL: the API on
``/api/v1/**``, the built frontend bundle on everything else. Development is
unchanged — Vite still serves the app on ``:5173`` and proxies ``/api`` to the
backend (DEVELOPMENT.md) — this module adds the *production* path, it does not
replace the development one.

**The fallback must never shadow the API, and that is a routing-order problem
with a silent failure mode.** A plain catch-all route (or a
``StaticFiles(html=True)`` mount at ``/``) matches ``/api/v1/nope`` just as
happily as it matches ``/jobs/01J…``, so every unknown API path — and every
client bug that produces one — would come back ``200 text/html`` instead of the
documented JSON error envelope. Nothing would look broken until someone tried
to parse it. So the fallback is a route that **refuses to match** anything under
:data:`API_PATH_PREFIX` (:meth:`SinglePageAppRoute.matches` returns
``Match.NONE``), rather than a handler that notices afterwards. The difference
is not cosmetic: Starlette's router dispatches the *first full match* and only
falls back to a recorded partial match, so a fallback that matched
``POST /api/v1/health`` would answer it with ``index.html`` instead of letting
the real route produce its ``405``. Refusing at match time leaves the routing
table exactly as it was when nothing was mounted.

**A checkout with no bundle is a documented state, not an error** (the pattern
feature 018 set for a missing compute backend). ``frontend/dist`` does not exist
until someone runs ``npm run build``, which is how the backend test suite runs,
how the Playwright tier runs, and how every contributor works before their first
build. The application must therefore start and serve the API normally; the only
difference is that the root URL answers with a short page saying what to build,
instead of a ``404`` nobody can act on.
"""

import html
import logging
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, Response
from starlette.routing import BaseRoute, Match, NoMatchFound, Route
from starlette.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

logger = logging.getLogger(__name__)

INDEX_FILENAME = "index.html"
"""The bundle's entry document — the file a deep link falls back to."""

API_PATH_PREFIX = "/api"
"""Path namespace the SPA fallback must never answer for.

Deliberately the whole ``/api`` tree rather than ``straticate.main.API_PREFIX``
(``/api/v1``). A future ``/api/v2`` should be a routing decision, not a silent
change in which requests turn into HTML; ``/api/anything`` unrouted is a client
error that deserves the JSON envelope; and this is already the boundary Vite's
dev proxy uses (``frontend/vite.config.ts``), so development and production
agree on where the API ends. ``tests/test_frontend_mount.py`` pins that
``API_PREFIX`` still lives under this prefix.
"""

FALLBACK_METHODS = frozenset({"GET", "HEAD"})
"""Methods the SPA answers: a browser navigation, and nothing else.

Anything else on an unrouted path is not a page load, so it stays a ``404``
envelope rather than becoming ``index.html`` with a ``200``.
"""

NOT_FOUND = 404


def bundle_index(directory: Path) -> Path | None:
    """The bundle's ``index.html`` in ``directory``, or ``None`` if there is none.

    A directory that exists but holds no ``index.html`` counts as *no bundle* —
    that is what an interrupted or failed ``npm run build`` leaves behind, and
    serving assets with no entry document would be a worse answer than the page
    telling you to build it.
    """
    index = directory / INDEX_FILENAME
    return index if index.is_file() else None


def route_path(scope: Scope) -> str:
    """The request path with any ASGI ``root_path`` prefix removed.

    Mirrors Starlette's own ``get_route_path`` (which is private, and which
    :class:`~starlette.staticfiles.StaticFiles` uses to resolve the file it
    serves) so the API guard and the file lookup measure the same string. Only
    matters when the whole application is served under a prefix; at the
    loopback default the two are identical.
    """
    path = cast(str, scope["path"])
    root_path = cast(str, scope.get("root_path", ""))
    if not root_path or not path.startswith(root_path):
        return path
    if path == root_path:
        return ""
    return path[len(root_path) :] if path[len(root_path)] == "/" else path


def is_api_path(path: str) -> bool:
    """Whether ``path`` belongs to the API rather than to the SPA."""
    return path == API_PATH_PREFIX or path.startswith(f"{API_PATH_PREFIX}/")


class SinglePageApp:
    """ASGI app serving a built bundle: the real file, or ``index.html``.

    A file that exists is served by :class:`~starlette.staticfiles.StaticFiles`
    (with its ETag/Last-Modified conditional handling and, importantly, its
    refusal to serve anything outside the bundle directory). Anything else is a
    client-side route — ``/jobs/01J…``, or a refresh of one — so the entry
    document is returned and the app's own router takes it from there.

    Only a ``404`` becomes the fallback. A ``401`` from an unreadable file, or
    any other failure, is re-raised and reaches the application's error
    handlers as itself, because "your bundle directory is not readable" must not
    be reported as "here is the app".

    A traversal attempt (``/../secrets``) is therefore answered with the app's
    own ``index.html``: ``StaticFiles`` refuses to leave the directory, and the
    fallback then treats the path like any other unknown one. Nothing outside
    the bundle is reachable through this class.
    """

    def __init__(self, directory: Path) -> None:
        """Serve ``directory``, whose ``index.html`` is the fallback document."""
        self.directory = directory
        self.index = directory / INDEX_FILENAME
        self._files = StaticFiles(directory=directory)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the requested file, falling back to the entry document."""
        try:
            await self._files(scope, receive, send)
            return
        except HTTPException as exc:
            if exc.status_code != NOT_FOUND:
                raise
        await FileResponse(self.index)(scope, receive, send)


class SinglePageAppRoute(BaseRoute):
    """The last route in the table: the SPA, and never the API.

    Registered after every router, and matching only what the API has no claim
    to. See this module's docstring for why the exclusion lives in
    :meth:`matches` rather than in the handler.

    It is a :class:`~starlette.routing.BaseRoute` rather than a FastAPI route
    because it is not part of the contract: it takes no parameters, returns no
    schema, and must stay out of the OpenAPI document that the frontend's own
    types are generated from (FastAPI builds that document from ``APIRoute``
    instances only, so this route is invisible to it by construction).
    """

    def __init__(self, app: SinglePageApp) -> None:
        """Route browser navigations to ``app``."""
        self.app = app
        self.name = "single-page-app"

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        """Match browser navigations outside the API, and nothing else.

        ``Match.NONE`` (rather than ``Match.PARTIAL``) for a non-``GET`` is
        deliberate: a partial match is what produces a ``405``, and an unrouted
        path has no method to advertise. It stays a ``404`` envelope, exactly as
        it was before anything was mounted.
        """
        if scope["type"] != "http":
            return Match.NONE, {}
        if scope.get("method") not in FALLBACK_METHODS:
            return Match.NONE, {}
        if is_api_path(route_path(scope)):
            return Match.NONE, {}
        return Match.FULL, {}

    def url_path_for(self, name: str, /, **path_params: Any) -> Any:
        """Never reversible: the SPA owns no named, parameterised paths."""
        raise NoMatchFound(name, path_params)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the bundle."""
        await self.app(scope, receive, send)


def not_built_page(directory: Path) -> str:
    """The page the root URL serves when there is no bundle.

    Names the directory that was looked in, the one command that fixes it, and
    the two things that *do* work meanwhile (the API and its docs), because a
    developer meeting this page needs to know whether the server is broken or
    merely bare. It is plain HTML with no asset of its own: the whole point is
    that nothing has been built.
    """
    location = html.escape(str(directory))
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Straticate — frontend not built</title>
  </head>
  <body>
    <h1>Straticate is running. The frontend is not built.</h1>
    <p>
      The API on this port is working — try
      <a href="/api/v1/health">/api/v1/health</a> or the interactive
      documentation at <a href="/docs">/docs</a>. There is simply no built
      frontend bundle for this process to serve.
    </p>
    <p>To serve the app from here, build it once and restart the server:</p>
    <pre>cd frontend
npm ci
npm run build</pre>
    <p>
      The bundle is expected at <code>{location}</code>. Override that with the
      <code>STRATICATE_FRONTEND_DIST_DIR</code> environment variable.
    </p>
    <p>
      Developing the frontend instead? Run the Vite dev server
      (<code>npm run dev</code>, then <a href="http://localhost:5173"
      >http://localhost:5173</a>), which proxies <code>/api</code> here and
      reloads on every edit. See <code>DEVELOPMENT.md</code>.
    </p>
  </body>
</html>
"""


def not_built_route(directory: Path) -> Route:
    """A ``GET /`` route serving :func:`not_built_page` for ``directory``.

    **200, not 404 or 503.** The server is not degraded and nothing failed: the
    API is complete and every documented endpoint answers. What is missing is a
    build step the reader can run, so the response is the instructions for
    running it — the same reasoning that makes a host with no GPU a normal
    ``200`` from ``/system/devices`` (feature 018) rather than an error.
    """

    async def endpoint(request: Request) -> Response:
        return HTMLResponse(not_built_page(directory))

    return Route("/", endpoint, methods=["GET"], name="frontend-not-built")


def mount_frontend(app: FastAPI, directory: Path) -> Path | None:
    """Serve the bundle in ``directory``, or explain how to build it.

    Call **last**, after every router is included: the SPA fallback is the end
    of the routing table, so ``/docs``, ``/openapi.json`` and every API route
    are matched before it is consulted (and it refuses ``/api/**`` outright
    regardless — see :class:`SinglePageAppRoute`).

    Returns:
        The ``index.html`` being served, or ``None`` when there is no bundle.

    The bundle is looked for **once, when the application is built**, not per
    request: that keeps a filesystem check off the hot path, and the answer
    cannot change for a running server in any way that matters — a build
    finishing later leaves the process serving what it found at startup. So
    building the frontend while the server is running means restarting it, which
    is what DEVELOPMENT.md says.
    """
    index = bundle_index(directory)
    if index is None:
        app.router.routes.append(not_built_route(directory))
        return None
    app.router.routes.append(SinglePageAppRoute(SinglePageApp(directory)))
    return index


def log_bundle_state(app: FastAPI) -> None:
    """Log which of the two documented modes this server started in.

    Called from the lifespan rather than from ``create_app``, for the reason
    every other startup log record is (see :func:`straticate.main.lifespan`):
    ``create_app`` runs at *import*, before either entry path has configured
    logging, so a record written there lands on ``logging.lastResort``.
    """
    directory = cast(Path | None, getattr(app.state, "frontend_dist_dir", None))
    if directory is None:
        return
    if getattr(app.state, "frontend_index", None) is None:
        logger.info(
            "No frontend bundle at %s: serving the API only. Build it with "
            "`npm run build` in frontend/ and restart to serve the app from this process.",
            directory,
        )
    else:
        logger.info("Serving the frontend bundle from %s", directory)
