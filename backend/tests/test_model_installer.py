"""Tests for the model download manager (feature 025).

These run the **real** application over a **real** HTTP download: a synthetic
catalog under ``tmp_path``, an ``httpx2.AsyncClient`` on an ASGI transport for
the API, and the installer's own client streaming from a
:class:`~tests.weights_server.WeightsServer` bound to ``127.0.0.1`` on an
ephemeral port. Nothing here touches the network, and nothing here stubs the
transport — a mocked download would prove that the code calls a mock, not that
it streams a body and stops a server that misbehaves.

Every wait is gated: a :class:`threading.Event` in the serving thread, awaited
from the test through :func:`asyncio.to_thread`, or the installer's own
:meth:`~straticate.models.ModelInstaller.wait`. **No sleep is used as
synchronization.**
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from fastapi import FastAPI

from straticate.config import Settings
from straticate.main import create_app
from straticate.models import (
    ModelCatalog,
    ModelCatalogError,
    ModelInstaller,
    partial_weights_path,
    weights_path,
)
from straticate.models.installer import (
    CHECKSUM_MISMATCH,
    CONNECTION_FAILED,
    DOWNLOAD_FAILED,
    HTTP_STATUS,
    SIZE_EXCEEDED,
    SIZE_MISMATCH,
)
from straticate.schemas import ModelInstallState
from tests.test_model_catalog import make_model, write_catalog
from tests.weights_server import ServedArtifact, WeightsServer

MODELS_URL = "/api/v1/models"
MODES_URL = "/api/v1/separation-modes"
HEALTH_URL = "/api/v1/health"
WAIT_TIMEOUT = 30.0

MODEL_ID = "vocals-hq-001"
BUILT_IN_ID = "fake-vocals-001"
ARTIFACT_PATH = "/weights/vocals-hq-001.ckpt"


def blob(size: int) -> bytes:
    """A deterministic, incompressible-enough body of exactly ``size`` bytes."""
    return bytes((index * 37 + 11) % 256 for index in range(size))


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def free_port() -> int:
    """A loopback port with nothing listening on it (for connection failures)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return cast(int, probe.getsockname()[1])


# -- harness -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Harness:
    """One running application, its client, and where its weights would go."""

    app: FastAPI
    client: httpx2.AsyncClient
    models_dir: Path

    @property
    def installer(self) -> ModelInstaller:
        return cast(ModelInstaller, self.app.state.model_installer)

    def weights(self, model_id: str = MODEL_ID) -> Path:
        return weights_path(self.models_dir, model_id)

    def partial(self, model_id: str = MODEL_ID) -> Path:
        return partial_weights_path(self.models_dir, model_id)

    async def get(self, model_id: str = MODEL_ID) -> dict[str, Any]:
        response = await self.client.get(f"{MODELS_URL}/{model_id}")
        assert response.status_code == 200, response.text
        return cast(dict[str, Any], response.json())

    async def installation(self, model_id: str = MODEL_ID) -> dict[str, Any]:
        return cast(dict[str, Any], (await self.get(model_id))["installation"])

    async def install(self, model_id: str = MODEL_ID) -> httpx2.Response:
        return await self.client.post(f"{MODELS_URL}/{model_id}/install")

    async def remove(self, model_id: str = MODEL_ID) -> httpx2.Response:
        return await self.client.delete(f"{MODELS_URL}/{model_id}/weights")

    async def settle(self, model_id: str = MODEL_ID) -> dict[str, Any]:
        """Wait for the running install, then read the model back."""
        await asyncio.wait_for(self.installer.wait(model_id), timeout=WAIT_TIMEOUT)
        return await self.installation(model_id)


