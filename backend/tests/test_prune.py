"""Tests for prune planning/removal and ``POST /api/v1/system/prune`` (feature 060).

Two tiers, the split ``test_disk_usage.py`` established for its own feature:

- Unit tests drive :func:`straticate.system.prune.plan_prune` and
  :func:`~straticate.system.prune.execute_prune` directly against a
  hand-built ``tmp_path`` tree — no application, no separator, just files
  this test writes. Planning and removal are separate functions precisely so
  a test can assert *what would be removed* without removing it.
- HTTP tests run the **real** application (the ``test_api_jobs.py`` pattern:
  a real job manager on the test's own event loop, a real ``FakeSeparator``
  writing real stems, a real export build), because the properties that
  matter most here — a running job's tree is untouched, an export cache is
  rebuildable, a pruned job stays gone across a restart — are not properties
  of a function, they are properties of the application.

The headline is fail-first: before the route existed,
``POST /api/v1/system/prune`` answered ``404 not_found`` (captured against a
real app instance with ``api/system.py`` reverted to its pre-060 state; the
path was genuinely new — no other method was registered on it, so it is not
even a ``405``).
"""

import asyncio
import json
import os
import shutil
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest

from straticate.audio import AudioStore
from straticate.inference import (
    FAKE_ARCHITECTURE,
    SeparationProgress,
    Separator,
    SeparatorInfo,
    SeparatorRegistry,
    fake_separator_builder,
)
from straticate.jobs import CancellationToken
from straticate.jobs.layout import job_exports_dir, job_output_dir, job_record_path
from straticate.schemas import (
    Job,
    JobState,
    Model,
    PruneRequest,
    ReclaimClass,
    SeparationConfiguration,
    SeparationResult,
)
from straticate.system.prune import (
    PARTIALLY_REMOVED,
    UNREADABLE,
    PrunePlan,
    execute_prune,
    is_expired,
    plan_prune,
)
from tests.restart_harness import running_app, write_job_record
from tests.test_api_jobs import (
    JOBS_URL,
    WAIT_TIMEOUT,
    EventRecorder,
    build_app,
    configuration,
    create_job,
    manager_of,
    register_audio,
)

PRUNE_URL = "/api/v1/system/prune"
DISK_USAGE_URL = "/api/v1/system/disk-usage"

EVERYTHING: dict[str, Any] = {"export_caches": True, "orphans": True, "terminal_jobs": True}
"""Every class enabled, as a request body."""

ALL_CLASSES = PruneRequest(export_caches=True, orphans=True, terminal_jobs=True)
"""Every class enabled, as a parsed request (for the unit tier)."""


# ============================================================================
# Fixtures shared by both tiers
# ============================================================================


