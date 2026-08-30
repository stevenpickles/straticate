"""Tests for durable job records and interrupted-job recovery (feature 057).

Most tests here restart the backend: they drive one real application over a
temporary ``data_dir``, drop it, and ask a **second** application built over
the same directory what it found. See ``tests/restart_harness.py`` for the
rules that make that honest, and ``tests/test_api_jobs.py`` for the application
builder and the event-driven waits reused here — no sleeps anywhere.

The rest hand-write a ``job.json`` that no orderly shutdown could have left
behind (a job still ``separating``) and boot on top of it: that is the crash
this feature is really about.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from fastapi import FastAPI
from ulid import ULID

from straticate import main
from straticate.jobs import JOB_INTERRUPTED_CODE, JobManager, JobStore
from straticate.jobs.layout import job_record_path, jobs_root
from straticate.schemas.common import ErrorInfo
from straticate.schemas.events import JobCompletedEvent
from straticate.schemas.jobs import (
    Job,
    JobState,
    SeparationConfiguration,
    SeparationResult,
    SeparationResultMetrics,
    Stem,
)
from tests.restart_harness import read_job_record, running_app, write_job_record
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
from tests.test_jobs_manager import instant_executor

# -- helpers ----------------------------------------------------------------


def make_job(state: JobState, **overrides: Any) -> Job:
    """A plausible job record in ``state``, built through the real model.

    Going through :class:`~straticate.schemas.jobs.Job` rather than hand-typing
    JSON is the point: the record on disk *is* the wire shape, so a test that
    writes one cannot drift from the contract without failing to construct.
    """
    job = Job(
        id=str(ULID()),
        audio_id=str(ULID()),
        configuration=SeparationConfiguration(
            audio_id=str(ULID()), mode_id="vocals", quality_id="balanced", device_id="cpu"
        ),
        model_id="fake-vocals-001",
        state=state,
        progress=0.0,
        created_at=datetime.now(UTC),
        started_at=None,
        finished_at=None,
        error=None,
        result=None,
    )
    return job.model_copy(update=overrides)


def as_record(job: Job) -> dict[str, Any]:
    """The job exactly as it is written to (and served from) disk."""
    return job.model_dump(mode="json")


def place(data_dir: Path, job: Job) -> dict[str, Any]:
    """Write ``job`` straight to disk, as a killed process would have left it."""
    record = as_record(job)
    write_job_record(data_dir, record)
    return record


def listen_from_startup(monkeypatch: pytest.MonkeyPatch, recorder: EventRecorder) -> None:
    """Attach ``recorder`` to every job manager the lifespan builds, from birth.

    A listener added after the lifespan has returned cannot prove that startup
    emitted nothing, so this substitutes a manager subclass that registers the
    listener in its own constructor — before ``restore`` is ever called.
    """

    class RecordingManager(JobManager):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.add_listener(recorder)

    monkeypatch.setattr(main, "JobManager", RecordingManager)


async def run_one_job(client: httpx2.AsyncClient, app: FastAPI, recorder: EventRecorder) -> str:
    """Run a real job to completion and return its ID.

    Used as a **barrier** rather than for its own sake: events are delivered in
    strict emission order by a single dispatcher, so once this job's terminal
    event has arrived, anything startup might have emitted would already have
    been delivered too. That is what makes "no event for the restored job" an
    assertion rather than a race.
    """
    audio = register_audio(app, filename="barrier.wav")
    created = await create_job(client, **configuration(audio))
    job_id = cast(str, created["id"])
    terminal = await recorder.wait_for_terminal(job_id)
    assert isinstance(terminal, JobCompletedEvent), terminal
    return job_id


# -- the headline -----------------------------------------------------------


async def test_a_completed_job_and_its_outputs_survive_a_restart(tmp_path: Path) -> None:
    """A completed job is fully reachable in the next process.

    Before feature 057 every one of these assertions failed — the second
    application's job manager started empty, so the record was a ``404`` and
    the stems on disk were unreachable orphans.
    """
    first = build_app(tmp_path)
    audio = register_audio(first)
    async with running_app(first) as client:
        recorder = EventRecorder()
        manager_of(first).add_listener(recorder)
        created = await create_job(client, **configuration(audio))
        job_id = cast(str, created["id"])
        terminal = await recorder.wait_for_terminal(job_id)
        assert isinstance(terminal, JobCompletedEvent), terminal
        before: dict[str, Any] = (await client.get(f"{JOBS_URL}/{job_id}")).json()
        assert before["state"] == "completed"
    del first

    async with running_app(build_app(tmp_path)) as client:
        listed: list[dict[str, Any]] = (await client.get(JOBS_URL)).json()
        assert [job["id"] for job in listed] == [job_id]

        fetched = await client.get(f"{JOBS_URL}/{job_id}")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json() == before

        result = await client.get(f"{JOBS_URL}/{job_id}/result")
        assert result.status_code == 200, result.text
        assert result.json() == before["result"]

        stem = await client.get(f"{JOBS_URL}/{job_id}/stems/vocals")
        assert stem.status_code == 200, stem.text
        assert stem.content

        export = await client.get(
            f"{JOBS_URL}/{job_id}/export", params={"format": "wav_pcm24", "stems": "vocals"}
        )
        assert export.status_code == 200, export.text
        assert export.content


async def test_several_jobs_come_back_in_ulid_order(tmp_path: Path) -> None:
    """``GET /jobs`` still means submission order across the restart."""
    first = build_app(tmp_path)
    audio = register_audio(first)
    async with running_app(first) as client:
        recorder = EventRecorder()
        manager_of(first).add_listener(recorder)
        submitted: list[str] = []
        for _ in range(3):
            created = await create_job(client, **configuration(audio))
            job_id = cast(str, created["id"])
            await recorder.wait_for_terminal(job_id)
            submitted.append(job_id)
    del first

    async with running_app(build_app(tmp_path)) as client:
        listed: list[dict[str, Any]] = (await client.get(JOBS_URL)).json()
        assert [job["id"] for job in listed] == submitted


# -- the interrupted job ----------------------------------------------------


@pytest.mark.parametrize("state", [JobState.QUEUED, JobState.SEPARATING])
async def test_a_job_the_server_stopped_under_comes_back_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: JobState
) -> None:
    """The crash case: a non-terminal record on disk is repaired, not re-run.

    ``queued`` and ``separating`` are the two ends of it — a job that never
    started and one that was mid-inference — and both get the same answer,
    which is why no intermediate state is ever persisted.
    """
    interrupted = make_job(
        state, started_at=None if state is JobState.QUEUED else datetime.now(UTC)
    )
    place(tmp_path, interrupted)
    recorder = EventRecorder()
    listen_from_startup(monkeypatch, recorder)

    app = build_app(tmp_path)
    async with running_app(app) as client:
        response = await client.get(f"{JOBS_URL}/{interrupted.id}")
        assert response.status_code == 200, response.text
        job: dict[str, Any] = response.json()
        assert job["state"] == "failed"
        assert job["error"] == {
            "code": JOB_INTERRUPTED_CODE,
            "message": "The server stopped while this job was queued or running.",
            "detail": {},
        }
        assert job["finished_at"] is not None
        # Everything the record already said is still there.
        assert job["configuration"] == as_record(interrupted)["configuration"]
        assert job["created_at"] == as_record(interrupted)["created_at"]

        # The repair is on disk, so the next boot does not re-derive it.
        assert read_job_record(tmp_path, interrupted.id) == job

        # Nothing was queued and nothing was announced: the barrier job's own
        # terminal event has arrived, so any startup event would have too.
        await run_one_job(client, app, recorder)
        assert recorder.types(interrupted.id) == []
        assert (await client.get(f"{JOBS_URL}/{interrupted.id}")).json()["state"] == "failed"


async def test_an_interrupted_job_is_not_re_run_by_a_later_restart(tmp_path: Path) -> None:
    """Two boots over the same directory leave it failed, not queued again."""
    interrupted = make_job(JobState.SEPARATING)
    place(tmp_path, interrupted)

    async with running_app(build_app(tmp_path)) as client:
        first_view: dict[str, Any] = (await client.get(f"{JOBS_URL}/{interrupted.id}")).json()
    async with running_app(build_app(tmp_path)) as client:
        second_view: dict[str, Any] = (await client.get(f"{JOBS_URL}/{interrupted.id}")).json()

    assert first_view["state"] == second_view["state"] == "failed"
    assert second_view["error"]["code"] == JOB_INTERRUPTED_CODE
    # The second boot found a terminal record and left it exactly alone.
    assert second_view == first_view


async def test_a_job_still_running_at_shutdown_is_cancelled_not_interrupted(
    tmp_path: Path,
) -> None:
    """An orderly shutdown really does cancel; only a crash is ``job_interrupted``.

    The manager cancels a running job at ``aclose`` and persists that, so the
    restart finds a terminal record and restores it verbatim.
    """
    started, gate = asyncio.Event(), asyncio.Event()
    first = build_app(tmp_path)
    first.state.separator_registry = gated_registry(started, gate)[0]
    audio = register_audio(first)
    async with running_app(first) as client:
        created = await create_job(client, **configuration(audio))
        job_id = cast(str, created["id"])
        await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT)
    del first

    async with running_app(build_app(tmp_path)) as client:
        job: dict[str, Any] = (await client.get(f"{JOBS_URL}/{job_id}")).json()
    assert job["state"] == "cancelled"
    assert job["error"] is None


# -- terminal records restore verbatim --------------------------------------


async def test_cancelled_and_failed_records_restore_verbatim(tmp_path: Path) -> None:
    cancelled = make_job(
        JobState.CANCELLED, started_at=datetime.now(UTC), finished_at=datetime.now(UTC)
    )
    failed = make_job(
        JobState.FAILED,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        error=ErrorInfo(code="separation_failed", message="Something went wrong."),
    )
    records = {job.id: place(tmp_path, job) for job in (cancelled, failed)}

    async with running_app(build_app(tmp_path)) as client:
        for job_id, record in records.items():
            response = await client.get(f"{JOBS_URL}/{job_id}")
            assert response.status_code == 200, response.text
            assert response.json() == record
        # A restored terminal job is a no-op to cancel, exactly as a live one is.
        cancel = await client.post(f"{JOBS_URL}/{cancelled.id}/cancel")
        assert cancel.status_code == 200, cancel.text
        assert cancel.json() == records[cancelled.id]


async def test_a_completed_record_restores_its_result(tmp_path: Path) -> None:
    """The record carries the result, so ``/result`` answers without the stems."""
    completed = make_job(
        JobState.COMPLETED,
        progress=1.0,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    completed = completed.model_copy(
        update={
            "result": SeparationResult(
                job_id=completed.id,
                model_id="fake-vocals-001",
                stems=[Stem(name="vocals", duration_seconds=1.0, sample_rate_hz=44100, channels=2)],
                metrics=SeparationResultMetrics(processing_seconds=1.0, realtime_factor=1.0),
            )
        }
    )
    record = place(tmp_path, completed)

    async with running_app(build_app(tmp_path)) as client:
        assert (await client.get(f"{JOBS_URL}/{completed.id}")).json() == record
        result = await client.get(f"{JOBS_URL}/{completed.id}/result")
        assert result.status_code == 200, result.text
        assert result.json() == record["result"]
        # The stems are not on disk, and that is the documented 404 rather than
        # a restore failure — the record and its files are separate things.
        stem = await client.get(f"{JOBS_URL}/{completed.id}/stems/vocals")
        assert stem.status_code == 404, stem.text
        assert stem.json()["error"]["code"] == "stem_file_missing"


# -- what startup ignores ---------------------------------------------------


async def test_a_job_directory_without_a_record_is_ignored(tmp_path: Path) -> None:
    """An orphan from a run older than this feature: boot is clean, list empty."""
    orphan = jobs_root(tmp_path) / str(ULID()) / "stems"
    orphan.mkdir(parents=True)
    (orphan / "vocals.wav").write_bytes(b"RIFF....")

    async with running_app(build_app(tmp_path)) as client:
        assert (await client.get(JOBS_URL)).json() == []


async def test_a_corrupt_record_is_skipped_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One unreadable record may never stop a server from starting."""
    good = make_job(JobState.COMPLETED, finished_at=datetime.now(UTC), progress=1.0)
    place(tmp_path, good)
    broken_id = str(ULID())
    broken = job_record_path(tmp_path, broken_id)
    broken.parent.mkdir(parents=True)
    broken.write_text("{ this is not json", encoding="utf-8")
    truncated_id = str(ULID())
    truncated = job_record_path(tmp_path, truncated_id)
    truncated.parent.mkdir(parents=True)
    truncated.write_text(json.dumps({"id": truncated_id}), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="straticate.jobs.store"):
        async with running_app(build_app(tmp_path)) as client:
            listed: list[dict[str, Any]] = (await client.get(JOBS_URL)).json()

    assert [job["id"] for job in listed] == [good.id]
    warnings = [record.getMessage() for record in caplog.records]
    assert sum(str(broken) in message for message in warnings) == 1
    assert sum(str(truncated) in message for message in warnings) == 1