class Builder:
    """Starts applications over synthetic catalogs, tearing them down after."""

    def __init__(self, tmp_path: Path, server: WeightsServer, stack: AsyncExitStack) -> None:
        self._tmp = tmp_path
        self._server = server
        self._stack = stack
        self._count = 0

    async def start(
        self, models: list[dict[str, Any]], *, chunk_bytes: int | None = None
    ) -> Harness:
        """Build and start an app whose catalog holds ``models``."""
        self._count += 1
        models_dir = self._tmp / f"models-{self._count}"
        models_dir.mkdir()
        write_catalog(models_dir, models)
        app = create_app(Settings(models_dir=models_dir, data_dir=self._tmp / "data"))
        if chunk_bytes is not None:
            app.state.model_installer = ModelInstaller(
                app.state.model_catalog, models_dir, chunk_bytes=chunk_bytes
            )
        await self._stack.enter_async_context(app.router.lifespan_context(app))
        client = await self._stack.enter_async_context(
            httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test")
        )
        return Harness(app=app, client=client, models_dir=models_dir)

    def downloadable(
        self,
        body: bytes,
        *,
        model_id: str = MODEL_ID,
        sha: str | None = None,
        size_bytes: int | None = None,
        served: ServedArtifact | None = None,
        url: str | None = None,
        **overrides: Any,
    ) -> dict[str, Any]:
        """A catalog entry whose artifact this server will serve.

        ``sha`` and ``size_bytes`` default to the body's real digest and length,
        so a test only states the ones it is deliberately making wrong.
        """
        download_url = url or self._server.serve(
            ARTIFACT_PATH, served if served is not None else ServedArtifact(body=body)
        )
        return make_model(
            model_id,
            artifact={
                "download_url": download_url,
                "size_bytes": len(body) if size_bytes is None else size_bytes,
                "sha256": sha or sha256(body),
            },
            **overrides,
        )

    def dead_url(self) -> str:
        """A URL on the running server that no artifact is registered for."""
        return self._server.dead_url()


@pytest.fixture
def server() -> Iterator[WeightsServer]:
    with WeightsServer() as running:
        yield running


@pytest.fixture
async def build(tmp_path: Path, server: WeightsServer) -> AsyncIterator[Builder]:
    async with AsyncExitStack() as stack:
        yield Builder(tmp_path, server, stack)


def assert_envelope(response: httpx2.Response, code: str, status: int) -> dict[str, Any]:
    """Assert the standard error envelope and return its ``error`` object."""
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    assert set(body) == {"error"}
    error: dict[str, Any] = body["error"]
    assert set(error) == {"code", "message", "detail"}
    assert error["code"] == code, error
    assert error["message"]
    return error


async def await_thread_event(event: threading.Event) -> None:
    """Wait for a worker thread's event without blocking the loop or sleeping."""
    await asyncio.wait_for(asyncio.to_thread(event.wait, WAIT_TIMEOUT), timeout=WAIT_TIMEOUT)
    assert event.is_set()


async def until(check: Callable[[], Awaitable[bool]]) -> None:
    """Re-ask ``check`` until it holds, bounded by :data:`WAIT_TIMEOUT`.

    Used only where the thing being waited for has *already happened* on the
    serving thread and the test is waiting for the download task to notice it.
    ``asyncio.sleep(0)`` here is a single scheduling yield, not a timed wait:
    it hands the loop to the download task and asks again. Nothing in this
    module waits by sleeping for a duration.
    """

    async def spin() -> None:
        while not await check():
            await asyncio.sleep(0)

    await asyncio.wait_for(spin(), timeout=WAIT_TIMEOUT)


# -- models with no artifact are installed by definition ---------------------


async def test_a_model_without_an_artifact_reports_installed(build: Builder) -> None:
    harness = await build.start([make_model(BUILT_IN_ID)])
    installation = await harness.installation(BUILT_IN_ID)
    assert installation["state"] == "installed"
    assert installation["requires_download"] is False
    assert installation["total_bytes"] is None
    assert installation["downloaded_bytes"] is None
    assert installation["progress"] is None
    assert installation["error"] is None


async def test_the_repository_catalog_is_entirely_installed(
    client: httpx2.AsyncClient,
) -> None:
    """Every built-in fake model is ready; none is offered as a download."""
    response = await client.get(MODELS_URL)
    assert response.status_code == 200
    for model in cast(list[dict[str, Any]], response.json()):
        assert model["installation"] == {
            "state": "installed",
            "requires_download": False,
            "total_bytes": None,
            "downloaded_bytes": None,
            "progress": None,
            "error": None,
        }, model["id"]


