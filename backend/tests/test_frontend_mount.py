"""The built frontend is served from the API process, and never over the API.

Every application here is built with an **explicit** ``frontend_dist_dir``,
pointing either at a throwaway bundle written into ``tmp_path`` or at a
directory that does not exist. That is deliberate: the default is the
repository's ``frontend/dist``, which exists on the machine of anyone who has
run ``npm run build`` and does not on CI, so a test that relied on the default
would assert opposite things depending on who ran it. It also keeps these tests
fast and hermetic — a four-line ``index.html`` proves the routing, and a real
Vite build proves nothing extra about it.
"""

from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.types import Message

from straticate import frontend, main
from straticate.config import Settings, get_settings

INDEX_HTML = (
    "<!doctype html><html><body><div id=root></div>"
    '<script src="/assets/app.js"></script></body></html>'
)
ASSET_JS = "export const straticate = 1\n"
ASSET_PATH = "/assets/app.js"
MISSING_ASSET = "/assets/index-OLD-HASH.js"

API_HEALTH = "/api/v1/health"
API_UNKNOWN = "/api/v1/definitely-not-a-route"
WS_URL = "/api/v1/ws"
DEEP_LINK = "/jobs/01JQZ0000000000000000000"

HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
"""What every browser navigation sends, and no module ``import()`` does."""


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """A throwaway build output: an entry document and one hashed asset."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    # ``newline=""`` disables the platform newline translation that would make
    # the bytes on disk differ from the string asserted against on Windows.
    (dist / "index.html").write_text(INDEX_HTML, encoding="utf-8", newline="")
    (dist / "assets" / "app.js").write_text(ASSET_JS, encoding="utf-8", newline="")
    return dist


def build_app(directory: Path, tmp_path: Path) -> FastAPI:
    """An application serving ``directory`` as its frontend bundle."""
    return main.create_app(Settings(frontend_dist_dir=directory, data_dir=tmp_path / "data"))


@pytest.fixture
def served(bundle: Path, tmp_path: Path) -> FastAPI:
    """An application with the throwaway bundle mounted."""
    return build_app(bundle, tmp_path)


@pytest.fixture
def bare(tmp_path: Path) -> FastAPI:
    """An application whose bundle directory does not exist."""
    return build_app(tmp_path / "never-built", tmp_path)


async def get(app: FastAPI, path: str, **kwargs: Any) -> httpx2.Response:
    """One GET against ``app`` over in-process ASGI transport."""
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, **kwargs)


async def request(app: FastAPI, method: str, path: str, **kwargs: Any) -> httpx2.Response:
    """One request of any method against ``app``."""
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


class RawResponse:
    """The status, headers and body of a response collected off the ASGI wire."""

    def __init__(self, status_code: int, headers: dict[str, str], content: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode()


async def raw_get(app: FastAPI, path: str) -> RawResponse:
    """One GET with the path **exactly as written**, bypassing the HTTP client.

    An httpx client cannot express the spellings the API guard exists to
    reject: it resolves ``/./x`` and ``/a/../x`` while building the URL, and it
    reads a leading ``//`` as a protocol-relative *authority* (so
    ``//api/v1/nope`` becomes host ``api``, path ``/v1/nope``). A socket-level
    client sends all of them through untouched, and so does this: the scope is
    what a server hands the application, with the raw path in it.
    """
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }
    await app(scope, receive, send)

    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    headers = {key.decode().lower(): value.decode() for key, value in start["headers"]}
    return RawResponse(start["status"], headers, body)


def assert_error_envelope(
    response: httpx2.Response | RawResponse, status_code: int, code: str
) -> None:
    """The response is the documented JSON error envelope, not a page."""
    assert response.status_code == status_code, response.text
    assert response.headers["content-type"].startswith("application/json"), response.text
    assert '"code":"' + code + '"' in response.text.replace(" ", ""), response.text


# -- the bundle is served ---------------------------------------------------


async def test_the_root_url_serves_the_built_app(served: FastAPI) -> None:
    response = await get(served, "/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == INDEX_HTML


async def test_hashed_assets_are_served_from_the_bundle(served: FastAPI) -> None:
    response = await get(served, ASSET_PATH)

    assert response.status_code == 200
    assert response.text == ASSET_JS
    assert "javascript" in response.headers["content-type"]


async def test_a_deep_link_returns_the_app_rather_than_a_404(served: FastAPI) -> None:
    """A refresh on a client-side route is a page load, not a missing resource."""
    response = await get(served, DEEP_LINK)

    assert response.status_code == 200
    assert response.text == INDEX_HTML


async def test_head_is_answered_like_get(served: FastAPI) -> None:
    response = await request(served, "HEAD", DEEP_LINK)

    assert response.status_code == 200


async def test_the_fallback_cannot_serve_anything_outside_the_bundle(
    served: FastAPI, tmp_path: Path
) -> None:
    """A traversal attempt gets the app, never the file it was reaching for."""
    (tmp_path / "secret.txt").write_text("not yours", encoding="utf-8")

    response = await get(served, "/%2e%2e/secret.txt", headers={"accept": HTML_ACCEPT})

    assert response.status_code == 200
    assert response.text == INDEX_HTML
    assert "not yours" not in response.text


# -- a missing *file* is not a deep link ------------------------------------


async def test_a_missing_bundle_file_is_a_404_not_the_entry_document(served: FastAPI) -> None:
    """The stale-chunk trap.

    A tab left open across a rebuild ``import()``s a hash that no longer
    exists. Answering that with ``index.html`` makes the browser report
    "expected a JavaScript module script but the server responded with a MIME
    type of text/html", which names neither the missing chunk nor the stale
    tab. It has to stay a ``404``.
    """
    assert_error_envelope(await get(served, MISSING_ASSET), 404, "not_found")


async def test_a_missing_file_stays_a_404_for_every_asset_kind(served: FastAPI) -> None:
    for path in ("/assets/index-OLD.css", "/favicon.ico", "/manifest.webmanifest"):
        assert_error_envelope(await get(served, path), 404, "not_found")


async def test_a_navigation_to_a_path_that_looks_like_a_file_still_gets_the_app(
    served: FastAPI,
) -> None:
    """The ``Accept`` header rescues the case the extension heuristic misreads."""
    response = await get(served, "/reports/2026.05", headers={"accept": HTML_ACCEPT})

    assert response.status_code == 200
    assert response.text == INDEX_HTML


async def test_a_module_import_of_a_missing_chunk_is_never_html(served: FastAPI) -> None:
    """What a browser actually sends for ``import()``: ``Accept: */*``."""
    response = await get(served, MISSING_ASSET, headers={"accept": "*/*"})

    assert response.status_code == 404
    assert "text/html" not in response.headers["content-type"]