def write(path: Path, content: bytes = b"x") -> Path:
    """Write ``content`` at ``path``, creating parents. Returns the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def measure(path: Path) -> tuple[int, int]:
    """Independent ``(file count, total bytes)`` for a tree, via a plain ``os.walk``.

    Deliberately not a call into the module under test: a bug in the
    implementation's own arithmetic would otherwise pass against itself.
    """
    if path.is_file():
        return 1, path.stat().st_size
    files = 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for filename in filenames:
            files += 1
            total += (Path(dirpath) / filename).stat().st_size
    return files, total


def a_job(
    job_id: str,
    *,
    state: JobState = JobState.COMPLETED,
    finished_at: datetime | None = None,
    audio_id: str = "aud1",
) -> Job:
    """A minimal :class:`Job` for the planner, which reads only id/state/finished_at."""
    created = datetime(2026, 1, 1, tzinfo=UTC)
    return Job(
        id=job_id,
        audio_id=audio_id,
        configuration=SeparationConfiguration(
            audio_id=audio_id, mode_id="vocals", quality_id="balanced"
        ),
        model_id="fake-vocals-001",
        state=state,
        progress=1.0 if state.is_terminal else 0.0,
        created_at=created,
        started_at=created,
        finished_at=finished_at,
        error=None,
        result=None,
    )


NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def plan(
    data_dir: Path,
    request: PruneRequest,
    *,
    audio_ids: tuple[str, ...] = (),
    jobs: tuple[Job, ...] = (),
    now: datetime = NOW,
) -> PrunePlan:
    """Plan a prune over ``data_dir``, with the live registries named inline."""
    return plan_prune(data_dir, request, live_audio_ids=audio_ids, jobs=jobs, now=now)


def targets_of(planned: PrunePlan, reclaim_class: ReclaimClass) -> list[str]:
    """The ``data_dir``-relative targets one class planned, sorted."""
    return sorted(
        target.target for target in planned.targets if target.reclaim_class is reclaim_class
    )


# ============================================================================
# Unit tier: planning
# ============================================================================


def test_an_empty_request_plans_nothing_at_all(tmp_path: Path) -> None:
    """The safe default: a request that names no class removes nothing."""
    write(tmp_path / "audio" / "ghost" / "original.wav", b"orphaned")
    write(tmp_path / "jobs" / "ghost" / "job.json", b"{}")

    planned = plan(tmp_path, PruneRequest())

    assert planned.targets == ()
    assert planned.failures == ()


def test_export_caches_plans_only_terminal_jobs_exports(tmp_path: Path) -> None:
    write(tmp_path / "jobs" / "done" / "job.json", b"{...}")
    write(tmp_path / "jobs" / "done" / "stems" / "vocals.wav", b"0123456789")
    write(tmp_path / "jobs" / "done" / "exports" / "wav_pcm24-vocals.wav", b"012345678901234")
    write(tmp_path / "jobs" / "busy" / "job.json", b"{...}")
    write(tmp_path / "jobs" / "busy" / "exports" / "half.wav", b"nope")

    planned = plan(
        tmp_path,
        PruneRequest(export_caches=True),
        jobs=(a_job("done"), a_job("busy", state=JobState.SEPARATING)),
    )

    assert targets_of(planned, ReclaimClass.EXPORT_CACHES) == ["jobs/done/exports"]
    assert planned.targets[0].files == 1
    assert planned.targets[0].bytes == 15


def test_export_caches_skips_a_job_with_no_exports_directory(tmp_path: Path) -> None:
    write(tmp_path / "jobs" / "done" / "job.json", b"{...}")

    planned = plan(tmp_path, PruneRequest(export_caches=True), jobs=(a_job("done"),))

    assert planned.targets == ()


def test_orphans_plans_record_less_directories_and_debris(tmp_path: Path) -> None:
    # Live upload with a stray sidecar temp file, live job with a stray .part.
    write(tmp_path / "audio" / "aud1" / "original.wav", b"real")
    write(tmp_path / "audio" / "aud1" / "audio.json.abc.tmp", b"unfinished")
    write(tmp_path / "audio" / "gone" / "original.wav", b"orphaned-upload")
    write(tmp_path / "jobs" / "done" / "job.json", b"{...}")
    write(tmp_path / "jobs" / "done" / "exports" / "a.wav.dead.part", b"leftover")
    write(tmp_path / "jobs" / "vanished" / "stems" / "x.wav", b"orphaned-job")

    planned = plan(tmp_path, PruneRequest(orphans=True), audio_ids=("aud1",), jobs=(a_job("done"),))

    assert targets_of(planned, ReclaimClass.ORPHANS) == [
        "audio/aud1/audio.json.abc.tmp",
        "audio/gone",
        "jobs/done/exports/a.wav.dead.part",
        "jobs/vanished",
    ]


def test_a_build_staging_directory_is_planned_as_one_directory(tmp_path: Path) -> None:
    """Removing its members and leaving the empty directory would not be idempotent."""
    write(tmp_path / "jobs" / "done" / "job.json", b"{...}")
    write(tmp_path / "jobs" / "done" / "exports" / ".build-xy" / "vocals.wav", b"half")
    write(tmp_path / "jobs" / "done" / "exports" / ".build-xy" / "drums.wav", b"encoded")

    planned = plan(tmp_path, PruneRequest(orphans=True), jobs=(a_job("done"),))

    assert targets_of(planned, ReclaimClass.ORPHANS) == ["jobs/done/exports/.build-xy"]
    (target,) = planned.targets
    assert target.is_directory
    assert (target.files, target.bytes) == (2, len(b"half") + len(b"encoded"))


def test_a_running_jobs_directory_is_never_planned_in_any_class(tmp_path: Path) -> None:
    """A separator writes `{stem}.wav.part` — live output that *looks* like debris."""
    write(tmp_path / "jobs" / "busy" / "job.json", b"{...}")
    write(tmp_path / "jobs" / "busy" / "stems" / "vocals.wav.part", b"in-flight")
    write(tmp_path / "jobs" / "busy" / "exports" / "old.wav", b"cached")

    planned = plan(tmp_path, ALL_CLASSES, jobs=(a_job("busy", state=JobState.SEPARATING),))

    assert planned.targets == ()
    assert planned.failures == ()


def test_a_queued_jobs_directory_is_never_planned_either(tmp_path: Path) -> None:
    write(tmp_path / "jobs" / "waiting" / "job.json", b"{...}")

    planned = plan(tmp_path, ALL_CLASSES, jobs=(a_job("waiting", state=JobState.QUEUED),))

    assert planned.targets == ()


def test_terminal_jobs_takes_the_whole_directory_and_the_others_stand_down(
    tmp_path: Path,
) -> None:
    """The overlap rule: nothing is counted twice, so the report's totals are a sum."""
    write(tmp_path / "jobs" / "done" / "job.json", b"{...}")
    write(tmp_path / "jobs" / "done" / "stems" / "vocals.wav", b"0123456789")
    write(tmp_path / "jobs" / "done" / "exports" / "a.wav", b"export")
    write(tmp_path / "jobs" / "done" / "exports" / "a.wav.dead.part", b"debris")

    planned = plan(tmp_path, ALL_CLASSES, jobs=(a_job("done"),))

    (target,) = planned.targets
    assert target.reclaim_class is ReclaimClass.TERMINAL_JOBS
    assert target.target == "jobs/done"
    assert (target.files, target.bytes) == measure(tmp_path / "jobs" / "done")