async def test_a_record_that_disagrees_with_its_directory_is_skipped(tmp_path: Path) -> None:
    """The ID is what every other path is built from, so a mismatch is refused."""
    job = make_job(JobState.COMPLETED, finished_at=datetime.now(UTC), progress=1.0)
    misplaced = job_record_path(tmp_path, str(ULID()))
    misplaced.parent.mkdir(parents=True)
    misplaced.write_text(job.model_dump_json(), encoding="utf-8")

    async with running_app(build_app(tmp_path)) as client:
        assert (await client.get(JOBS_URL)).json() == []


async def test_leftover_temporary_files_are_ignored(tmp_path: Path) -> None:
    """A crash mid-write leaves ``job.json.{uuid}.tmp``; it is not a record."""
    good = make_job(JobState.COMPLETED, finished_at=datetime.now(UTC), progress=1.0)
    place(tmp_path, good)
    (jobs_root(tmp_path) / f"{good.id}" / "job.json.deadbeef.tmp").write_text(
        "{}", encoding="utf-8"
    )
    (jobs_root(tmp_path) / "job.json.deadbeef.tmp").write_text("{}", encoding="utf-8")

    async with running_app(build_app(tmp_path)) as client:
        assert [job["id"] for job in (await client.get(JOBS_URL)).json()] == [good.id]


