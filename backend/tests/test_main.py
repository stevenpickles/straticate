"""Entry points and application wiring: settings really reach the server."""

import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from straticate import main
from straticate.config import Settings, get_settings


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


def test_serve_binds_the_configured_host_and_port(
    monkeypatch: pytest.MonkeyPatch, fresh_settings: None
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
    monkeypatch: pytest.MonkeyPatch, fresh_settings: None
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
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        allowed = await client.get("/api/v1/health", headers={"Origin": "https://studio.example"})
        rejected = await client.get("/api/v1/health", headers={"Origin": "https://evil.example"})

    assert allowed.headers["access-control-allow-origin"] == "https://studio.example"
    assert "access-control-allow-origin" not in rejected.headers


def test_settings_read_cors_origins_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, fresh_settings: None
) -> None:
    monkeypatch.setenv("STRATICATE_CORS_ORIGINS", '["https://one.example","https://two.example"]')
    assert get_settings().cors_origins == ["https://one.example", "https://two.example"]


def test_serve_configures_logging_but_create_app_does_not(
    monkeypatch: pytest.MonkeyPatch, fresh_settings: None
) -> None:
    configured: list[str] = []
    monkeypatch.setattr(main, "configure_logging", configured.append)
    monkeypatch.setattr(main.uvicorn, "run", _no_server)

    main.create_app()
    assert configured == [], "building an application must not touch global logging"

    main.serve()
    assert configured == ["INFO"]


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
