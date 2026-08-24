"""Entry points and application wiring: settings really reach the server."""

import logging
from collections.abc import Iterator
from typing import Any, cast

import httpx2
import pytest

from straticate import main
from straticate.config import Settings, get_settings
from straticate.inference import FakeSeparator, SeparatorRegistry
from straticate.models import ModelCatalog


def _no_server(*_args: Any, **_kwargs: Any) -> None:
    """Stand in for ``uvicorn.run`` — serve() must be callable without a socket."""


@pytest.fixture
def fresh_settings() -> Iterator[None]:
    """Drop the cached process-wide settings around a test that sets env vars."""
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture
def quiet_serve(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Neutralise serve()'s two process-global acts for the duration of a test.

    ``serve()`` legitimately calls ``configure_logging`` — i.e.
    ``logging.basicConfig(force=True)`` — and starts a server. A test that
    stubs only ``uvicorn.run`` therefore strips every root handler mid-session
    and installs a ``StreamHandler`` that outlives it, which is precisely the
    global side effect this feature exists to remove. Any test that calls
    ``serve()`` uses this fixture and asserts against the recorded levels
    instead.
    """
    configured: list[str] = []
    monkeypatch.setattr(main, "configure_logging", configured.append)
    monkeypatch.setattr(main.uvicorn, "run", _no_server)
    yield configured


def test_serve_binds_the_configured_host_and_port(
    monkeypatch: pytest.MonkeyPatch, fresh_settings: None, quiet_serve: list[str]
) -> None:
    monkeypatch.setenv("STRATICATE_HOST", "0.0.0.0")
    monkeypatch.setenv("STRATICATE_PORT", "9123")
    captured: dict[str, Any] = {}

    def fake_run(target: Any, **kwargs: Any) -> None:
        captured["target"] = target
        captured.update(kwargs)

    monkeypatch.setattr(main.uvicorn, "run", fake_run)

    main.serve()

    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9123
    # The already-built application object, not an import string that would
    # make uvicorn construct a second one.
    assert captured["target"] is main.app


def test_serve_defaults_to_the_documented_loopback_port(
    monkeypatch: pytest.MonkeyPatch, fresh_settings: None, quiet_serve: list[str]
) -> None:
    monkeypatch.delenv("STRATICATE_HOST", raising=False)
    monkeypatch.delenv("STRATICATE_PORT", raising=False)
    captured: dict[str, Any] = {}

    def fake_run(target: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(main.uvicorn, "run", fake_run)

    main.serve()

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8000
    # Our own root configuration must survive: uvicorn's dictConfig would
    # replace it.
    assert captured["log_config"] is None


def test_main_module_calls_serve() -> None:
    # ``python -m straticate`` must be the same code path as serve() itself.
    from straticate import __main__

    assert __main__.serve is main.serve


async def test_cors_origins_come_from_settings() -> None:
    settings = Settings(cors_origins=["https://studio.example"])
    app = main.create_app(settings)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        allowed = await client.get("/api/v1/health", headers={"Origin": "https://studio.example"})
        rejected = await client.get("/api/v1/health", headers={"Origin": "https://evil.example"})

    assert allowed.headers["access-control-allow-origin"] == "https://studio.example"
    assert "access-control-allow-origin" not in rejected.headers


def test_settings_read_cors_origins_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, fresh_settings: None
) -> None:
    monkeypatch.setenv("STRATICATE_CORS_ORIGINS", '["https://one.example","https://two.example"]')
    assert get_settings().cors_origins == ["https://one.example", "https://two.example"]


def test_named_origins_keep_credentials_enabled() -> None:
    assert main.allows_credentials(["https://studio.example"]) is True


def test_a_wildcard_origin_disables_credentials() -> None:
    """``"*"`` plus credentials is allow-any-origin-with-credentials.

    Starlette treats ``"*"`` as allow-all, and with ``allow_credentials=True``
    it echoes the caller's own ``Origin`` back beside
    ``Access-Control-Allow-Credentials: true`` — so every origin could read
    credentialed responses. Now that the allowlist is configurable (and
    documented as a JSON array, which invites ``'["*"]'``), the flag follows the
    list.
    """
    assert main.allows_credentials([main.CORS_WILDCARD]) is False
    assert main.allows_credentials(["https://studio.example", main.CORS_WILDCARD]) is False


async def test_a_wildcard_allowlist_does_not_echo_the_origin_with_credentials() -> None:
    app = main.create_app(Settings(cors_origins=[main.CORS_WILDCARD]))
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health", headers={"Origin": "https://evil.example"})

    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers


async def test_an_explicit_allowlist_still_allows_credentials() -> None:
    app = main.create_app(Settings(cors_origins=["https://studio.example"]))
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health", headers={"Origin": "https://studio.example"})

    assert response.headers["access-control-allow-origin"] == "https://studio.example"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_the_apps_settings_reach_the_separator_registry() -> None:
    """``create_app(Settings(...))`` governs FFmpeg, not just the environment."""
    app = main.create_app(Settings(ffmpeg_timeout_seconds=1.5))
    catalog = cast(ModelCatalog, app.state.model_catalog)
    registry = cast(SeparatorRegistry, app.state.separator_registry)
    separator = registry.get(catalog.list_models()[0])

    assert isinstance(separator, FakeSeparator)
    assert separator.ffmpeg_timeout_seconds == 1.5


def test_serve_configures_logging_but_create_app_does_not(
    fresh_settings: None, quiet_serve: list[str]
) -> None:
    main.create_app()
    assert quiet_serve == [], "building an application must not touch global logging"

    main.serve()
    assert quiet_serve == ["INFO"]


def test_calling_serve_in_a_test_leaves_root_logging_alone(
    fresh_settings: None, quiet_serve: list[str]
) -> None:
    """A test that exercises ``serve()`` must not reconfigure the session's logging.

    ``serve()`` legitimately calls ``logging.basicConfig(force=True)``, which
    strips every root handler and installs a ``StreamHandler`` that outlives
    the test. Stubbing only ``uvicorn.run`` therefore reintroduces, inside the
    suite, exactly the global side effect this feature removed from
    ``create_app``. The ``quiet_serve`` fixture is the guard; this test fails
    without it.
    """
    root = logging.getLogger()
    before = list(root.handlers)

    main.serve()

    assert list(root.handlers) == before
    assert quiet_serve == ["INFO"], "serve() must still configure logging for real"


async def test_the_uvicorn_entry_path_configures_application_logging() -> None:
    """The documented ``uvicorn straticate.main:app`` command must log properly.

    Uvicorn's ``LOGGING_CONFIG`` declares handlers for ``uvicorn``,
    ``uvicorn.error`` and ``uvicorn.access`` only — never the root logger — so
    with nothing else configuring it every ``straticate.*`` record falls
    through to ``logging.lastResort``: WARNING and above only, bare message, no
    timestamp, no logger name, and ``STRATICATE_LOG_LEVEL=DEBUG`` silently
    doing nothing.

    Startup (not import, not ``create_app``) is where that is fixed, so this
    reproduces the uvicorn situation — a bare root logger — and then runs the
    application lifespan.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    root.handlers = []
    try:
        app = main.create_app(Settings(log_level="DEBUG"))
        assert not root.handlers, "building the app must still configure nothing"

        async with app.router.lifespan_context(app):
            assert root.handlers, "startup must configure the root logger"
            formatter = root.handlers[0].formatter
            assert formatter is not None
            rendered = formatter.format(
                logging.LogRecord(
                    "straticate.jobs.hub",
                    logging.DEBUG,
                    __file__,
                    1,
                    "dropping a client",
                    None,
                    None,
                )
            )
            assert "straticate.jobs.hub" in rendered, rendered
            assert "DEBUG" in rendered, rendered
            assert rendered.endswith("dropping a client"), rendered
            # STRATICATE_LOG_LEVEL=DEBUG has to actually enable DEBUG records.
            assert logging.getLogger("straticate.jobs.hub").isEnabledFor(logging.DEBUG)
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


async def test_startup_never_overrides_an_existing_logging_configuration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Startup configures logging *non-destructively* — caplog survives it."""
    app = main.create_app()
    with caplog.at_level(logging.WARNING, logger="straticate.test"):
        async with app.router.lifespan_context(app):
            logging.getLogger("straticate.test").warning("still captured")

    assert [record.message for record in caplog.records] == ["still captured"]


def test_caplog_still_captures_after_create_app(caplog: pytest.LogCaptureFixture) -> None:
    # configure_logging() calls logging.basicConfig(force=True), which removes
    # every root handler — caplog's included. Building an application must
    # therefore never call it: this test fails outright (zero records) if the
    # call moves back into create_app().
    with caplog.at_level(logging.WARNING, logger="straticate.test"):
        main.create_app()
        logging.getLogger("straticate.test").warning("still captured")

    assert [record.message for record in caplog.records] == ["still captured"]


def test_module_level_app_is_an_application_instance() -> None:
    # ``uvicorn straticate.main:app`` is documented in DEVELOPMENT.md and used
    # by CI; the instance is deliberate, not an accident of import.
    assert main.app.title == "Straticate"


def test_ffmpeg_timeout_is_configurable_and_must_be_positive(
    monkeypatch: pytest.MonkeyPatch, fresh_settings: None
) -> None:
    monkeypatch.setenv("STRATICATE_FFMPEG_TIMEOUT_SECONDS", "12.5")
    assert get_settings().ffmpeg_timeout_seconds == 12.5
    with pytest.raises(ValueError):
        Settings(ffmpeg_timeout_seconds=0)