def test_export_caches_and_orphans_do_not_double_count_the_same_exports(
    tmp_path: Path,
) -> None:
    """`exports/` goes whole to export_caches; the debris inside it is not counted again."""
    write(tmp_path / "jobs" / "done" / "job.json", b"{...}")
    write(tmp_path / "jobs" / "done" / "exports" / "a.wav", b"export")
    write(tmp_path / "jobs" / "done" / "exports" / "a.wav.dead.part", b"debris")
    write(tmp_path / "jobs" / "done" / "stems" / "vocals.wav.tmp", b"stem-debris")

    planned = plan(tmp_path, PruneRequest(export_caches=True, orphans=True), jobs=(a_job("done"),))

    assert targets_of(planned, ReclaimClass.EXPORT_CACHES) == ["jobs/done/exports"]
    assert targets_of(planned, ReclaimClass.ORPHANS) == ["jobs/done/stems/vocals.wav.tmp"]
    planned_bytes = sum(target.bytes for target in planned.targets)
    exports_files, exports_bytes = measure(tmp_path / "jobs" / "done" / "exports")
    assert exports_files == 2
    assert planned_bytes == exports_bytes + len(b"stem-debris")


def test_older_than_seconds_selects_only_jobs_outside_the_window(tmp_path: Path) -> None:
    write(tmp_path / "jobs" / "old" / "job.json", b"{...}")
    write(tmp_path / "jobs" / "new" / "job.json", b"{...}")
    old = a_job("old", finished_at=NOW - timedelta(hours=2))
    new = a_job("new", finished_at=NOW - timedelta(seconds=5))

    planned = plan(
        tmp_path, PruneRequest(terminal_jobs=True, older_than_seconds=3600), jobs=(old, new)
    )

    assert targets_of(planned, ReclaimClass.TERMINAL_JOBS) == ["jobs/old"]


def test_a_terminal_job_with_no_finished_at_is_kept_while_a_window_is_set() -> None:
    """Unknown age must not read as "old enough" — that deletes what cannot be reasoned about."""
    undated = a_job("mystery", finished_at=None)

    assert is_expired(undated, None, NOW) is True
    assert is_expired(undated, 0, NOW) is False


def test_a_selected_job_whose_directory_is_already_gone_is_still_planned(
    tmp_path: Path,
) -> None:
    """Its manager entry is the only way it leaves `GET /jobs`; removing zero bytes is fine."""
    planned = plan(tmp_path, PruneRequest(terminal_jobs=True), jobs=(a_job("ghost"),))

    (target,) = planned.targets
    assert target.job_id == "ghost"
    assert (target.files, target.bytes) == (0, 0)