async def test_a_built_in_model_cannot_be_installed(build: Builder) -> None:
    harness = await build.start([make_model(BUILT_IN_ID)])
    error = assert_envelope(await harness.install(BUILT_IN_ID), "model_not_downloadable", 409)
    assert error["detail"] == {"model_id": BUILT_IN_ID}


async def test_a_built_in_model_s_weights_cannot_be_removed(build: Builder) -> None:
    harness = await build.start([make_model(BUILT_IN_ID)])
    assert_envelope(await harness.remove(BUILT_IN_ID), "model_not_downloadable", 409)


# -- the happy path ----------------------------------------------------------


async def test_install_downloads_verifies_and_publishes(build: Builder) -> None:
    body = blob(4096)
    harness = await build.start([build.downloadable(body)])

    before = await harness.installation()
    assert before["state"] == "available"
    assert before["requires_download"] is True
    assert before["total_bytes"] == len(body)

    started = await harness.install()
    assert started.status_code == 202, started.text
    assert started.json()["installation"]["state"] == "downloading"

    after = await harness.settle()
    assert after["state"] == "installed"
    assert after["total_bytes"] == len(body)
    assert after["error"] is None
    assert harness.weights().read_bytes() == body
    assert not harness.partial().exists()


async def test_the_models_list_reflects_what_is_on_disk(build: Builder) -> None:
    """``GET /models`` is served through the installer, not the static catalog."""
    body = blob(2048)
    harness = await build.start(
        [build.downloadable(body, quality_tier="high_quality"), make_model(BUILT_IN_ID)]
    )
    assert (await harness.install()).status_code == 202
    await harness.settle()

    response = await harness.client.get(MODELS_URL)
    assert response.status_code == 200
    states = {
        model["id"]: model["installation"]["state"]
        for model in cast(list[dict[str, Any]], response.json())
    }
    assert states == {MODEL_ID: "installed", BUILT_IN_ID: "installed"}


async def test_licensing_is_visible_before_installing(build: Builder) -> None:
    """A user can read the terms while the model is still only `available`."""
    harness = await build.start(
        [
            build.downloadable(
                blob(512),
                licensing={
                    "weights_license": "MIT",
                    "commercial_use_permitted": True,
                    "attribution": "Upstream Author",
                },
            )
        ]
    )
    model = await harness.get()
    assert model["installation"]["state"] == "available"
    assert model["licensing"] == {
        "code_license": None,
        "weights_license": "MIT",
        "redistribution_permitted": None,
        "commercial_use_permitted": True,
        "attribution": "Upstream Author",
    }


async def test_the_artifact_never_appears_in_a_response(build: Builder) -> None:
    """The download URL and pinned digest are the installer's business only."""
    body = blob(256)
    harness = await build.start([build.downloadable(body)])
    response = await harness.client.get(MODELS_URL)
    assert "download_url" not in response.text
    assert "sha256" not in response.text
    assert sha256(body) not in response.text


async def test_uninstalled_models_are_still_offered_as_quality_options(
    build: Builder,
) -> None:
    """010's open question is deliberately left alone until 026."""
    harness = await build.start([build.downloadable(blob(64), quality_tier="high_quality")])
    response = await harness.client.get(MODES_URL)
    assert response.status_code == 200
    modes = cast(list[dict[str, Any]], response.json())
    assert [option["model_id"] for option in modes[0]["quality_options"]] == [MODEL_ID]


# -- returning immediately, and never blocking the loop ----------------------


async def test_install_returns_before_the_download_finishes(build: Builder) -> None:
    """The 202 lands while the body is still on the wire."""
    body = blob(4096)
    served = ServedArtifact(body=body, gate=threading.Event(), stall_after=1024)
    harness = await build.start([build.downloadable(body, served=served)], chunk_bytes=128)

    assert served.gate is not None
    response = await asyncio.wait_for(harness.install(), timeout=WAIT_TIMEOUT)
    assert response.status_code == 202
    await await_thread_event(served.started)
    assert not served.gate.is_set(), "the body was fully served before the 202"
    assert (await harness.installation())["state"] == "downloading"

    served.gate.set()
    assert (await harness.settle())["state"] == "installed"


