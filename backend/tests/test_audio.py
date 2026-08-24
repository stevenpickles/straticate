"""Tests for the /api/v1/audio endpoints (upload, fetch, delete)."""

import io
import subprocess
import wave
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn, cast

import httpx2
import pytest
from fastapi import FastAPI

from straticate.audio import ffmpeg as ffmpeg_module
from straticate.audio import probe as probe_module
from straticate.audio.ffmpeg import FFmpegTimeout
from straticate.config import Settings
from straticate.main import create_app

AUDIO_URL = "/api/v1/audio"


def make_wav_bytes(seconds: float = 1.0, channels: int = 2, sample_rate: int = 44100) -> bytes:
    """Generate an in-memory PCM16 WAV file of silence."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(b"\x00\x00" * int(seconds * sample_rate) * channels)
    return buffer.getvalue()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings whose data_dir lives inside the test's tmp_path."""
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """Override the shared app fixture with tmp_path-backed settings."""
    return create_app(settings)


async def upload(client: httpx2.AsyncClient, filename: str, content: bytes) -> httpx2.Response:
    """POST ``content`` as a multipart upload named ``filename``."""
    return await client.post(AUDIO_URL, files={"file": (filename, content, "audio/wav")})


async def test_upload_returns_probed_metadata(
    client: httpx2.AsyncClient, settings: Settings
) -> None:
    wav = make_wav_bytes()
    response = await upload(client, "song.wav", wav)
    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "song.wav"
    assert body["size_bytes"] == len(wav)
    assert body["uploaded_at"].endswith("Z") or "+" in body["uploaded_at"]

    metadata = body["metadata"]
    assert metadata["duration_seconds"] == pytest.approx(1.0, abs=0.05)
    assert metadata["channels"] == 2
    assert metadata["sample_rate_hz"] == 44100
    assert metadata["container"] == "wav"
    assert metadata["codec"].startswith("pcm")
    assert metadata["bit_depth"] == 16

    stored = settings.data_dir / "audio" / body["id"] / "original.wav"
    assert stored.is_file()
    assert stored.stat().st_size == len(wav)


async def test_upload_text_file_rejected(client: httpx2.AsyncClient, settings: Settings) -> None:
    response = await upload(client, "notes.txt", b"this is not audio at all\n")
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "audio_not_decodable"
    assert (
        not any((settings.data_dir / "audio").glob("*"))
        or not (settings.data_dir / "audio").exists()
    )


async def test_upload_over_size_limit_rejected(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", max_upload_bytes=1024)
    app = create_app(settings)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await upload(client, "song.wav", make_wav_bytes())
    assert response.status_code == 413
    body = response.json()
    assert body["error"]["code"] == "audio_too_large"
    assert body["error"]["detail"]["max_upload_bytes"] == 1024
    audio_root = settings.data_dir / "audio"
    assert not audio_root.exists() or not any(audio_root.iterdir())


async def test_probe_timeout_is_its_own_error_not_not_decodable(
    client: httpx2.AsyncClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged ffprobe is a 504 with its own code — never a wait, never a 500.

    The runner is stubbed, so nothing here waits for a real timeout. Reusing
    ``audio_not_decodable`` would tell the user their file is broken; ffprobe
    never said that, it just ran out of time.
    """

    def wedged(command: Sequence[str], **_kwargs: object) -> NoReturn:
        raise FFmpegTimeout(command[0], 600.0)

    monkeypatch.setattr(probe_module, "run_ffmpeg", wedged)

    response = await upload(client, "song.wav", make_wav_bytes())

    assert response.status_code == 504
    error = response.json()["error"]
    assert error["code"] == "audio_probe_timed_out"
    assert error["detail"] == {"timeout_seconds": 600.0}
    # The rejected upload leaves nothing behind, exactly like the other paths.
    audio_root = settings.data_dir / "audio"
    assert not audio_root.exists() or not any(audio_root.iterdir())


async def test_the_apps_settings_govern_the_probe_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``create_app(Settings(...))`` must reach ffprobe, not just the environment.

    Every other setting arrives through ``app.state.settings``, and
    ``create_app(settings)`` is a documented path every test fixture uses — so
    a runner that read the process-global settings instead would silently
    ignore the bound this application was built with. The real
    ``subprocess.run`` is stubbed here, so nothing waits.
    """
    recorded: list[float] = []

    def record_and_wedge(command: Sequence[str], **kwargs: Any) -> NoReturn:
        timeout = cast(float, kwargs["timeout"])
        recorded.append(timeout)
        raise subprocess.TimeoutExpired(list(command), timeout)

    monkeypatch.setattr(ffmpeg_module.subprocess, "run", record_and_wedge)

    app = create_app(Settings(data_dir=tmp_path / "data", ffmpeg_timeout_seconds=1.5))
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await upload(client, "song.wav", make_wav_bytes())

    assert response.status_code == 504
    assert response.json()["error"]["detail"] == {"timeout_seconds": 1.5}
    assert recorded == [1.5], "the upload must probe with the application's own bound"


async def test_get_returns_uploaded_record(client: httpx2.AsyncClient) -> None:
    uploaded = (await upload(client, "song.wav", make_wav_bytes())).json()
    response = await client.get(f"{AUDIO_URL}/{uploaded['id']}")
    assert response.status_code == 200
    assert response.json() == uploaded


async def test_get_unknown_id_404(client: httpx2.AsyncClient) -> None:
    response = await client.get(f"{AUDIO_URL}/01UNKNOWNULID0000000000000")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "audio_not_found"


async def test_delete_unknown_id_404(client: httpx2.AsyncClient) -> None:
    response = await client.delete(f"{AUDIO_URL}/01UNKNOWNULID0000000000000")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "audio_not_found"


async def test_delete_removes_record_and_files(
    client: httpx2.AsyncClient, settings: Settings
) -> None:
    uploaded = (await upload(client, "song.wav", make_wav_bytes())).json()
    audio_dir = settings.data_dir / "audio" / uploaded["id"]
    assert audio_dir.is_dir()

    response = await client.delete(f"{AUDIO_URL}/{uploaded['id']}")
    assert response.status_code == 204
    assert not audio_dir.exists()
    assert (await client.get(f"{AUDIO_URL}/{uploaded['id']}")).status_code == 404


async def test_lying_extension_is_ignored(client: httpx2.AsyncClient) -> None:
    response = await upload(client, "song.mp3", make_wav_bytes())
    assert response.status_code == 201
    metadata = response.json()["metadata"]
    assert metadata["container"] == "wav"
    assert metadata["codec"].startswith("pcm")