def test_an_unreadable_directory_is_refused_rather_than_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never delete on the strength of an incomplete picture (059's deferred call)."""
    write(tmp_path / "audio" / "gone" / "original.wav", b"orphaned-upload")
    real_walk = os.walk

    def failing_walk(top: Any, onerror: Any = None, **kwargs: Any) -> Any:
        if Path(cast(str, top)).name == "gone":
            if onerror is not None:
                onerror(PermissionError(13, "Permission denied", str(top)))
            return iter(())
        return real_walk(top, onerror=onerror, **kwargs)

    monkeypatch.setattr(os, "walk", failing_walk)
    planned = plan(tmp_path, PruneRequest(orphans=True))

    assert planned.targets == ()
    (failure,) = planned.failures
    assert failure.target == "audio/gone"
    assert failure.reason == UNREADABLE
    assert failure.reclaim_class is ReclaimClass.ORPHANS


def test_an_unlistable_root_refuses_the_whole_root_rather_than_guessing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Orphan-ness is decided from a listing; without one there are no orphans."""
    (tmp_path / "audio").mkdir()
    write(tmp_path / "audio" / "gone" / "original.wav", b"orphaned-upload")

    def failing_iterdir(self: Path) -> Any:
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "iterdir", failing_iterdir)
    planned = plan(tmp_path, PruneRequest(orphans=True))

    assert planned.targets == ()
    assert [(failure.target, failure.reason) for failure in planned.failures] == [
        ("audio", UNREADABLE)
    ]


# ============================================================================
# Unit tier: removal
# ============================================================================


def test_execute_removes_the_planned_targets_and_totals_them_per_class(
    tmp_path: Path,
) -> None:
    write(tmp_path / "audio" / "gone" / "original.wav", b"orphaned-upload")
    write(tmp_path / "jobs" / "done" / "job.json", b"{...}")
    write(tmp_path / "jobs" / "done" / "exports" / "a.wav", b"export")
    expected_orphan = measure(tmp_path / "audio" / "gone")
    expected_exports = measure(tmp_path / "jobs" / "done" / "exports")

    planned = plan(tmp_path, PruneRequest(export_caches=True, orphans=True), jobs=(a_job("done"),))
    totals, failures = execute_prune(planned.targets)

    assert failures == []
    assert totals[ReclaimClass.ORPHANS] == expected_orphan
    assert totals[ReclaimClass.EXPORT_CACHES] == expected_exports
    assert not (tmp_path / "audio" / "gone").exists()
    assert not (tmp_path / "jobs" / "done" / "exports").exists()
    assert (tmp_path / "jobs" / "done" / "job.json").is_file()


def test_a_directory_that_survives_removal_is_subtracted_not_assumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows locked-file case, made reproducible: report what went, not what was planned.

    ``rmtree(..., ignore_errors=True)`` cannot fail, so a tolerated failure
    would otherwise be reported as a successful removal of bytes that are
    still on disk — and this report's whole value is that its numbers can be
    trusted against a disk-usage reading.
    """
    write(tmp_path / "audio" / "gone" / "original.wav", b"orphaned-upload")
    planned = plan(tmp_path, PruneRequest(orphans=True))

    def refuse_to_remove(path: Any, **kwargs: Any) -> None:
        """``ignore_errors=True`` swallowing every file, which cannot be signalled."""

    monkeypatch.setattr(shutil, "rmtree", refuse_to_remove)
    totals, failures = execute_prune(planned.targets)

    assert totals[ReclaimClass.ORPHANS] == (0, 0)
    assert [failure.reason for failure in failures] == [PARTIALLY_REMOVED]
    assert (tmp_path / "audio" / "gone" / "original.wav").is_file()


# ============================================================================
# Unit tier: the in-flight upload reservation
# ============================================================================


def test_an_upload_being_written_is_pending_until_it_is_registered(tmp_path: Path) -> None:
    """The one thing the filesystem cannot say: "arriving" and "abandoned" look identical."""
    store = AudioStore(tmp_path)
    audio_id = store.new_id()
    path = store.prepare_original_path(audio_id, "song.wav")
    path.write_bytes(b"partially uploaded")

    assert store.pending_ids() == [audio_id]
    assert store.ids() == []

    planned = plan(tmp_path, PruneRequest(orphans=True), audio_ids=(audio_id,))
    assert planned.targets == ()

    # Without the reservation the very same directory is an orphan.
    assert targets_of(plan(tmp_path, PruneRequest(orphans=True)), ReclaimClass.ORPHANS) == [
        f"audio/{audio_id}"
    ]

    store.remove_files(audio_id)
    assert store.pending_ids() == []


# ============================================================================
# HTTP tier: the real application
# ============================================================================


class GatedFakeSeparator:
    """The real ``FakeSeparator``, held at the gate before it writes anything.

    Wrapping rather than replacing matters: the job really does produce real
    stems once released, so a test can prune *while it runs* and then assert
    that the stems it went on to write are served.
    """

    def __init__(self, inner: Separator, started: asyncio.Event, gate: asyncio.Event) -> None:
        self._inner = inner
        self.started = started
        self.gate = gate

    @property
    def info(self) -> SeparatorInfo:
        return self._inner.info

    def runtime_stats(self) -> None:
        return None

    async def separate(
        self,
        input_path: Path,
        configuration: SeparationConfiguration,
        progress_callback: Callable[[SeparationProgress], None],
        cancellation_token: CancellationToken,
        *,
        job_id: str,
        output_dir: Path,
        stage_callback: Callable[[JobState], None] | None = None,
    ) -> SeparationResult:
        self.started.set()
        await self.gate.wait()
        return await self._inner.separate(
            input_path,
            configuration,
            progress_callback,
            cancellation_token,
            job_id=job_id,
            output_dir=output_dir,
            stage_callback=stage_callback,
        )


def gated_fake_registry(started: asyncio.Event, gate: asyncio.Event) -> SeparatorRegistry:
    """A registry whose fake models run the real separator, behind a gate."""
    build_fake = fake_separator_builder(
        chunk_seconds=0.2, chunk_delay_seconds=0.0, model_load_seconds=0.0
    )

    def build(model: Model) -> GatedFakeSeparator:
        return GatedFakeSeparator(build_fake(model), started, gate)

    return SeparatorRegistry({FAKE_ARCHITECTURE: build})


async def run_to_completion(
    client: httpx2.AsyncClient, recorder: EventRecorder, audio_id: str
) -> str:
    created = await create_job(client, **configuration(audio_id))
    job_id = cast(str, created["id"])
    terminal = await recorder.wait_for_terminal(job_id)
    assert terminal.type == "job_completed", terminal
    return job_id


async def export_one_stem(client: httpx2.AsyncClient, job_id: str) -> int:
    """Build (or serve) a single-stem export; returns its byte length."""
    response = await client.get(
        f"{JOBS_URL}/{job_id}/export", params={"format": "wav_pcm24", "stems": "vocals"}
    )
    assert response.status_code == 200, response.text
    return len(response.content)


async def prune(client: httpx2.AsyncClient, **body: Any) -> dict[str, Any]:
    response = await client.post(PRUNE_URL, json=body)
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


@pytest.fixture
async def prune_client(tmp_path: Path) -> AsyncIterator[httpx2.AsyncClient]:
    async with running_app(build_app(tmp_path)) as client:
        yield client


async def test_prune_route_is_present_and_an_empty_request_frees_nothing(
    prune_client: httpx2.AsyncClient,
) -> None:
    """The headline. Fail-first: without the route this was `404 not_found`."""
    response = await prune_client.post(PRUNE_URL, json={})
    assert response.status_code == 200, response.text

    payload = cast(dict[str, Any], response.json())
    assert set(payload) == {
        "export_caches",
        "orphans",
        "terminal_jobs",
        "items_removed",
        "bytes_freed",
        "failures",
    }
    for name in ("export_caches", "orphans", "terminal_jobs"):
        assert payload[name] == {"items_removed": 0, "bytes_freed": 0}
    assert payload["items_removed"] == 0
    assert payload["bytes_freed"] == 0
    assert payload["failures"] == []


async def test_pruning_export_caches_leaves_the_stems_and_the_export_rebuilds(
    tmp_path: Path,
) -> None:
    """The cache-rebuildable proof: what a prune removes here, a request puts back."""
    app = build_app(tmp_path)
    audio = register_audio(app)
    async with running_app(app) as client:
        recorder = EventRecorder()
        manager_of(app).add_listener(recorder)
        job_id = await run_to_completion(client, recorder, audio)
        exported_bytes = await export_one_stem(client, job_id)

        exports = job_exports_dir(tmp_path, job_id)
        expected_files, expected_bytes = measure(exports)
        assert expected_files == 1

        report = await prune(client, export_caches=True)

        assert report["export_caches"] == {
            "items_removed": expected_files,
            "bytes_freed": expected_bytes,
        }
        assert report["failures"] == []
        assert not exports.exists()
        # The job itself is untouched: record, stems, and every endpoint.
        assert job_record_path(tmp_path, job_id).is_file()
        assert any((job_output_dir(tmp_path, job_id) / "stems").iterdir())
        assert (await client.get(f"{JOBS_URL}/{job_id}")).status_code == 200

        # And the cache really was only a cache.
        assert await export_one_stem(client, job_id) == exported_bytes
        assert exports.is_dir()


async def test_pruning_everything_while_a_job_runs_leaves_that_job_alone(
    tmp_path: Path,
) -> None:
    """A running job's `.part` files are live output, not debris. It must finish and serve."""
    app = build_app(tmp_path)
    started, gate = asyncio.Event(), asyncio.Event()
    app.state.separator_registry = gated_fake_registry(started, gate)
    audio = register_audio(app)

    async with running_app(app) as client:
        recorder = EventRecorder()
        manager_of(app).add_listener(recorder)
        created = await create_job(client, **configuration(audio))
        job_id = cast(str, created["id"])
        await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT)

        directory = job_output_dir(tmp_path, job_id)
        in_flight = write(directory / "stems" / "vocals.wav.part", b"half a stem")

        report = await prune(client, older_than_seconds=None, **EVERYTHING)

        assert report["items_removed"] == 0
        assert report["bytes_freed"] == 0
        assert report["failures"] == []
        assert job_record_path(tmp_path, job_id).is_file()
        assert in_flight.is_file()

        gate.set()
        terminal = await recorder.wait_for_terminal(job_id)
        assert terminal.type == "job_completed", terminal

        stem = await client.get(f"{JOBS_URL}/{job_id}/stems/vocals")
        assert stem.status_code == 200, stem.text
        assert len(stem.content) > 0


