"""Serving the built single-page app from the same process as the API.

``uvicorn straticate.main:app`` is one command and one URL: the API on
``/api/v1/**``, the built frontend bundle on everything else. Development is
unchanged — Vite still serves the app on ``:5173`` and proxies ``/api`` to the
backend (DEVELOPMENT.md) — this module adds the *production* path, it does not
replace the development one.

**The frontend is the router's ``default``, not a route.** That is the whole
design, and it is what keeps the fallback from shadowing anything. Starlette's
router (and FastAPI's) dispatches in a fixed order::

    full match → partial match (405) → redirect_slashes → default

A catch-all *route* sits in the first step, where **a full match wins wherever
it is in the table**: it would answer ``POST /api/v1/health`` itself rather than
letting that route's partial match produce its ``405``, it would answer
``/api/v1/nope`` with ``200 text/html`` instead of the documented JSON envelope,
and by matching every unrouted path it would make ``redirect_slashes`` dead
code, so ``/docs/`` would quietly become the app instead of redirecting to
``/docs``. Installed as ``default`` the frontend is consulted **only after every
route, every partial match and every redirect have had their say**, so the
routing table behaves exactly as it did before anything was mounted. The
fallback can only add answers for requests that would otherwise have been
``404``.

It then declines three kinds of request back to the router's own ``not_found``,
so those keep the behaviour they had:

- anything under :data:`API_PATH_PREFIX`, matched on the **normalized** path —
  ``//api/v1/nope`` and ``/./api/v1/nope`` are the same resource as
  ``/api/v1/nope`` to every client that builds a URL by concatenation, and a
  guard that only reserves the canonical spelling reserves nothing;
- any method other than ``GET``/``HEAD``, because an unrouted path has no method
  to advertise and ``404`` is what it was;
- any non-HTTP scope, so the WebSocket is untouched.

**A miss inside the bundle is not a deep link.** ``/jobs/01J…`` is a
client-side route and gets ``index.html``; ``/assets/index-OLD.js`` is a file
that is genuinely not there and gets a ``404`` envelope. Answering the second
with the entry document is how a tab left open across a rebuild reports
"expected a JavaScript module script but the server responded with a MIME type
of text/html" — an error naming neither the missing chunk nor the stale tab.
:func:`is_navigation` draws the line.

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
import posixpath
import re
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import HTMLResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

INDEX_FILENAME = "index.html"
"""The bundle's entry document — the file a deep link falls back to."""

API_PATH_PREFIX = "/api"
"""Path namespace the frontend fallback must never answer for.

Deliberately the whole ``/api`` tree rather than ``straticate.main.API_PREFIX``
(``/api/v1``). A future ``/api/v2`` should be a routing decision, not a silent
change in which requests turn into HTML; ``/api/anything`` unrouted is a client
error that deserves the JSON envelope; and this is already the boundary Vite's
dev proxy uses (``frontend/vite.config.ts``), so development and production
agree on where the API ends. ``tests/test_frontend_mount.py`` pins that
``API_PREFIX`` still lives under this prefix.
"""

FALLBACK_METHODS = frozenset({"GET", "HEAD"})
"""Methods the frontend answers: a browser navigation, and nothing else.

Anything else on an unrouted path is not a page load, so it keeps the ``404``
envelope it had — including ``POST /``, which must not depend on whether
somebody has run ``npm run build``.
"""

NOT_FOUND = 404

_REPEATED_SLASHES = re.compile(r"/{2,}")


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


def normalized_path(path: str) -> str:
    """``path`` with repeated slashes collapsed and ``.``/``..`` resolved.

    The API guard has to run on this rather than on the raw path, because a
    client that builds ``f"{base}/api/v1/jobs"`` with a ``base`` ending in a
    slash emits ``//api/v1/jobs``, and browsers send that through untouched. No
    route matches it, so a guard checking only the canonical spelling would hand
    it to the frontend and answer an API call with ``200 text/html`` — the exact
    failure this module exists to prevent, reintroduced by a spelling.

    ``posixpath.normpath`` alone is not enough: POSIX gives a path beginning
    with **exactly two** slashes implementation-defined meaning, so
    ``normpath("//api")`` is ``"//api"``. Slashes are collapsed first.

    Resolving ``..`` is deliberate and errs toward the API: ``/x/../api/v1/nope``
    reserves rather than serves. Nothing is resolved against the filesystem here
    — :class:`~starlette.staticfiles.StaticFiles` does its own, stricter lookup
    for the file it serves, and refuses to leave the bundle directory.
    """
    collapsed = _REPEATED_SLASHES.sub("/", path)
    if not collapsed:
        return "/"
    return posixpath.normpath(collapsed)


def is_api_path(path: str) -> bool:
    """Whether ``path`` belongs to the API rather than to the frontend.

    Normalizes first (see :func:`normalized_path`), so every spelling of an API
    path is reserved, not only the canonical one.
    """
    normalized = normalized_path(path)
    return normalized == API_PATH_PREFIX or normalized.startswith(f"{API_PATH_PREFIX}/")


