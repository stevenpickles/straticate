"""Adapter turning any :class:`~straticate.inference.base.Separator` into a job executor.

This is the whole coupling between the job engine (feature 012) and the
separation engine: the job manager knows nothing about models, and separators
know nothing about jobs, events or WebSockets. Everything in between is the
three forwardings below.

- **Stages.** The manager has already moved the job to ``preparing`` before the
  executor runs. Every later stage is announced by the separator itself
  through the :data:`~straticate.inference.base.StageCallback` and forwarded
  verbatim to :meth:`straticate.jobs.JobContext.set_stage`. The adapter never
  invents a stage, so the job's stage history only ever claims work that was
  really performed (skipping forward is legal —
  :class:`~straticate.inference.fake.FakeSeparator` announces
  ``decoding → loading_model → separating → post_processing → encoding``,
  while a separator that does no post-processing simply never claims it).
- **Progress.** Each :class:`~straticate.inference.base.SeparationProgress`
  maps one-to-one onto
  :meth:`straticate.jobs.JobContext.report_progress`; throttling for the wire
  stays the manager's job.
- **Cancellation.** The context's token is handed straight to the separator.

Threading: the manager's context methods must be called on its event loop, but
a separator doing real compute may run its chunk loop in a worker thread. Both
callbacks therefore detect an off-loop call and marshal it with
``loop.call_soon_threadsafe`` — a real separator (feature 026) can offload
freely without knowing any of this.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from straticate.inference.base import SeparationProgress, Separator
from straticate.inference.layout import job_stems_dir
from straticate.jobs.manager import JobContext
from straticate.schemas.jobs import Job, JobState, SeparationResult


class SeparatorJobExecutor:
    """A :class:`~straticate.jobs.JobExecutor` backed by a ``Separator``.

    One instance per job: the input audio is resolved by the caller (feature
    015, from the :class:`~straticate.audio.AudioStore`), so this adapter never
    needs an audio registry. The separator itself is long-lived and may be
    shared across executors — the job manager runs one job at a time
    (ARCHITECTURE.md §6), which is exactly the separator's own concurrency
    contract.

    Usage (feature 015)::

        executor = SeparatorJobExecutor(separator, input_path=path, data_dir=settings.data_dir)
        job = manager.submit(configuration, executor, model_id=separator.info.model_id)

    Args:
        separator: The inference backend to run.
        input_path: The source media file for this job.
        data_dir: Application data directory; stems are written under
            ``{data_dir}/jobs/{job_id}/stems`` (see
            :mod:`straticate.inference.layout`).
    """

    __slots__ = ("_data_dir", "_input_path", "_separator")

    def __init__(self, separator: Separator, *, input_path: Path, data_dir: Path) -> None:
        self._separator = separator
        self._input_path = input_path
        self._data_dir = data_dir

    @property
    def separator(self) -> Separator:
        """The separator this executor drives (feature 019 polls its stats)."""
        return self._separator

    def output_dir(self, job_id: str) -> Path:
        """The directory this executor writes ``job_id``'s stems into."""
        return job_stems_dir(self._data_dir, job_id)

    async def __call__(self, job: Job, context: JobContext) -> SeparationResult:
        """Run the separation for ``job``, forwarding stages and progress."""
        loop = asyncio.get_running_loop()

        def on_stage(stage: JobState) -> None:
            _on_loop(loop, lambda: context.set_stage(stage))

        def on_progress(progress: SeparationProgress) -> None:
            _on_loop(
                loop,
                lambda: context.report_progress(
                    progress.fraction,
                    progress.chunks_completed,
                    progress.chunks_total,
                    progress.audio_processed_seconds,
                    progress.audio_total_seconds,
                ),
            )

        return await self._separator.separate(
            self._input_path,
            job.configuration,
            on_progress,
            context.cancellation,
            job_id=job.id,
            output_dir=self.output_dir(job.id),
            stage_callback=on_stage,
        )


def _on_loop(loop: asyncio.AbstractEventLoop, action: Callable[[], None]) -> None:
    """Run ``action`` on ``loop`` — directly when already there, else thread-safely."""
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        action()
    else:
        loop.call_soon_threadsafe(action)


__all__ = ["SeparatorJobExecutor"]