async def test_progress_is_observable_while_downloading(build: Builder) -> None:
    """Progress is read from the model resource — the mechanism 025 chose."""
    body = blob(4096)
    served = ServedArtifact(body=body, gate=threading.Event(), stall_after=2048)
    harness = await build.start([build.downloadable(body, served=served)], chunk_bytes=256)

    assert served.gate is not None
    assert (await harness.install()).status_code == 202
    await await_thread_event(served.started)

    # The bytes are already on the wire; wait for the download task to take
    # them off it. The download stays parked at the gate throughout.
    async def some_progress() -> bool:
        return bool((await harness.installation())["downloaded_bytes"])

    await until(some_progress)

    installation = await harness.installation()
    assert installation["state"] == "downloading"
    assert 0 < installation["downloaded_bytes"] <= len(body)
    assert 0.0 < installation["progress"] <= 1.0
    assert installation["total_bytes"] == len(body)
    assert not served.gate.is_set()

    served.gate.set()
    assert (await harness.settle())["state"] == "installed"


async def test_other_requests_are_served_while_a_download_is_in_flight(
    build: Builder,
) -> None:
    """If the download ran on the loop, nothing below could complete."""
    body = blob(4096)
    served = ServedArtifact(body=body, gate=threading.Event(), stall_after=512)
    harness = await build.start([build.downloadable(body, served=served)], chunk_bytes=128)

    assert (await harness.install()).status_code == 202
    await await_thread_event(served.started)

    health = await asyncio.wait_for(harness.client.get(HEALTH_URL), timeout=WAIT_TIMEOUT)
    assert health.status_code == 200, health.text
    listing = await asyncio.wait_for(harness.client.get(MODELS_URL), timeout=WAIT_TIMEOUT)
    assert listing.status_code == 200, listing.text
    modes = await asyncio.wait_for(harness.client.get(MODES_URL), timeout=WAIT_TIMEOUT)
    assert modes.status_code == 200, modes.text

    assert served.gate is not None
    served.gate.set()
    assert (await harness.settle())["state"] == "installed"


# -- failure paths -----------------------------------------------------------


async def test_a_checksum_mismatch_installs_nothing(build: Builder) -> None:
    """The whole point of the feature: wrong bytes are never published."""
    body = blob(4096)
    wrong = sha256(b"something else entirely")
    harness = await build.start([build.downloadable(body, sha=wrong)])

    assert (await harness.install()).status_code == 202
    installation = await harness.settle()

    assert installation["state"] == "failed"
    assert installation["error"]["code"] == CHECKSUM_MISMATCH
    assert installation["error"]["detail"]["expected"] == wrong
    assert installation["error"]["detail"]["actual"] == sha256(body)
    assert not harness.weights().exists()
    assert not harness.partial().exists()


async def test_an_http_error_page_is_never_installed(build: Builder) -> None:
    """A taken-down host serving HTML must fail loudly, not install a page."""
    body = blob(1024)
    served = ServedArtifact(body=b"<html>410 gone</html>", status=410)
    harness = await build.start([build.downloadable(body, served=served)])

    assert (await harness.install()).status_code == 202
    installation = await harness.settle()

    assert installation["state"] == "failed"
    assert installation["error"]["code"] == DOWNLOAD_FAILED
    assert installation["error"]["detail"] == {"reason": HTTP_STATUS, "status_code": 410}
    assert not harness.weights().exists()
    assert not harness.partial().exists()


async def test_a_missing_artifact_is_a_download_failure(build: Builder) -> None:
    """A checkpoint repository that was renamed answers 404, not weights."""
    harness = await build.start([build.downloadable(blob(1024), url=build.dead_url())])
    assert (await harness.install()).status_code == 202
    installation = await harness.settle()
    assert installation["state"] == "failed"
    assert installation["error"]["detail"] == {"reason": HTTP_STATUS, "status_code": 404}
    assert not harness.weights().exists()
    assert not harness.partial().exists()