# -- and never shadows the API ----------------------------------------------


async def test_the_api_still_answers_with_a_bundle_mounted(served: FastAPI) -> None:
    response = await get(served, API_HEALTH)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_an_unknown_api_path_is_still_the_json_envelope(served: FastAPI) -> None:
    """The failure this feature is most able to cause, pinned.

    A catch-all fallback would answer this with ``200 text/html``, and every
    client that parses an error would break silently.
    """
    assert_error_envelope(await get(served, API_UNKNOWN), 404, "not_found")


async def test_an_unknown_api_path_with_any_method_is_still_the_json_envelope(
    served: FastAPI,
) -> None:
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        assert_error_envelope(await request(served, method, API_UNKNOWN), 404, "not_found")


async def test_a_bad_method_on_a_real_api_route_is_still_405(served: FastAPI) -> None:
    """The fallback must not turn a routed path into a full match.

    Starlette dispatches the first *full* match and only then falls back to a
    recorded partial one, so a fallback route that matched every method would
    answer this with ``index.html`` instead of letting ``/health`` produce its
    ``405``. As the router's ``default`` it is never even consulted here.
    """
    response = await request(served, "POST", API_HEALTH)

    assert_error_envelope(response, 405, "method_not_allowed")


async def test_the_whole_api_namespace_is_reserved_not_just_the_version(
    served: FastAPI,
) -> None:
    """``/api/**``, not only ``/api/v1/**`` — including ``/api`` itself."""
    assert_error_envelope(await get(served, "/api"), 404, "not_found")
    assert_error_envelope(await get(served, "/api/v2/models"), 404, "not_found")


@pytest.mark.parametrize(
    "path",
    [
        "//api/v1/nope",
        "///api/v1/nope",
        "/api//v1/nope",
        "/./api/v1/nope",
        "/somewhere/../api/v1/nope",
        "//api",
        "/api/v1/nope/",
    ],
)
async def test_every_spelling_of_an_api_path_is_reserved(served: FastAPI, path: str) -> None:
    """A guard that only reserves the canonical spelling reserves nothing.

    A client building ``f"{base}/api/v1/jobs"`` with a ``base`` that ends in a
    slash emits ``//api/v1/jobs``; browsers send it through uncollapsed and no
    route matches it, so before the path was normalized it reached the frontend
    and an API call came back ``200 text/html``.
    """
    assert_error_envelope(await raw_get(served, path), 404, "not_found")