async def test_pruning_orphans_removes_debris_and_spares_everything_live(
    tmp_path: Path,
) -> None:
    app = build_app(tmp_path)
    audio = register_audio(app)
    async with running_app(app) as client:
        recorder = EventRecorder()
        manager_of(app).add_listener(recorder)
        job_id = await run_to_completion(client, recorder, audio)
        await export_one_stem(client, job_id)

        orphan_audio = write(
            tmp_path / "audio" / "01ORPHANAUDIOID000000000" / "original.wav", b"orphaned-upload"
        )
        orphan_job = write(
            tmp_path / "jobs" / "01ORPHANJOBID0000000000" / "stems" / "x.wav", b"orphaned-job"
        )
        stray_part = write(
            job_exports_dir(tmp_path, job_id) / "wav_pcm24-vocals.wav.deadbeef.part",
            b"leftover-build",
        )
        stray_tmp = write(
            tmp_path / "audio" / audio / "audio.json.deadbeef.tmp", b"unfinished-sidecar"
        )
        staging = write(
            job_exports_dir(tmp_path, job_id) / ".build-xy" / "vocals.wav", b"half-encoded"
        )

        expected_files = 0
        expected_bytes = 0
        for path in (
            orphan_audio.parent,
            orphan_job.parent.parent,
            stray_part,
            stray_tmp,
            staging.parent,
        ):
            files, size = measure(path)
            expected_files += files
            expected_bytes += size

        report = await prune(client, orphans=True)

        assert report["orphans"] == {
            "items_removed": expected_files,
            "bytes_freed": expected_bytes,
        }
        assert report["items_removed"] == expected_files
        assert report["failures"] == []

        for gone in (orphan_audio.parent, orphan_job.parent.parent, stray_part, staging.parent):
            assert not gone.exists()
        assert not stray_tmp.exists()

        # Everything live survives, endpoints included.
        assert job_record_path(tmp_path, job_id).is_file()
        assert (await client.get(f"{JOBS_URL}/{job_id}")).status_code == 200
        assert (await client.get(f"/api/v1/audio/{audio}")).status_code == 200
        assert await export_one_stem(client, job_id) > 0

        # And the report agrees with the read it is the write half of.
        usage = cast(dict[str, Any], (await client.get(DISK_USAGE_URL)).json())
        assert usage["orphans"] == {"count": 0, "bytes": 0}
        assert usage["complete"] is True