async def test_a_short_body_fails_and_leaves_nothing(build: Builder) -> None:
    """The server finishes cleanly, but with fewer bytes than the catalog pins."""
    body = blob(1024)
    served = ServedArtifact(body=body)
    harness = await build.start([build.downloadable(body, served=served, size_bytes=4096)])

    assert (await harness.install()).status_code == 202
    installation = await harness.settle()

    assert installation["state"] == "failed"
    assert installation["error"]["code"] == DOWNLOAD_FAILED
    assert installation["error"]["detail"] == {
        "reason": SIZE_MISMATCH,
        "expected_bytes": 4096,
        "received_bytes": 1024,
    }
    assert not harness.weights().exists()
    assert not harness.partial().exists()


async def test_a_truncated_transfer_fails_and_leaves_nothing(build: Builder) -> None:
    """The server promises more than it sends and then closes the connection."""
    body = blob(1024)
    served = ServedArtifact(body=body, declared_length=len(body) + 4096)
    harness = await build.start(
        [build.downloadable(body, served=served, size_bytes=len(body) + 4096)]
    )

    assert (await harness.install()).status_code == 202
    installation = await harness.settle()

    assert installation["state"] == "failed"
    assert installation["error"]["code"] == DOWNLOAD_FAILED
    assert installation["error"]["detail"]["reason"] in {CONNECTION_FAILED, SIZE_MISMATCH}
    assert not harness.weights().exists()
    assert not harness.partial().exists()


async def test_a_declared_over_long_body_is_refused_before_it_is_read(
    build: Builder,
) -> None:
    """A ``Content-Length`` above the catalog's size never reaches the disk."""
    body = blob(8192)
    served = ServedArtifact(body=body)
    harness = await build.start([build.downloadable(body, served=served, size_bytes=1024)])

    assert (await harness.install()).status_code == 202
    installation = await harness.settle()

    assert installation["state"] == "failed"
    assert installation["error"]["code"] == DOWNLOAD_FAILED
    assert installation["error"]["detail"] == {
        "reason": SIZE_EXCEEDED,
        "expected_bytes": 1024,
        "declared_bytes": 8192,
    }
    assert not harness.weights().exists()
    assert not harness.partial().exists()


async def test_a_length_less_over_long_body_is_stopped_mid_stream(
    build: Builder,
) -> None:
    """No ``Content-Length``: the running total is what stops the download."""
    body = blob(8192)
    served = ServedArtifact(body=body, omit_length=True)
    harness = await build.start(
        [build.downloadable(body, served=served, size_bytes=1024)], chunk_bytes=256
    )

    assert (await harness.install()).status_code == 202
    installation = await harness.settle()

    assert installation["state"] == "failed"
    assert installation["error"]["code"] == DOWNLOAD_FAILED
    assert installation["error"]["detail"] == {"reason": SIZE_EXCEEDED, "expected_bytes": 1024}
    assert not harness.weights().exists()
    assert not harness.partial().exists()


async def test_a_connection_failure_fails_loudly(build: Builder) -> None:
    """Nothing is listening: the install fails rather than hanging or crashing."""
    body = blob(1024)
    url = f"http://127.0.0.1:{free_port()}{ARTIFACT_PATH}"
    harness = await build.start([build.downloadable(body, url=url)])

    assert (await harness.install()).status_code == 202
    installation = await harness.settle()

    assert installation["state"] == "failed"
    assert installation["error"]["code"] == DOWNLOAD_FAILED
    assert installation["error"]["detail"] == {"reason": CONNECTION_FAILED}
    assert not harness.weights().exists()
    assert not harness.partial().exists()


