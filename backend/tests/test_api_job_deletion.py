"""Tests for ``DELETE /api/v1/jobs/{job_id}`` (feature 058).

Like ``test_api_jobs.py`` and ``test_jobs_persistence.py`` these run the
**real** application: a real job manager, a real event hub, and a real
``FakeSeparator`` writing real stems, with only its simulated delays zeroed.
Every wait is gated with an ``asyncio.Event``; no sleep is ever used as
synchronization.

Deletion removes a job's whole directory — record, stems and exports — in one
``shutil.rmtree``, which is what finally answers A1's worst case: before this
feature nothing could remove the stems and exports a completed job produced.
See ``docs/features/058-job-deletion.md`` for the design.
"""

import asyncio
from pathlib import Path
from typing import Any, cast

import httpx2
from fastapi import FastAPI

from straticate.jobs.layout import job_output_dir
from straticate.schemas.events import JobCompletedEvent
from tests.restart_harness import running_app
from tests.test_api_jobs import (
    JOBS_URL,
    WAIT_TIMEOUT,
    EventRecorder,
    build_app,
    configuration,
    create_job,
    gated_registry,
    manager_of,
    register_audio,
)

# -- helpers ------------------------------------------------------------------


async def run_job_to_completion(
    client: httpx2.AsyncClient, app: FastAPI, recorder: EventRecorder, audio: str
) -> str:
    created = await create_job(client, **configuration(audio))
    job_id = cast(str, created["id"])
    terminal = await recorder.wait_for_terminal(job_id)
    assert isinstance(terminal, JobCompletedEvent), terminal
    return job_id


async def export_one_stem(client: httpx2.AsyncClient, job_id: str) -> None:
    """Populate the job's ``exports/`` directory so a delete has something to remove."""
    response = await client.get(
        f"{JOBS_URL}/{job_id}/export", params={"format": "wav_pcm24", "stems": "vocals"}
    )
    assert response.status_code == 200, response.text


def assert_envelope(response: httpx2.Response, code: str, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    error = cast(dict[str, Any], body["error"])
    assert error["code"] == code
    return error


# -- delete a completed job ---------------------------------------------------


async def test_delete_completed_job_removes_record_stems_and_exports(tmp_path: Path) -> None:
    app = build_app(tmp_path)
    audio = register_audio(app)
    async with running_app(app) as client:
        recorder = EventRecorder()
        manager_of(app).add_listener(recorder)
        job_id = await run_job_to_completion(client, app, recorder, audio)
        await export_one_stem(client, job_id)

        directory = job_output_dir(tmp_path, job_id)
        assert (directory / "job.json").is_file()
        assert any((directory / "stems").iterdir())
        assert any((directory / "exports").iterdir())

        response = await client.delete(f"{JOBS_URL}/{job_id}")
        assert response.status_code == 204
        assert response.content == b""

        assert not directory.exists()
        assert_envelope(await client.get(f"{JOBS_URL}/{job_id}"), "job_not_found", 404)
        assert [job["id"] for job in (await client.get(JOBS_URL)).json()] == []


async def test_delete_removes_a_job_that_never_exported_anything(tmp_path: Path) -> None:
    """Exports are optional; a job that was never exported still deletes cleanly."""
    app = build_app(tmp_path)
    audio = register_audio(app)
    async with running_app(app) as client:
        recorder = EventRecorder()
        manager_of(app).add_listener(recorder)
        job_id = await run_job_to_completion(client, app, recorder, audio)
        directory = job_output_dir(tmp_path, job_id)
        assert not (directory / "exports").exists()

        response = await client.delete(f"{JOBS_URL}/{job_id}")
        assert response.status_code == 204
        assert not directory.exists()


# -- delete an active job -----------------------------------------------------


async def test_delete_a_queued_job_is_refused(tmp_path: Path) -> None:
    app = build_app(tmp_path)
    started, gate = asyncio.Event(), asyncio.Event()
    registry, _ = gated_registry(started, gate)
    app.state.separator_registry = registry
    audio = register_audio(app)

    async with running_app(app) as client:
        recorder = EventRecorder()
        manager_of(app).add_listener(recorder)
        running = await create_job(client, **configuration(audio))
        queued = await create_job(client, **configuration(audio))
        await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT)

        error = assert_envelope(
            await client.delete(f"{JOBS_URL}/{queued['id']}"), "job_active", 409
        )
        assert error["detail"] == {"job_id": queued["id"], "state": "queued"}
        # Refused, not removed.
        assert (await client.get(f"{JOBS_URL}/{queued['id']}")).status_code == 200

        gate.set()
        await recorder.wait_for_terminal(cast(str, running["id"]))
        await recorder.wait_for_terminal(cast(str, queued["id"]))