# -- when the record is written ---------------------------------------------


async def test_the_record_exists_as_soon_as_the_job_is_created(tmp_path: Path) -> None:
    """A ``201`` means the record is already on disk, in state ``queued``.

    The separator is gated shut, so nothing has moved the job on; the record
    stays ``queued`` while the job runs precisely because intermediate states
    are never persisted.
    """
    started, gate = asyncio.Event(), asyncio.Event()
    app = build_app(tmp_path)
    app.state.separator_registry = gated_registry(started, gate)[0]
    audio = register_audio(app)

    async with running_app(app) as client:
        created = await create_job(client, **configuration(audio))
        job_id = cast(str, created["id"])
        record = read_job_record(tmp_path, job_id)
        assert record["state"] == "queued"
        assert record == created

        await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT)
        running: dict[str, Any] = (await client.get(f"{JOBS_URL}/{job_id}")).json()
        assert running["state"] == "separating"
        assert read_job_record(tmp_path, job_id)["state"] == "queued"


async def test_the_terminal_record_is_written_before_the_terminal_event(tmp_path: Path) -> None:
    """A client that saw ``job_completed`` can trust the record behind it."""
    app = build_app(tmp_path)
    audio = register_audio(app)
    seen: list[str] = []

    async with running_app(app) as client:
        recorder = EventRecorder()

        def watch(event: Any) -> None:
            if event.type == "job_completed":
                seen.append(read_job_record(tmp_path, cast(str, event.job_id))["state"])

        manager_of(app).add_listener(watch)
        manager_of(app).add_listener(recorder)
        created = await create_job(client, **configuration(audio))
        await recorder.wait_for_terminal(cast(str, created["id"]))

    assert seen == ["completed"]