async def test_a_second_identical_prune_frees_nothing(tmp_path: Path) -> None:
    """Idempotence, stated in numbers: the first request really did remove it all."""
    app = build_app(tmp_path)
    audio = register_audio(app)
    async with running_app(app) as client:
        recorder = EventRecorder()
        manager_of(app).add_listener(recorder)
        job_id = await run_to_completion(client, recorder, audio)
        await export_one_stem(client, job_id)
        write(tmp_path / "audio" / "01ORPHANAUDIOID000000000" / "original.wav", b"orphan")

        first = await prune(client, **EVERYTHING)
        assert first["items_removed"] > 0
        assert first["bytes_freed"] > 0

        second = await prune(client, **EVERYTHING)
        assert second["items_removed"] == 0
        assert second["bytes_freed"] == 0
        assert second["failures"] == []


async def test_the_totals_are_the_sum_of_the_classes_and_match_an_independent_walk(
    tmp_path: Path,
) -> None:
    """Ground truth: what the report claims it freed is what was measurably there."""
    app = build_app(tmp_path)
    audio = register_audio(app)
    async with running_app(app) as client:
        recorder = EventRecorder()
        manager_of(app).add_listener(recorder)
        job_id = await run_to_completion(client, recorder, audio)
        await export_one_stem(client, job_id)
        write(tmp_path / "audio" / "01ORPHANAUDIOID000000000" / "original.wav", b"orphaned")

        job_files, job_bytes = measure(job_output_dir(tmp_path, job_id))
        orphan_files, orphan_bytes = measure(tmp_path / "audio" / "01ORPHANAUDIOID000000000")

        report = await prune(client, **EVERYTHING)

        # `terminal_jobs` claims the whole job directory, exports included, so
        # `export_caches` reports nothing rather than the same bytes twice.
        assert report["terminal_jobs"] == {"items_removed": job_files, "bytes_freed": job_bytes}
        assert report["export_caches"] == {"items_removed": 0, "bytes_freed": 0}
        assert report["orphans"] == {
            "items_removed": orphan_files,
            "bytes_freed": orphan_bytes,
        }
        assert report["items_removed"] == job_files + orphan_files
        assert report["bytes_freed"] == job_bytes + orphan_bytes

        # The upload is live and untouched — nothing asked for it.
        assert (await client.get(f"/api/v1/audio/{audio}")).status_code == 200