async def test_delete_a_running_job_is_refused_then_succeeds_after_cancel(
    tmp_path: Path,
) -> None:
    app = build_app(tmp_path)
    started, gate = asyncio.Event(), asyncio.Event()
    registry, _ = gated_registry(started, gate)
    app.state.separator_registry = registry
    audio = register_audio(app)

    async with running_app(app) as client:
        recorder = EventRecorder()
        manager_of(app).add_listener(recorder)
        created = await create_job(client, **configuration(audio))
        job_id = cast(str, created["id"])
        await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT)

        error = assert_envelope(await client.delete(f"{JOBS_URL}/{job_id}"), "job_active", 409)
        assert error["detail"] == {"job_id": job_id, "state": "separating"}

        cancel = await client.post(f"{JOBS_URL}/{job_id}/cancel")
        assert cancel.status_code == 200, cancel.text
        gate.set()
        terminal = await recorder.wait_for_terminal(job_id)
        assert terminal.type == "job_cancelled"

        directory = job_output_dir(tmp_path, job_id)
        assert directory.exists()
        response = await client.delete(f"{JOBS_URL}/{job_id}")
        assert response.status_code == 204
        assert not directory.exists()


# -- unknown job ---------------------------------------------------------------


async def test_delete_of_an_unknown_job_returns_job_not_found(tmp_path: Path) -> None:
    async with running_app(build_app(tmp_path)) as client:
        assert_envelope(await client.delete(f"{JOBS_URL}/01NOTAJOB"), "job_not_found", 404)


async def test_delete_is_idempotent_in_effect_not_response(tmp_path: Path) -> None:
    """A second delete of an already-deleted job is a fresh 404, not a repeat 204."""
    app = build_app(tmp_path)
    audio = register_audio(app)
    async with running_app(app) as client:
        recorder = EventRecorder()
        manager_of(app).add_listener(recorder)
        job_id = await run_job_to_completion(client, app, recorder, audio)

        assert (await client.delete(f"{JOBS_URL}/{job_id}")).status_code == 204
        assert_envelope(await client.delete(f"{JOBS_URL}/{job_id}"), "job_not_found", 404)


# -- restart does not resurrect a deleted job ---------------------------------


async def test_a_deleted_job_does_not_survive_a_restart(tmp_path: Path) -> None:
    """The record died with the directory; the next boot never hears of it."""
    first = build_app(tmp_path)
    audio = register_audio(first)
    async with running_app(first) as client:
        recorder = EventRecorder()
        manager_of(first).add_listener(recorder)
        job_id = await run_job_to_completion(client, first, recorder, audio)
        response = await client.delete(f"{JOBS_URL}/{job_id}")
        assert response.status_code == 204
    del first

    async with running_app(build_app(tmp_path)) as client:
        assert (await client.get(JOBS_URL)).json() == []
        assert_envelope(await client.get(f"{JOBS_URL}/{job_id}"), "job_not_found", 404)


async def test_a_surviving_sibling_job_is_unaffected_by_a_delete(tmp_path: Path) -> None:
    """Deleting one job's directory never touches another's."""
    app = build_app(tmp_path)
    audio = register_audio(app)
    async with running_app(app) as client:
        recorder = EventRecorder()
        manager_of(app).add_listener(recorder)
        deleted_id = await run_job_to_completion(client, app, recorder, audio)
        kept_id = await run_job_to_completion(client, app, recorder, audio)

        assert (await client.delete(f"{JOBS_URL}/{deleted_id}")).status_code == 204

        assert (await client.get(f"{JOBS_URL}/{kept_id}")).status_code == 200
        assert job_output_dir(tmp_path, kept_id).exists()
        assert [job["id"] for job in (await client.get(JOBS_URL)).json()] == [kept_id]


# -- best-effort removal (Windows: a locked file is debris, not a failure) ----


async def test_delete_tolerates_a_file_locked_by_an_open_handle(tmp_path: Path) -> None:
    """A held-open stem file cannot be unlinked on Windows; the delete still succeeds.

    This is the trade the route's docstring documents: ``shutil.rmtree(...,
    ignore_errors=True)`` best-effort removes everything it can, answers `204`
    regardless, and whatever a locked handle kept alive is debris a later
    pruning feature (060) sweeps up — not a reason to fail a request that, from
    the API's point of view, really did delete the job (it is gone from every
    other endpoint immediately).
    """
    app = build_app(tmp_path)
    audio = register_audio(app)
    async with running_app(app) as client:
        recorder = EventRecorder()
        manager_of(app).add_listener(recorder)
        job_id = await run_job_to_completion(client, app, recorder, audio)

        directory = job_output_dir(tmp_path, job_id)
        stems_dir = directory / "stems"
        locked_stem = next(iter(stems_dir.iterdir()))

        handle = locked_stem.open("rb")
        try:
            response = await client.delete(f"{JOBS_URL}/{job_id}")
            assert response.status_code == 204
            assert response.content == b""

            # The job is gone from the API's point of view, whatever debris
            # a locked handle prevented from being unlinked.
            assert_envelope(await client.get(f"{JOBS_URL}/{job_id}"), "job_not_found", 404)
            assert [job["id"] for job in (await client.get(JOBS_URL)).json()] == []

            # The locked file (and the directories it prevented from being
            # emptied) are the debris a later pruning feature sweeps up — but
            # the record is gone, which is what makes the job vanish from
            # every endpoint above.
            assert locked_stem.exists()
            assert not (directory / "job.json").exists()
        finally:
            handle.close()