async def test_a_non_api_path_with_the_same_oddities_still_gets_the_app(
    served: FastAPI,
) -> None:
    """Normalizing is for the guard; it must not make the frontend pickier."""
    response = await raw_get(served, "//jobs/01JQZ")

    assert response.status_code == 200
    assert response.text == INDEX_HTML


def test_the_api_prefix_lives_under_the_reserved_namespace() -> None:
    """If the API ever moves out from under ``/api``, the guard must move too."""
    assert main.API_PREFIX.startswith(f"{frontend.API_PATH_PREFIX}/")


def test_normalized_path_collapses_every_spelling_to_one() -> None:
    """``posixpath.normpath`` alone keeps exactly two leading slashes (POSIX)."""
    assert frontend.normalized_path("//api/v1/x") == "/api/v1/x"
    assert frontend.normalized_path("/api//v1/x") == "/api/v1/x"
    assert frontend.normalized_path("/./api/v1/x") == "/api/v1/x"
    assert frontend.normalized_path("/a/../api/v1/x") == "/api/v1/x"
    assert frontend.normalized_path("//") == "/"
    assert frontend.normalized_path("") == "/"


async def test_the_openapi_document_and_docs_are_not_shadowed(served: FastAPI) -> None:
    document = await get(served, "/openapi.json")
    docs = await get(served, "/docs")

    assert document.status_code == 200
    assert document.json()["info"]["title"] == "Straticate"
    assert docs.status_code == 200
    assert "swagger" in docs.text.lower()


@pytest.mark.parametrize("path", ["/docs/", "/openapi.json/", "/redoc/"])
async def test_a_trailing_slash_still_redirects_to_the_real_route(
    served: FastAPI, path: str
) -> None:
    """``redirect_slashes`` runs *before* the router's default, and must still run.

    A catch-all route would full-match these and serve the app, silently making
    the redirect dead code; as the ``default`` the frontend is only reached
    after the redirect has had its chance.
    """
    response = await get(served, path)

    assert response.status_code == 307, response.text
    assert response.headers["location"].endswith(path.rstrip("/"))


def test_the_mount_adds_no_path_to_the_published_contract(served: FastAPI, bare: FastAPI) -> None:
    """Frontend types are generated from this document; serving HTML is not API."""
    assert served.openapi()["paths"] == bare.openapi()["paths"]
    assert "/" not in served.openapi()["paths"]


def test_the_mount_adds_no_route_at_all(bundle: Path, tmp_path: Path) -> None:
    """It is the router's ``default``, which is what leaves the table untouched.

    Every property this feature has to preserve — the ``405`` from a partial
    match, ``redirect_slashes``, an unknown API path staying an envelope —
    follows from the routing table being exactly what it was.
    """
    app = build_app(tmp_path / "never-built", tmp_path)
    routes_before = list(app.routes)
    default_before = app.router.default

    index = frontend.mount_frontend(app, bundle)

    assert index == bundle / "index.html"
    assert list(app.routes) == routes_before
    assert app.router.default is not default_before
    assert isinstance(app.router.default, frontend.FrontendFallback)


def test_the_websocket_is_untouched(bundle: Path, tmp_path: Path) -> None:
    """The one real-time channel still connects with the frontend mounted."""
    with TestClient(build_app(bundle, tmp_path)) as client, client.websocket_connect(WS_URL):
        pass


# -- conditional requests and a bundle that disappears ----------------------


async def test_a_deep_link_honours_if_none_match(served: FastAPI) -> None:
    """The entry document is served by ``StaticFiles``, so its ETag is spendable.

    Built as a fresh ``FileResponse`` instead, every refresh would return the
    whole document with an ``ETag`` the browser could never cash in.
    """
    first = await get(served, DEEP_LINK)
    etag = first.headers["etag"]

    second = await get(served, DEEP_LINK, headers={"if-none-match": etag})

    assert second.status_code == 304
    assert second.content == b""


async def test_an_index_deleted_under_a_running_server_is_a_404_not_a_500(
    bundle: Path, tmp_path: Path
) -> None:
    """The directory is inspected once at startup, so this state is reachable."""
    app = build_app(bundle, tmp_path)
    (bundle / "index.html").unlink()

    assert_error_envelope(await get(app, DEEP_LINK), 404, "not_found")


# -- no bundle is a documented state, not a failure -------------------------