def is_navigation(scope: Scope) -> bool:
    """Whether this request is a browser navigating to a page.

    Decides whether a path the bundle has no file for is a **client-side route**
    (answer with ``index.html``) or a **missing file** (answer with the ``404``
    envelope). Two signals, either sufficient:

    - the client asked for HTML. Every browser navigation sends
      ``Accept: text/html,…``; a module ``import()``, a stylesheet fetch and an
      ``XMLHttpRequest`` for a bundle file do not.
    - the last path segment carries no file extension. ``/jobs/01J…`` is a
      route, ``/assets/index-OLD.js`` is a file. This covers ``curl`` and any
      client that sends ``Accept: */*`` for a page.

    Getting this wrong in the generous direction is what makes a tab left open
    across a rebuild fail with "expected a JavaScript module script but the
    server responded with a MIME type of text/html" instead of a plain ``404``
    naming the chunk.
    """
    if "text/html" in Headers(scope=scope).get("accept", ""):
        return True
    return "." not in route_path(scope).rsplit("/", 1)[-1]


def html_response(scope: Scope, markup: str) -> Response:
    """An HTML response, with ``HEAD`` answered as headers only.

    :class:`~starlette.responses.Response` sends its body whatever the method
    is; ``FileResponse`` is the one that checks. Emptying the body *after*
    construction keeps the ``Content-Length`` the matching ``GET`` would have
    reported, which is what ``HEAD`` is for.
    """
    response = HTMLResponse(markup)
    if scope.get("method") == "HEAD":
        response.body = b""
    return response


class SinglePageApp:
    """ASGI app serving a built bundle: the real file, or ``index.html``.

    A file that exists is served by :class:`~starlette.staticfiles.StaticFiles`
    (with its ETag/Last-Modified conditional handling and, importantly, its
    refusal to serve anything outside the bundle directory). A **navigation**
    that matches no file is a client-side route — ``/jobs/01J…``, or a refresh
    of one — so the entry document is returned and the app's own router takes it
    from there. Anything else that matches no file is a missing file, and keeps
    its ``404``; see :func:`is_navigation`.

    The entry document is served through
    :meth:`~starlette.staticfiles.StaticFiles.get_response` rather than a
    freshly built ``FileResponse``, which buys two things. Conditional requests
    work: a browser that already holds the document sends ``If-None-Match`` and
    gets ``304``, instead of the full body plus an ``ETag`` it can never spend.
    And if ``index.html`` is deleted from a running server, the answer is the
    documented ``404`` envelope rather than a ``FileNotFoundError`` and a
    ``500`` — the directory is inspected once at startup, so that state is
    reachable.

    Only a ``404`` becomes the fallback. A ``401`` from an unreadable file, or
    any other failure, is re-raised and reaches the application's error handlers
    as itself, because "your bundle directory is not readable" must not be
    reported as "here is the app".

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
            if exc.status_code != NOT_FOUND or not is_navigation(scope):
                raise
        response = await self._files.get_response(INDEX_FILENAME, scope)
        await response(scope, receive, send)


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


class FrontendFallback:
    """The router's ``default``: the frontend, or the answer there was before.

    Installed by :func:`mount_frontend` over the router's own ``not_found``,
    which it delegates to for everything it declines — so a request the frontend
    has no claim on gets byte-for-byte the response it got before this feature
    existed. See the module docstring for why ``default`` rather than a route.

    With a bundle it serves it. Without one it answers the **root URL only**
    with :func:`not_built_page`; a deep link with no bundle stays a ``404``,
    because pretending to be an app that does not exist is the dishonest answer.
    """

    def __init__(
        self, *, bundle: SinglePageApp | None, directory: Path, not_found: ASGIApp
    ) -> None:
        """Fall back to ``bundle`` (or ``directory``'s instructions), else ``not_found``."""
        self.bundle = bundle
        self.directory = directory
        self.not_found = not_found

    def claims(self, scope: Scope) -> bool:
        """Whether the frontend answers this request at all.

        Three refusals, each restoring the pre-feature behaviour exactly: a
        non-HTTP scope (the WebSocket), a method other than ``GET``/``HEAD``,
        and anything under :data:`API_PATH_PREFIX` in **any** spelling.
        """
        if scope["type"] != "http":
            return False
        if scope.get("method") not in FALLBACK_METHODS:
            return False
        return not is_api_path(route_path(scope))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the frontend, or hand back to the router's ``not_found``."""
        if not self.claims(scope):
            await self.not_found(scope, receive, send)
            return
        if self.bundle is not None:
            await self.bundle(scope, receive, send)
            return
        if normalized_path(route_path(scope)) == "/":
            await html_response(scope, not_built_page(self.directory))(scope, receive, send)
            return
        await self.not_found(scope, receive, send)


def mount_frontend(app: FastAPI, directory: Path) -> Path | None:
    """Serve the bundle in ``directory``, or explain how to build it.

    Installs a :class:`FrontendFallback` as the router's ``default``, wrapping
    whatever was there (Starlette's ``not_found``). Being the default rather
    than a route is what keeps ``/api/**``, method errors and ``redirect_slashes``
    working exactly as they did — see the module docstring.

    Returns:
        The ``index.html`` being served, or ``None`` when there is no bundle.

    The bundle is looked for **once, when the application is built**, not per
    request: that keeps a filesystem check off the hot path, and the answer
    cannot change for a running server in any way that matters — a build
    finishing later leaves the process serving what it found at startup. So
    building the frontend while the server is running means restarting it, which
    is what DEVELOPMENT.md says. (Deleting the bundle under a running server is
    the reachable other half of that, and answers ``404``; see
    :class:`SinglePageApp`.)
    """
    index = bundle_index(directory)
    app.router.default = FrontendFallback(
        bundle=SinglePageApp(directory) if index is not None else None,
        directory=directory,
        not_found=app.router.default,
    )
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
