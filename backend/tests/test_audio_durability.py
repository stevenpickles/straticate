"""Feature 056: uploaded-audio records survive a backend restart.

Every test here builds **two** applications over the **same** ``tmp_path``
data directory — ``app1`` "before the restart", ``app2`` "after" — with
``app1``'s lifespan torn down (job manager, hub, sampler all stopped, client
closed) before ``app2`` is built, so nothing but the filesystem carries state
across the boundary. Both run their real lifespan
(``async with app.router.lifespan_context(app)``), because that is where
:meth:`~straticate.audio.AudioStore.load` runs — see its docstring for why it
cannot run from ``AudioStore.__init__``, which would make the "restart" here
indistinguishable from just reusing the same store.
"""

import asyncio
import io
import json
import logging
import os
import wave
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import httpx2
import pytest
from fastapi import FastAPI

from straticate.audio import AudioStore
from straticate.audio.storage import SIDECAR_FILENAME
from straticate.config import Settings
from straticate.inference import FAKE_ARCHITECTURE, SeparatorRegistry, fake_separator_builder
from straticate.jobs import JobEvent, JobManager
from straticate.main import create_app
from straticate.schemas import AudioFile, AudioMetadata
from straticate.schemas.events import JobCancelledEvent, JobCompletedEvent, JobFailedEvent
from tests.conftest import fake_quality_id

AUDIO_URL = "/api/v1/audio"
JOBS_URL = "/api/v1/jobs"
WAIT_TIMEOUT = 30.0