async def test_without_a_bundle_the_api_works_normally(bare: FastAPI) -> None:
    """How the backend suite, the E2E tier and a fresh checkout all run."""
    response = await get(bare, API_HEALTH)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert_error_envelope(await get(bare, API_UNKNOWN), 404, "not_found")


async def test_without_a_bundle_the_root_url_says_what_to_do(bare: FastAPI) -> None:
    response = await get(bare, "/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "npm run build" in response.text
    assert "STRATICATE_FRONTEND_DIST_DIR" in response.text
    assert "never-built" in response.text


async def test_head_on_the_not_built_page_sends_headers_only(bare: FastAPI) -> None:
    response = await request(bare, "HEAD", "/")

    assert response.status_code == 200
    assert response.content == b""
    assert int(response.headers["content-length"]) > 0


async def test_without_a_bundle_a_deep_link_is_still_a_404(bare: FastAPI) -> None:
    """Only the root URL explains itself; nothing else pretends to be the app."""
    assert_error_envelope(await get(bare, DEEP_LINK), 404, "not_found")


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
async def test_the_root_url_answers_the_same_way_in_both_modes(
    served: FastAPI, bare: FastAPI, method: str
) -> None:
    """A client must not get a different status because someone ran ``npm run build``.

    ``POST /`` was ``404`` before this feature and stays ``404`` in both modes:
    the frontend claims only ``GET``/``HEAD``, so a write to the root is handed
    straight back to the router's ``not_found``.
    """
    assert_error_envelope(await request(served, method, "/"), 404, "not_found")
    assert_error_envelope(await request(bare, method, "/"), 404, "not_found")


async def test_a_directory_with_no_index_html_counts_as_no_bundle(tmp_path: Path) -> None:
    """What an interrupted build leaves behind is not something to serve."""
    empty = tmp_path / "dist"
    (empty / "assets").mkdir(parents=True)

    response = await get(build_app(empty, tmp_path), "/")

    assert response.status_code == 200
    assert "npm run build" in response.text


def test_bundle_index_reports_what_it_found(bundle: Path, tmp_path: Path) -> None:
    assert frontend.bundle_index(bundle) == bundle / "index.html"
    assert frontend.bundle_index(tmp_path / "never-built") is None


def test_startup_logs_which_mode_the_server_is_in(
    bundle: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One line, at startup, saying whether this process serves the app."""
    with caplog.at_level("INFO", logger=frontend.__name__):
        frontend.log_bundle_state(build_app(bundle, tmp_path))
        frontend.log_bundle_state(build_app(tmp_path / "never-built", tmp_path))

    served_message, bare_message = (record.getMessage() for record in caplog.records)
    assert str(bundle) in served_message
    assert "No frontend bundle" in bare_message
    assert "npm run build" in bare_message


# -- the bundle path is configurable and working-directory independent ------


def test_the_default_bundle_path_is_the_repository_build_output() -> None:
    default = Settings().frontend_dist_dir

    assert default.is_absolute()
    assert default.name == "dist"
    assert default.parent.name == "frontend"
    assert (default.parent / "package.json").is_file(), default


def test_the_bundle_path_does_not_depend_on_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``uvicorn`` started from anywhere must serve the same app.

    The trap this closes is a relative ``frontend/dist``: it would resolve
    against the process working directory, so the app would appear from
    ``backend/`` and vanish from ``/``.
    """
    from_here = Settings().frontend_dist_dir
    monkeypatch.chdir(tmp_path)

    # The `chdir` has to have actually happened, or the assertion below passes
    # for the wrong reason.
    assert Path.cwd().resolve() == tmp_path.resolve()
    assert Settings().frontend_dist_dir == from_here


def test_the_bundle_path_is_configurable_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STRATICATE_FRONTEND_DIST_DIR", str(tmp_path / "elsewhere"))
    get_settings.cache_clear()
    try:
        assert get_settings().frontend_dist_dir == tmp_path / "elsewhere"
    finally:
        get_settings.cache_clear()


async def test_an_application_serves_the_bundle_it_was_configured_with(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit settings govern, and the working directory does not.

    ``create_app(Settings(...))`` is the whole configuration path — the same
    property ``ffmpeg_timeout_seconds`` has — so a server started from an
    unrelated directory serves exactly the bundle it was pointed at.
    """
    app = build_app(bundle, tmp_path)
    elsewhere = tmp_path.parent
    monkeypatch.chdir(elsewhere)
    assert Path.cwd().resolve() == elsewhere.resolve()

    response = await get(app, "/")

    assert response.text == INDEX_HTML