async def test_terminal_jobs_honours_the_retention_window(tmp_path: Path) -> None:
    """Two jobs, one aged by a hand-written record the next process restores."""
    first = build_app(tmp_path)
    audio = register_audio(first)
    async with running_app(first) as client:
        recorder = EventRecorder()
        manager_of(first).add_listener(recorder)
        recent_id = await run_to_completion(client, recorder, audio)
        record = cast(
            dict[str, Any],
            json.loads(job_record_path(tmp_path, recent_id).read_text(encoding="utf-8")),
        )
    del first

    # An older twin of the same completed job, written straight to disk the way
    # a previous run would have left it (tests/restart_harness.py).
    old_id = "01OLDJOBID00000000000000"
    aged = dict(record)
    aged["id"] = old_id
    aged["finished_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    write_job_record(tmp_path, aged)
    write(job_output_dir(tmp_path, old_id) / "stems" / "vocals.wav", b"an old stem")
    old_files, old_bytes = measure(job_output_dir(tmp_path, old_id))

    async with running_app(build_app(tmp_path)) as client:
        listed = {job["id"] for job in cast(list[Any], (await client.get(JOBS_URL)).json())}
        assert listed == {recent_id, old_id}

        report = await prune(client, terminal_jobs=True, older_than_seconds=3600)

        assert report["terminal_jobs"] == {
            "items_removed": old_files,
            "bytes_freed": old_bytes,
        }
        assert not job_output_dir(tmp_path, old_id).exists()
        assert job_output_dir(tmp_path, recent_id).exists()
        assert [job["id"] for job in cast(list[Any], (await client.get(JOBS_URL)).json())] == [
            recent_id
        ]


async def test_a_pruned_job_stays_gone_across_a_restart_and_the_others_survive(
    tmp_path: Path,
) -> None:
    first = build_app(tmp_path)
    audio = register_audio(first)
    async with running_app(first) as client:
        recorder = EventRecorder()
        manager_of(first).add_listener(recorder)
        pruned_id = await run_to_completion(client, recorder, audio)
        kept_id = await run_to_completion(client, recorder, audio)

        # Age only the first one, so the window is what decides.
        record = job_record_path(tmp_path, pruned_id)
        aged = cast(dict[str, Any], json.loads(record.read_text(encoding="utf-8")))
        aged["finished_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        record.write_text(json.dumps(aged), encoding="utf-8")
    del first

    async with running_app(build_app(tmp_path)) as client:
        report = await prune(client, terminal_jobs=True, older_than_seconds=3600)
        assert report["terminal_jobs"]["items_removed"] > 0

    async with running_app(build_app(tmp_path)) as client:
        assert [job["id"] for job in cast(list[Any], (await client.get(JOBS_URL)).json())] == [
            kept_id
        ]
        assert (await client.get(f"{JOBS_URL}/{pruned_id}")).status_code == 404
        assert job_output_dir(tmp_path, kept_id).exists()


async def test_an_upload_still_being_written_is_not_an_orphan(tmp_path: Path) -> None:
    """The reservation, end to end: a directory with bytes and no record yet survives."""
    app = build_app(tmp_path)
    async with running_app(app) as client:
        store = cast(AudioStore, app.state.audio_store)
        audio_id = store.new_id()
        arriving = store.prepare_original_path(audio_id, "song.wav")
        arriving.write_bytes(b"the first megabyte of a long upload")

        report = await prune(client, orphans=True)

        assert report["orphans"] == {"items_removed": 0, "bytes_freed": 0}
        assert arriving.is_file()


async def test_a_refused_target_is_reported_and_costs_the_rest_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One unreadable orphan must not stop the readable one from being reclaimed."""
    app = build_app(tmp_path)
    async with running_app(app) as client:
        readable = write(tmp_path / "audio" / "01READABLE00000000000000" / "a.wav", b"orphaned")
        write(tmp_path / "audio" / "01UNREADABLE000000000000" / "b.wav", b"also-orphaned")
        real_walk = os.walk

        def failing_walk(top: Any, onerror: Any = None, **kwargs: Any) -> Any:
            if "UNREADABLE" in str(top):
                if onerror is not None:
                    onerror(PermissionError(13, "Permission denied", str(top)))
                return iter(())
            return real_walk(top, onerror=onerror, **kwargs)

        monkeypatch.setattr(os, "walk", failing_walk)
        report = await prune(client, orphans=True)

        assert report["orphans"] == {"items_removed": 1, "bytes_freed": len(b"orphaned")}
        assert report["failures"] == [
            {
                "reclaim_class": "orphans",
                "target": "audio/01UNREADABLE000000000000",
                "reason": UNREADABLE,
            }
        ]
        assert not readable.parent.exists()
        assert (tmp_path / "audio" / "01UNREADABLE000000000000" / "b.wav").is_file()


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"older_than_seconds": 60}, id="window-without-terminal-jobs"),
        pytest.param({"terminal_jobs": True, "older_than_seconds": -1}, id="negative-window"),
        pytest.param({"exports": True}, id="misspelled-class"),
        pytest.param({"orphans": "yes please"}, id="wrong-type"),
    ],
)
async def test_a_request_that_cannot_mean_what_it_says_is_refused(
    prune_client: httpx2.AsyncClient, body: dict[str, Any]
) -> None:
    """A prune that silently does nothing reads exactly like one that found nothing."""
    response = await prune_client.post(PRUNE_URL, json=body)
    assert response.status_code == 422, response.text
    assert cast(dict[str, Any], response.json())["error"]["code"] == "validation_error"


async def test_the_prune_runs_off_the_event_loop(
    prune_client: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planning is a full walk of `data_dir`; the loop must keep serving while it runs.

    The same proof feature 040 established for `/system/storage` and 059
    reused for `/system/disk-usage`: park the blocking half on a
    `threading.Event` and show `/health` still answers. The wait is bounded,
    so a regression fails in seconds rather than hanging the suite.
    """
    import threading

    from straticate.api import system as system_api
    from straticate.system.prune import PrunePlan

    entered = threading.Event()
    release = threading.Event()

    def parked_plan(*args: Any, **kwargs: Any) -> PrunePlan:
        entered.set()
        release.wait(5.0)
        return PrunePlan(targets=(), failures=())

    monkeypatch.setattr(system_api, "plan_prune", parked_plan)
    task = asyncio.create_task(prune_client.post(PRUNE_URL, json={"orphans": True}))
    try:
        await asyncio.wait_for(asyncio.to_thread(entered.wait, 5.0), timeout=5.0)
        assert entered.is_set(), "planning never started"
        health = await asyncio.wait_for(prune_client.get("/api/v1/health"), timeout=2.0)
        assert health.status_code == 200
    finally:
        release.set()
        response = await asyncio.wait_for(task, timeout=5.0)
    assert response.status_code == 200
