"""Entry points and application wiring: settings really reach the server."""

import logging
import re
from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, cast

import httpx2
import pytest

from straticate import main
from straticate.config import Settings, get_settings
from straticate.inference import FakeSeparator, SeparatorRegistry
from straticate.models import ModelCatalog
from straticate.schemas import ComputeDevice
from straticate.system import DeviceDetector
from straticate.system import devices as devices_module


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


@contextmanager
def bare_root_logger() -> Generator[logging.Logger]:
    """Reproduce the uvicorn situation: a root logger nobody has configured.

    Uvicorn's ``LOGGING_CONFIG`` declares handlers for its own loggers and
    leaves the root one alone, which is the state the startup-logging tests
    need. The session's handlers are *detached* rather than removed, so
    ``logging.basicConfig(force=True)`` — which closes every handler it takes
    off the root logger — cannot reach pytest's own capture, and everything is
    put back afterwards.

    A context manager rather than a fixture because pytest attaches its
    ``caplog`` handler to the root logger for the *call* phase, i.e. after
    fixture setup has run: emptying the list has to happen inside the test
    body to have any effect.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    root.handlers = []
    try:
        yield root
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


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
    with bare_root_logger() as root:
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


# -- startup device probing -------------------------------------------------

PROBE_BACKEND = "imaginary"
PROBE_DEBUG_MESSAGE = "the imaginary runtime is not installed"
PROBE_FAILURE = "no such backend"

DEVICES_LOGGER = devices_module.__name__

FORMATTED_RECORD = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
    r"(?P<level>[A-Z]+) +(?P<logger>\S+) - (?P<message>.*)$",
    re.MULTILINE,
)
"""Matches one line rendered by :data:`straticate.logging._FORMAT`.

Asserting on the *rendered* line is the point: ``logging.lastResort`` prints a
bare ``%(message)s``, so a record that reaches it matches nothing here — which
is exactly the failure this section guards against.
"""


class LoggingProbe:
    """A device probe that logs where the real startup probes log.

    Two records, mirroring the two the finding is about:
    ``TorchCudaProbe``'s ``logger.debug("PyTorch is not installed; …")``
    (dropped entirely by ``lastResort``), and the
    ``logger.warning(…, exc_info=True)`` that
    :meth:`~straticate.system.DeviceDetector._probe_safely` emits for a probe
    that raises — the warning here is genuinely produced by that production
    code, by raising. Both go to the real
    :mod:`straticate.system.devices` logger, since where the records *land* is
    what is under test.
    """

    backend: str = PROBE_BACKEND

    def detect(self) -> Sequence[ComputeDevice]:
        logging.getLogger(DEVICES_LOGGER).debug(PROBE_DEBUG_MESSAGE)
        raise RuntimeError(PROBE_FAILURE)


class LoggingDetector(DeviceDetector):
    """The real detector, pre-loaded with :class:`LoggingProbe`."""

    def __init__(self) -> None:
        super().__init__(probes=[LoggingProbe()])


def use_a_logging_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``create_app``'s own detector the chatty one.

    Patching the class rather than assigning ``app.state.device_detector``
    afterwards is deliberate: the defect was *when* ``create_app`` probed, so
    the probe has to be in place before ``create_app`` is called.
    """
    monkeypatch.setattr(main, "DeviceDetector", LoggingDetector)


def formatted_records(stderr: str) -> list[tuple[str, str, str]]:
    """Every project-formatted ``(level, logger, message)`` line in ``stderr``."""
    return [
        (match["level"], match["logger"], match["message"])
        for match in FORMATTED_RECORD.finditer(stderr)
    ]


def assert_startup_probe_records(stderr: str) -> None:
    """Assert both startup device records arrived filtered and formatted."""
    records = formatted_records(stderr)
    assert ("DEBUG", DEVICES_LOGGER, PROBE_DEBUG_MESSAGE) in records, stderr
    assert any(
        level == "WARNING" and logger == DEVICES_LOGGER and PROBE_BACKEND in message
        for level, logger, message in records
    ), stderr
    # ``exc_info=True`` must reach the handler too, or a failing probe is
    # reported without saying why.
    assert f"RuntimeError: {PROBE_FAILURE}" in stderr, stderr


async def test_the_uvicorn_entry_path_formats_startup_device_records(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``uvicorn straticate.main:app``: the device probe logs *after* startup.

    ``create_app`` runs at **import** — before ``serve()``'s
    ``configure_logging`` and before the lifespan's
    ``ensure_logging_configured`` — so a probe run from there wrote to
    ``logging.lastResort``: the debug line dropped even under
    ``STRATICATE_LOG_LEVEL=DEBUG``, the warning printed bare. Probing moved
    into the lifespan, which is the earliest point that runs once per *running*
    application with logging already configured; ``create_app`` gained no
    global logging call, which is what 029 removed.

    The first assertion is the one that fails against the old code: building
    the application probed, and the warning landed on a bare stderr.
    """
    use_a_logging_probe(monkeypatch)

    with bare_root_logger() as root:
        app = main.create_app(Settings(log_level="DEBUG"))
        assert capsys.readouterr().err == "", "building the app must not probe, so must not log"
        assert not root.handlers, "building the app must still configure nothing"

        async with app.router.lifespan_context(app):
            pass

        assert_startup_probe_records(capsys.readouterr().err)


async def test_the_serve_entry_path_formats_startup_device_records(
    monkeypatch: pytest.MonkeyPatch,
    fresh_settings: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The same guarantee for ``python -m straticate`` (i.e. :func:`serve`).

    Deliberately **not** using ``quiet_serve``: the real ``configure_logging``
    is what is under test. ``bare_root_logger`` contains it — the session's own
    handlers are detached before ``basicConfig(force=True)`` can close them,
    and reattached afterwards.

    ``serve()`` configures logging and hands the module-level ``app`` to
    ``uvicorn.run``, which runs its lifespan; that last step is what the stub
    stands in for.
    """
    monkeypatch.setenv("STRATICATE_LOG_LEVEL", "DEBUG")
    use_a_logging_probe(monkeypatch)

    targets: list[Any] = []

    def capture(target: Any, **_kwargs: Any) -> None:
        targets.append(target)

    monkeypatch.setattr(main.uvicorn, "run", capture)

    with bare_root_logger():
        # The module-level ``app`` is built at import, with logging as bare as
        # it is here; ``serve()`` then configures logging and hands it over.
        served = main.create_app()
        monkeypatch.setattr(main, "app", served)
        assert capsys.readouterr().err == "", "building the app must not probe, so must not log"

        main.serve()
        assert targets == [served]

        async with served.router.lifespan_context(served):
            pass

        assert_startup_probe_records(capsys.readouterr().err)


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