async def test_a_failure_message_never_leaks_url_credentials(build: Builder) -> None:
    """A catalog is user-editable; an error message is user-pasteable."""
    body = blob(64)
    url = f"http://user:hunter2@127.0.0.1:{free_port()}{ARTIFACT_PATH}"
    harness = await build.start([build.downloadable(body, url=url)])
    assert (await harness.install()).status_code == 202
    installation = await harness.settle()
    assert "hunter2" not in installation["error"]["message"]


# -- concurrency -------------------------------------------------------------


async def test_a_second_install_is_rejected_rather_than_racing(build: Builder) -> None:
    body = blob(4096)
    served = ServedArtifact(body=body, gate=threading.Event(), stall_after=512)
    harness = await build.start([build.downloadable(body, served=served)], chunk_bytes=128)

    assert (await harness.install()).status_code == 202
    await await_thread_event(served.started)

    error = assert_envelope(await harness.install(), "model_busy", 409)
    assert error["detail"] == {"model_id": MODEL_ID}

    assert served.gate is not None
    served.gate.set()
    assert (await harness.settle())["state"] == "installed"
    assert served.requests == 1, "the rejected request started a second download"


async def test_weights_cannot_be_removed_while_an_install_runs(build: Builder) -> None:
    body = blob(4096)
    served = ServedArtifact(body=body, gate=threading.Event(), stall_after=512)
    harness = await build.start([build.downloadable(body, served=served)], chunk_bytes=128)

    assert (await harness.install()).status_code == 202
    await await_thread_event(served.started)
    assert_envelope(await harness.remove(), "model_busy", 409)

    assert served.gate is not None
    served.gate.set()
    assert (await harness.settle())["state"] == "installed"


# -- remove, and install again -----------------------------------------------


async def test_install_remove_install_again(build: Builder) -> None:
    body = blob(2048)
    served = ServedArtifact(body=body)
    harness = await build.start([build.downloadable(body, served=served)])

    assert (await harness.install()).status_code == 202
    assert (await harness.settle())["state"] == "installed"

    removed = await harness.remove()
    assert removed.status_code == 200, removed.text
    assert removed.json()["installation"]["state"] == "available"
    assert not harness.weights().exists()

    assert (await harness.install()).status_code == 202
    assert (await harness.settle())["state"] == "installed"
    assert harness.weights().read_bytes() == body
    assert served.requests == 2


async def test_removing_weights_that_are_not_installed_is_a_no_op(build: Builder) -> None:
    harness = await build.start([build.downloadable(blob(64))])
    response = await harness.remove()
    assert response.status_code == 200, response.text
    assert response.json()["installation"]["state"] == "available"


async def test_removing_weights_clears_a_recorded_failure(build: Builder) -> None:
    body = blob(512)
    harness = await build.start([build.downloadable(body, sha=sha256(b"wrong"))])
    assert (await harness.install()).status_code == 202
    assert (await harness.settle())["state"] == "failed"

    response = await harness.remove()
    assert response.json()["installation"]["state"] == "available"
    assert response.json()["installation"]["error"] is None


async def test_installing_an_installed_model_does_not_download_again(
    build: Builder,
) -> None:
    body = blob(1024)
    served = ServedArtifact(body=body)
    harness = await build.start([build.downloadable(body, served=served)])

    assert (await harness.install()).status_code == 202
    assert (await harness.settle())["state"] == "installed"

    again = await harness.install()
    assert again.status_code == 202
    assert again.json()["installation"]["state"] == "installed"
    assert served.requests == 1


async def test_a_retry_after_a_failure_clears_the_error(build: Builder) -> None:
    """A failed install is a report, not a resting place."""
    body = blob(1024)
    served = ServedArtifact(body=body, status=503)
    harness = await build.start([build.downloadable(body, served=served)])

    assert (await harness.install()).status_code == 202
    assert (await harness.settle())["state"] == "failed"

    served.status = 200
    assert (await harness.install()).status_code == 202
    installation = await harness.settle()
    assert installation["state"] == "installed"
    assert installation["error"] is None


# -- cancellation ------------------------------------------------------------