def make_wav_bytes(seconds: float = 1.0, channels: int = 2, sample_rate: int = 44100) -> bytes:
    """Generate an in-memory PCM16 WAV file of silence (mirrors test_audio.py)."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(b"\x00\x00" * int(seconds * sample_rate) * channels)
    return buffer.getvalue()


def fast_registry() -> SeparatorRegistry:
    """The real fake separator with every simulated delay removed."""
    return SeparatorRegistry(
        {
            FAKE_ARCHITECTURE: fake_separator_builder(
                chunk_seconds=0.2,
                chunk_delay_seconds=0.0,
                model_load_seconds=0.0,
            )
        }
    )


def build_app(data_dir: Path) -> FastAPI:
    """An application isolated to ``data_dir``, with the fast fake separator."""
    app = create_app(Settings(data_dir=data_dir))
    app.state.separator_registry = fast_registry()
    return app


class EventRecorder:
    """Sync manager listener that records events and lets tests await them.

    Mirrors ``test_api_jobs.py``'s helper of the same name, trimmed to what
    this file needs.
    """

    def __init__(self) -> None:
        self.events: list[JobEvent] = []
        self._changed = asyncio.Event()

    def __call__(self, event: JobEvent) -> None:
        self.events.append(event)
        self._changed.set()

    async def wait_for(self, predicate: Callable[[JobEvent], bool]) -> JobEvent:
        index = 0
        while True:
            while index < len(self.events):
                event = self.events[index]
                index += 1
                if predicate(event):
                    return event
            self._changed.clear()
            await asyncio.wait_for(self._changed.wait(), timeout=WAIT_TIMEOUT)

    async def wait_for_terminal(self, job_id: str) -> JobEvent:
        return await self.wait_for(
            lambda event: (
                event.job_id == job_id
                and isinstance(event, JobCompletedEvent | JobCancelledEvent | JobFailedEvent)
            )
        )


class RunningApp:
    """Async context manager: one app, its lifespan running, an HTTP client.

    Models one process's lifetime for the restart tests below, which build a
    second, independent instance of this over the same ``data_dir`` to model
    the next. Exiting tears the lifespan down (job manager, hub and sampler
    all stopped) and closes the client, so nothing survives the boundary
    except what is on disk.
    """

    def __init__(self, data_dir: Path) -> None:
        self.app = build_app(data_dir)

    async def __aenter__(self) -> httpx2.AsyncClient:
        self._lifespan_cm = self.app.router.lifespan_context(self.app)
        await self._lifespan_cm.__aenter__()
        transport = httpx2.ASGITransport(app=self.app)
        self._client_cm = httpx2.AsyncClient(transport=transport, base_url="http://test")
        self.client = await self._client_cm.__aenter__()
        return self.client

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._client_cm.__aexit__(exc_type, exc_value, traceback)
        await self._lifespan_cm.__aexit__(exc_type, exc_value, traceback)

    def job_manager(self) -> JobManager:
        return cast(JobManager, self.app.state.job_manager)


async def upload(client: httpx2.AsyncClient, filename: str, content: bytes) -> httpx2.Response:
    """POST ``content`` as a multipart upload named ``filename``."""
    return await client.post(AUDIO_URL, files={"file": (filename, content, "audio/wav")})


def configuration(audio_id: str, **overrides: Any) -> dict[str, Any]:
    """A create-job request body against ``vocals`` unless overridden."""
    mode_id = cast(str, overrides.pop("mode_id", "vocals"))
    body: dict[str, Any] = {
        "audio_id": audio_id,
        "mode_id": mode_id,
        "quality_id": fake_quality_id(mode_id),
    }
    body.update(overrides)
    return body


async def create_job(client: httpx2.AsyncClient, **body: Any) -> dict[str, Any]:
    response = await client.post(JOBS_URL, json=body)
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


# -- the headline: a fresh registry loses the record ------------------------


async def test_a_fresh_store_does_not_see_a_previous_runs_upload(tmp_path: Path) -> None:
    """Proves the bug against the *unmodified* (in-memory-only) behaviour.

    This does not touch :meth:`AudioStore.load` at all — it constructs a
    brand-new store over a directory that already holds a valid upload
    (original file + sidecar, written by hand exactly as ``register`` would)
    and shows that, without a sweep, the record simply is not there.

    Run against a checkout of this branch with the ``load()`` call deleted
    from ``main.py``'s lifespan (the one-line hunk this feature adds), the
    HTTP-level restart test below (``test_get_audio_survives_a_restart``)
    fails with a 404 the same way; this test isolates the same fact down to
    the store alone, without needing to route it through a full app restart.
    """
    store = AudioStore(tmp_path / "data")
    audio_id = store.new_id()
    path = store.prepare_original_path(audio_id, "song.wav")
    path.write_bytes(make_wav_bytes())
    record = AudioFile(
        id=audio_id,
        filename="song.wav",
        size_bytes=path.stat().st_size,
        uploaded_at=datetime.now(UTC),
        metadata=AudioMetadata(
            duration_seconds=1.0,
            container="wav",
            codec="pcm_s16le",
            channels=2,
            sample_rate_hz=44100,
            bit_depth=16,
            bit_rate_bps=1411000,
        ),
    )
    store.register(record)
    assert (path.parent / SIDECAR_FILENAME).is_file(), "register() must have written the sidecar"

    # A second store over the same directory, with no sweep performed.
    reopened = AudioStore(tmp_path / "data")
    assert reopened.get(audio_id) is None, (
        "a freshly constructed store must start empty until load() runs — "
        "this is the bug feature 056 fixes"
    )


# -- restart: GET /audio/{id} ------------------------------------------------


async def test_get_audio_survives_a_restart(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    async with RunningApp(data_dir) as client1:
        response = await upload(client1, "song.wav", make_wav_bytes())
        assert response.status_code == 201
        uploaded = response.json()

    async with RunningApp(data_dir) as client2:
        response = await client2.get(f"{AUDIO_URL}/{uploaded['id']}")
        assert response.status_code == 200
        assert response.json() == uploaded


async def test_create_job_against_a_restored_upload_completes(tmp_path: Path) -> None:
    """Proves the resolution path (``jobs/resolution.py``) reads the restored record."""
    data_dir = tmp_path / "data"
    async with RunningApp(data_dir) as client1:
        uploaded = (await upload(client1, "song.wav", make_wav_bytes())).json()
        audio_id = uploaded["id"]

    running = RunningApp(data_dir)
    async with running as client2:
        recorder = EventRecorder()
        running.job_manager().add_listener(recorder)
        job = await create_job(client2, **configuration(audio_id))

        terminal = await recorder.wait_for_terminal(job["id"])
        assert isinstance(terminal, JobCompletedEvent), terminal

        finished = (await client2.get(f"{JOBS_URL}/{job['id']}")).json()
        assert finished["state"] == "completed", finished
        assert finished["result"]["stems"], "a completed job must have produced stems"


# -- orphans and corruption --------------------------------------------------


async def test_sidecar_without_file_is_a_404_not_a_crash(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    async with RunningApp(data_dir) as client1:
        uploaded = (await upload(client1, "song.wav", make_wav_bytes())).json()
        audio_id = uploaded["id"]

    # Remove the original file but leave the sidecar, e.g. modeling a crash
    # or a manual disk edit that removed one of the pair.
    original = next((data_dir / "audio" / audio_id).glob("original.*"))
    original.unlink()

    async with RunningApp(data_dir) as client2:
        response = await client2.get(f"{AUDIO_URL}/{audio_id}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "audio_not_found"
        # Boot clean: the sidecar-only directory was not recreated or touched.
        assert not (data_dir / "audio" / audio_id / "original.wav").exists()


async def test_file_without_sidecar_is_a_404_and_boots_clean(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    async with RunningApp(data_dir) as client1:
        uploaded = (await upload(client1, "song.wav", make_wav_bytes())).json()
        audio_id = uploaded["id"]

    (data_dir / "audio" / audio_id / SIDECAR_FILENAME).unlink()

    async with RunningApp(data_dir) as client2:
        response = await client2.get(f"{AUDIO_URL}/{audio_id}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "audio_not_found"
        # The orphaned original file is left exactly as found (never deleted
        # at startup — a later feature surfaces/prunes it).
        assert any((data_dir / "audio" / audio_id).glob("original.*"))


async def test_corrupt_sidecar_is_a_warning_not_a_startup_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data_dir = tmp_path / "data"
    async with RunningApp(data_dir) as client1:
        uploaded = (await upload(client1, "song.wav", make_wav_bytes())).json()
        audio_id = uploaded["id"]

    sidecar = data_dir / "audio" / audio_id / SIDECAR_FILENAME
    sidecar.write_text("{not valid json")

    with caplog.at_level(logging.WARNING, logger="straticate.audio.storage"):
        async with RunningApp(data_dir) as client2:
            response = await client2.get(f"{AUDIO_URL}/{audio_id}")
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "audio_not_found"

    # Pinned to the logger and the id (review finding): a bare "any warning"
    # would pass on any unrelated startup warning a future feature adds.
    assert any(
        record.levelno == logging.WARNING
        and record.name == "straticate.audio.storage"
        and audio_id in record.getMessage()
        for record in caplog.records
    ), caplog.records


async def test_mismatched_sidecar_id_is_skipped_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A sidecar whose ``id`` disagrees with its directory restores nothing.

    Review finding: restored verbatim, the record was served under the
    directory's id while its body carried the foreign one — a client
    following the contract then posted the foreign id to ``/jobs`` and got a
    404 for audio it had just fetched. Either id would be wrong; the record
    is skipped like a corrupt one.
    """
    data_dir = tmp_path / "data"
    async with RunningApp(data_dir) as client1:
        uploaded = (await upload(client1, "song.wav", make_wav_bytes())).json()
        audio_id = uploaded["id"]

    sidecar = data_dir / "audio" / audio_id / SIDECAR_FILENAME
    body = json.loads(sidecar.read_text())
    body["id"] = "01TOTALLY0OTHER0ID0000000000"
    sidecar.write_text(json.dumps(body))

    with caplog.at_level(logging.WARNING, logger="straticate.audio.storage"):
        async with RunningApp(data_dir) as client2:
            for requested in (audio_id, body["id"]):
                response = await client2.get(f"{AUDIO_URL}/{requested}")
                assert response.status_code == 404, requested

    assert any(
        record.name == "straticate.audio.storage" and "different id" in record.getMessage()
        for record in caplog.records
    ), caplog.records