async def test_two_lifespans_of_one_application_agree(tmp_path: Path) -> None:
    """The pattern ``main.lifespan`` documents: one app object, several cycles.

    Each cycle builds a fresh manager, so the second one learns about the first
    one's job from the disk — the same path a restart takes, without a second
    application object.
    """
    app = build_app(tmp_path)
    audio = register_audio(app)

    async with running_app(app) as client:
        recorder = EventRecorder()
        manager_of(app).add_listener(recorder)
        created = await create_job(client, **configuration(audio))
        job_id = cast(str, created["id"])
        await recorder.wait_for_terminal(job_id)
        first: dict[str, Any] = (await client.get(f"{JOBS_URL}/{job_id}")).json()

    async with running_app(app) as client:
        assert [job["id"] for job in (await client.get(JOBS_URL)).json()] == [job_id]
        assert (await client.get(f"{JOBS_URL}/{job_id}")).json() == first
        assert (await client.get(f"{JOBS_URL}/{job_id}/result")).status_code == 200


# -- the store on its own ---------------------------------------------------


def test_saving_publishes_atomically_and_leaves_no_scratch_file(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    job = make_job(JobState.COMPLETED, finished_at=datetime.now(UTC), progress=1.0)

    store.save(job)
    store.save(job.model_copy(update={"progress": 1.0}))

    directory = job_record_path(tmp_path, job.id).parent
    assert sorted(path.name for path in directory.iterdir()) == ["job.json"]
    assert store.load_all() == [job]


def test_load_all_of_an_empty_data_directory_is_empty(tmp_path: Path) -> None:
    """A first run has no ``jobs/`` directory at all, and that is not an error."""
    assert JobStore(tmp_path).load_all() == []
    assert JobStore(tmp_path).recover() == []


def test_recover_normalizes_only_the_non_terminal_records(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    completed = make_job(JobState.COMPLETED, finished_at=datetime.now(UTC), progress=1.0)
    decoding = make_job(JobState.DECODING)
    for job in (completed, decoding):
        store.save(job)

    recovered = {job.id: job for job in store.recover()}

    assert recovered[completed.id] == completed
    repaired = recovered[decoding.id]
    assert repaired.state is JobState.FAILED
    assert repaired.error is not None and repaired.error.code == JOB_INTERRUPTED_CODE
    assert repaired.finished_at is not None
    assert repaired.created_at == decoding.created_at
    assert store.load_all() == sorted(
        [completed, repaired], key=lambda job: job.id
    )  # the repair is on disk


async def test_restore_refuses_a_non_terminal_record() -> None:
    """A restored job never runs, so a non-terminal one would be stranded."""
    manager = JobManager()
    with pytest.raises(ValueError, match="non-terminal"):
        manager.restore([make_job(JobState.QUEUED)])
    assert manager.list_jobs() == []


async def _parks(job: Job, context: Any) -> SeparationResult:
    """An executor that never finishes; the manager cancels it at ``aclose``."""
    await asyncio.Event().wait()
    raise AssertionError("unreachable")  # pragma: no cover - the wait never returns


async def test_restore_emits_nothing() -> None:
    """Restoring is not an event: the manager's own view of it, without an app."""
    recorder = EventRecorder()
    manager = JobManager()
    manager.add_listener(recorder)
    manager.start()
    restored = make_job(JobState.COMPLETED, finished_at=datetime.now(UTC), progress=1.0)
    try:
        manager.restore([restored])
        assert manager.get(restored.id) == restored
        # Emit one event of our own and wait for it: the dispatcher is strictly
        # ordered, so anything `restore` had emitted would arrive before it.
        job = manager.submit(
            SeparationConfiguration(audio_id="01AUDIO", mode_id="vocals", quality_id="balanced"),
            _parks,
        )
        await recorder.wait_for(lambda event: event.job_id == job.id)
    finally:
        await manager.aclose()

    assert recorder.types(restored.id) == []


async def test_a_failing_store_refuses_the_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A submit-time persist failure fails the POST, not the promise.

    Review finding: swallowing here quietly re-opened the "201 then gone"
    window — a job the API confirmed but that no future process would ever
    hear of. Nothing has run at submit time, so refusing is honest and the
    manager holds no half-registered entry afterwards.
    """
    store = JobStore(tmp_path)

    def explode(job: Job) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(store, "save", explode)
    manager = JobManager(store=store)
    try:
        with pytest.raises(OSError, match="no space left"):
            manager.submit(
                SeparationConfiguration(
                    audio_id="01AUDIO", mode_id="vocals", quality_id="balanced"
                ),
                _parks,
            )
        assert manager.list_jobs() == []
    finally:
        await manager.aclose()


async def test_a_failing_store_never_fails_a_terminal_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once work ran, persistence stays a promise, never a precondition.

    The store fails only *after* the successful submit-time write: a full
    disk at completion must not turn a separation that really finished into
    ``separation_failed``.
    """
    store = JobStore(tmp_path)
    real_save = store.save
    calls = {"count": 0}

    def explode_after_submit(job: Job) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            real_save(job)
            return
        raise OSError("no space left on device")

    monkeypatch.setattr(store, "save", explode_after_submit)
    manager = JobManager(store=store)
    recorder = EventRecorder()
    manager.add_listener(recorder)
    manager.start()
    try:
        job = manager.submit(
            SeparationConfiguration(audio_id="01AUDIO", mode_id="vocals", quality_id="balanced"),
            instant_executor,
        )
        await recorder.wait_for_terminal(job.id)
        assert manager.get(job.id).state is JobState.COMPLETED
    finally:
        await manager.aclose()