async def test_a_cancelled_install_leaves_no_part_and_no_weights(
    build: Builder,
) -> None:
    body = blob(8192)
    served = ServedArtifact(body=body, gate=threading.Event(), stall_after=1024)
    harness = await build.start([build.downloadable(body, served=served)], chunk_bytes=128)

    assert (await harness.install()).status_code == 202
    await await_thread_event(served.started)

    await asyncio.wait_for(harness.installer.aclose(), timeout=WAIT_TIMEOUT)

    assert not harness.partial().exists()
    assert not harness.weights().exists()
    assert (await harness.installation())["state"] == "available"


async def test_shutdown_cancels_a_running_install(tmp_path: Path, server: WeightsServer) -> None:
    """The lifespan closes the installer, so nothing is orphaned by a restart."""
    body = blob(8192)
    served = ServedArtifact(body=body, gate=threading.Event(), stall_after=1024)
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    url = server.serve(ARTIFACT_PATH, served)
    write_catalog(
        models_dir,
        [
            make_model(
                MODEL_ID,
                artifact={
                    "download_url": url,
                    "size_bytes": len(body),
                    "sha256": sha256(body),
                },
            )
        ],
    )
    app = create_app(Settings(models_dir=models_dir, data_dir=tmp_path / "data"))
    installer = ModelInstaller(app.state.model_catalog, models_dir, chunk_bytes=128)
    app.state.model_installer = installer

    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.post(f"{MODELS_URL}/{MODEL_ID}/install")).status_code == 202
            await await_thread_event(served.started)

    assert not partial_weights_path(models_dir, MODEL_ID).exists()
    assert not weights_path(models_dir, MODEL_ID).exists()
    entry = cast(ModelCatalog, app.state.model_catalog).get_entry(MODEL_ID)
    assert installer.describe(entry).installation.state is ModelInstallState.AVAILABLE


# -- unknown and unusable IDs ------------------------------------------------


@pytest.mark.parametrize("model_id", ["nope", "%2e%2e", "a_b", "C%3A%5Cwindows", "Vocals-HQ"])
async def test_an_unknown_or_unusable_id_is_a_clean_404(build: Builder, model_id: str) -> None:
    """An ID that could not be a model ID is simply not a catalog key.

    It exits as ``model_not_found`` — never a 500, and never a path outside
    ``models_dir``.
    """
    harness = await build.start([build.downloadable(blob(64))])
    for response in (
        await harness.client.get(f"{MODELS_URL}/{model_id}"),
        await harness.client.post(f"{MODELS_URL}/{model_id}/install"),
        await harness.client.delete(f"{MODELS_URL}/{model_id}/weights"),
    ):
        assert_envelope(response, "model_not_found", 404)
    assert not (harness.models_dir / "weights").exists()


@pytest.mark.parametrize("model_id", ["..%2Fetc", "..%2F..%2Fescape", "%2Fabsolute"])
async def test_a_traversal_attempt_writes_nothing(build: Builder, model_id: str) -> None:
    """An encoded separator is a 404 from the router or the catalog, never a write.

    Which of the two answers it is depends on URL normalization and is not
    something this feature should pin down; what matters is that it is a 404
    and that nothing appears under ``models_dir``.
    """
    harness = await build.start([build.downloadable(blob(64))])
    for response in (
        await harness.client.get(f"{MODELS_URL}/{model_id}"),
        await harness.client.post(f"{MODELS_URL}/{model_id}/install"),
        await harness.client.delete(f"{MODELS_URL}/{model_id}/weights"),
    ):
        assert response.status_code == 404, response.text
    assert not (harness.models_dir / "weights").exists()
    assert sorted(path.name for path in harness.models_dir.iterdir()) == ["catalog.json"]


async def test_a_catalog_whose_model_id_could_escape_fails_to_load(tmp_path: Path) -> None:
    """An unusable ID is caught at load, not at the first install."""
    models_dir = tmp_path / "bad-models"
    models_dir.mkdir()
    write_catalog(models_dir, [make_model("../escape")])
    with pytest.raises(ModelCatalogError, match="invalid model ID"):
        create_app(Settings(models_dir=models_dir, data_dir=tmp_path / "data"))