async def test_failed_sidecar_write_leaves_no_record_and_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sidecar write failure fails the upload wholesale.

    Review finding: the record used to be cached before the sidecar write,
    so a disk-full 500 left a live, servable record and — after a restart —
    an orphan. Now the client's 500 agrees with the server: no record, no
    files, nothing to restore.
    """
    data_dir = tmp_path / "data"
    real_replace = os.replace

    def disk_full_for_sidecars(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        # Only the sidecar publish fails; every other rename stays real, so
        # the app under test is otherwise healthy.
        if str(dst).endswith(SIDECAR_FILENAME):
            raise OSError(28, "No space left on device")
        real_replace(src, dst)

    monkeypatch.setattr("straticate.audio.storage.os.replace", disk_full_for_sidecars)
    async with RunningApp(data_dir) as client:
        # The ASGI test transport re-raises unhandled server exceptions
        # instead of rendering them; production uvicorn turns this same
        # escape into the 500 the client sees.
        with pytest.raises(OSError, match="No space left"):
            await upload(client, "song.wav", make_wav_bytes())
    monkeypatch.undo()

    audio_root = data_dir / "audio"
    leftovers = sorted(audio_root.iterdir()) if audio_root.is_dir() else []
    assert leftovers == [], "the failed upload's directory must be gone"


async def test_stray_tmp_file_is_ignored(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    async with RunningApp(data_dir) as client1:
        uploaded = (await upload(client1, "song.wav", make_wav_bytes())).json()
        audio_id = uploaded["id"]

    stray = data_dir / "audio" / audio_id / f"{SIDECAR_FILENAME}.deadbeef.tmp"
    stray.write_text("garbage, never attempted to be parsed")

    async with RunningApp(data_dir) as client2:
        response = await client2.get(f"{AUDIO_URL}/{audio_id}")
        assert response.status_code == 200
        assert response.json() == uploaded
        # The stray file is left untouched.
        assert stray.is_file()


# -- rejected uploads leave nothing, sidecar included ------------------------


async def test_rejected_upload_leaves_neither_file_nor_sidecar(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    async with RunningApp(data_dir) as client:
        response = await upload(client, "notes.txt", b"this is not audio at all\n")
        assert response.status_code == 422

    audio_root = data_dir / "audio"
    assert not audio_root.exists() or not any(audio_root.iterdir())


# -- delete -------------------------------------------------------------


async def test_delete_removes_the_sidecar_and_the_record_is_gone_after_restart(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    async with RunningApp(data_dir) as client1:
        uploaded = (await upload(client1, "song.wav", make_wav_bytes())).json()
        audio_id = uploaded["id"]
        audio_dir = data_dir / "audio" / audio_id
        assert (audio_dir / SIDECAR_FILENAME).is_file()

        response = await client1.delete(f"{AUDIO_URL}/{audio_id}")
        assert response.status_code == 204
        assert not audio_dir.exists()

    async with RunningApp(data_dir) as client2:
        response = await client2.get(f"{AUDIO_URL}/{audio_id}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "audio_not_found"
