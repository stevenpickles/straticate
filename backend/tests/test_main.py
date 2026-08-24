"""Entry points and application wiring: settings really reach the server."""

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from straticate import main
from straticate.config import Settings, get_settings


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

    assert captured == {"host": "127.0.0.1", "port": 8000}


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


def test_ffmpeg_timeout_is_configurable_and_must_be_positive(
    monkeypatch: pytest.MonkeyPatch, fresh_settings: None
) -> None:
    monkeypatch.setenv("STRATICATE_FFMPEG_TIMEOUT_SECONDS", "12.5")
    assert get_settings().ffmpeg_timeout_seconds == 12.5
    with pytest.raises(ValueError):
        Settings(ffmpeg_timeout_seconds=0)
