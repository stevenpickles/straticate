"""Feature 045: the fake separator's chunk loop must not occupy the event loop.

Feature 044 diagnosed a stall that had been read as Playwright flakiness for
three waves: ``FakeSeparator._run_chunks`` filtered every chunk inline, and the
only yield was the ``asyncio.sleep`` that came *after* all of it, so for the
length of a chunk the backend served nothing — not REST, not the feature 013
WebSocket hub, not the progress of the job doing the filtering. It measured
0.37 s per chunk on a quiet machine and 8.1 s at 17x CPU contention.

Three tests, deliberately different in kind. Two are **structural and
timing-free**, so they can never be flaky and between them pin the whole
mechanism: the filtering happens on a thread that is not the loop's, and it is
handed over in units no larger than :data:`FILTER_BLOCK_FRAMES`. Both halves are
load-bearing — feature 045 measured that a thread hop *per chunk*, which is what
:class:`~straticate.inference.torch_separator.TorchSeparator` does, leaves the
stall almost exactly where it was (a 271 ms 95th percentile became 266 ms),
because pure-Python arithmetic holds the GIL for the whole hop. It is the return
to the loop between blocks that serves requests.

The third is the **behavioural** form the feature brief asks for: something cheap
is awaited on the loop throughout a real separation, and the loop is required to
have stayed responsive while chunks were being filtered. It measures itself
against *this machine's* chunk time rather than against a constant, because both
the stall and the tolerance scale with how busy the machine is — a fixed
millisecond budget would be either meaningless on a fast host or flaky on a
loaded one.

All three fail against the pre-045 code; the mutation output is recorded in
``docs/features/045-fake-separator-event-loop.md``.
"""

import asyncio
import threading
import time
from array import array
from pathlib import Path
from typing import Any

import pytest

from straticate.inference import (
    FAKE_STANDARD_INFO,
    FakeSeparator,
    SeparationProgress,
    SeparatorInfo,
)
from straticate.inference import fake as fake_module
from straticate.inference.fake import FILTER_BLOCK_FRAMES
from straticate.jobs import CancellationToken
from straticate.schemas.jobs import JobState, SeparationConfiguration
from tests.audio_fixtures import write_tone_wav

JOB_ID = "01JOB000000000000000000000"

AUDIO_SECONDS = 3.0
"""Fixture length. Three chunks of real filtering is enough to catch a stall."""

CHUNK_SECONDS = 1.0
"""Chunk length, chosen so one chunk is unmistakably more work than a loop tick.

Four stems by two channels by 44 100 frames is ~350 k multiply-adds, tens of
milliseconds — the same shape as the five-second chunks a server runs, two
orders of magnitude smaller.
"""

EXPECTED_CHUNKS = 3
"""``AUDIO_SECONDS / CHUNK_SECONDS`` — asserted, so a fixture change is noticed."""

STALL_FRACTION = 0.5
"""Share of one chunk's compute the loop may be unavailable for (99th percentile).

Generous on purpose, and there is nothing in between for it to be generous
*to*. Before feature 045 the loop was unavailable for essentially all of every
chunk — the ticker got one turn per chunk, so the ratio is 1.0. After, the p99
gap measured 0.0007 to 0.003 of a chunk on a host at ~18x contention. The
threshold sits in a three-order-of-magnitude gap between the two.
"""


def make_separator(info: SeparatorInfo = FAKE_STANDARD_INFO, **kwargs: Any) -> FakeSeparator:
    """A fake separator with every simulated delay switched off."""
    options: dict[str, Any] = {
        "chunk_delay_seconds": 0.0,
        "model_load_seconds": 0.0,
        "chunk_seconds": CHUNK_SECONDS,
    }
    options.update(kwargs)
    return FakeSeparator(info, **options)


def make_configuration(mode_id: str = "standard_stems") -> SeparationConfiguration:
    return SeparationConfiguration(
        audio_id="01AUDIO0000000000000000000",
        mode_id=mode_id,
        quality_id="fast",
        device_id=None,
    )


def discard(progress: SeparationProgress) -> None:
    """A progress callback for tests that are not about progress."""


class LoopTicker:
    """Something cheap awaited on the event loop, recording how long it waited.

    Each iteration yields with ``asyncio.sleep(0)`` and records how long it took
    to be scheduled again, stamped so the caller can look at one stage's window.
    A *timed* sleep would measure the platform's timer granularity as much as
    the loop's health — 15.6 ms of it on Windows, comparable to a chunk at this
    fixture size — whereas a bare yield goes straight through the ready queue
    and is limited only by whether the loop is running at all.
    """

    def __init__(self) -> None:
        self.gaps: list[tuple[float, float]] = []
        self._stop = asyncio.Event()

    async def run(self) -> None:
        previous = time.perf_counter()
        while not self._stop.is_set():
            await asyncio.sleep(0)
            now = time.perf_counter()
            self.gaps.append((now, now - previous))
            previous = now

    def stop(self) -> None:
        self._stop.set()

    def worst_between(self, start: float, end: float) -> tuple[float, float]:
        """The 99th percentile and the maximum wait inside ``[start, end]``.

        The assertion is on the percentile rather than the maximum because the
        maximum measures the *host*: on a machine oversubscribed several times
        over, the whole process is descheduled for a couple of hundred
        milliseconds now and then, whatever the backend is doing. Measured on a
        host running at ~18x, over three runs: the maximum reached 0.27, 0.42
        and 0.29 of a chunk, while the 99th percentile stayed at 0.0007, 0.003
        and 0.003 of one. The failure this test exists to catch puts *every*
        sample at a full chunk, so the percentile catches it just as surely and
        does not fail for the host's reasons.
        """
        window = sorted(gap for stamp, gap in self.gaps if start <= stamp <= end)
        assert window, "the ticker never ran inside the window"
        index = min(len(window) - 1, int(0.99 * len(window)))
        return window[index], window[-1]


class StageWindow:
    """Records when the ``separating`` stage began and ended."""

    def __init__(self) -> None:
        self.start: float | None = None
        self.end: float | None = None

    def __call__(self, stage: JobState) -> None:
        if stage is JobState.SEPARATING:
            self.start = time.perf_counter()
        elif self.start is not None and self.end is None:
            self.end = time.perf_counter()


def record_filter_calls(monkeypatch: pytest.MonkeyPatch) -> tuple[list[int], set[int]]:
    """Record the size and calling thread of every comb-filter call.

    ``_CombFilter.process`` is the whole of the per-chunk compute and it predates
    this feature, so watching it observes the *behaviour* — how the work is
    handed over — rather than the mechanism. Patching the ``asyncio.to_thread``
    call site instead would only assert that the fix is the fix.

    Returns:
        The frame count of each call, and the set of thread identities they ran
        on.
    """
    sizes: list[int] = []
    threads: set[int] = set()
    original = fake_module._CombFilter.process  # pyright: ignore[reportPrivateUsage]

    def recording(
        self: fake_module._CombFilter,  # pyright: ignore[reportPrivateUsage]
        chunk: array[int],
    ) -> array[int]:
        sizes.append(len(chunk))
        threads.add(threading.get_ident())
        return original(self, chunk)

    monkeypatch.setattr(
        fake_module._CombFilter,  # pyright: ignore[reportPrivateUsage]
        "process",
        recording,
    )
    return sizes, threads


async def separate(tmp_path: Path, separator: FakeSeparator, **kwargs: Any) -> None:
    """Run one four-stem separation over a freshly written fixture."""
    source = write_tone_wav(tmp_path / "source.wav", seconds=AUDIO_SECONDS)
    await separator.separate(
        source,
        make_configuration(),
        discard,
        CancellationToken(),
        job_id=JOB_ID,
        output_dir=tmp_path / "stems",
        **kwargs,
    )


async def test_chunk_filtering_does_not_run_on_the_event_loop_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Structural, with no timing in it at all: whatever thread the comb filter
    # runs on, it must not be the one running the event loop.
    loop_thread = threading.get_ident()
    _, threads = record_filter_calls(monkeypatch)

    await separate(tmp_path, make_separator())

    assert threads, "the filter never ran — the test observed nothing"
    assert loop_thread not in threads, (
        "the per-chunk filtering ran on the event loop thread, so the backend "
        "served nothing for the length of every chunk (feature 044's stall)"
    )


async def test_the_loop_gets_its_turn_back_between_blocks_within_a_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other structural half, and the one that a thread hop per *chunk*
    # would fail: the loop is only served when the work comes back to it, so
    # what bounds a request's wait is the size of a single handover, not
    # whether a handover happened at all.
    sizes, _ = record_filter_calls(monkeypatch)

    await separate(tmp_path, make_separator())

    frames = int(AUDIO_SECONDS * FAKE_STANDARD_INFO.sample_rate)
    passes = FAKE_STANDARD_INFO.stem_count * 2  # every stem, both channels
    assert sum(sizes) == frames * passes, "every frame must still be filtered exactly once"
    assert max(sizes) <= FILTER_BLOCK_FRAMES, (
        f"the largest unit of filtering was {max(sizes)} frames against a "
        f"{FILTER_BLOCK_FRAMES}-frame block: the event loop waits that long for "
        "its turn, however many threads the work is handed to"
    )


async def test_the_loop_stays_responsive_while_chunks_are_filtered(tmp_path: Path) -> None:
    # The behavioural half: a real separation, with something cheap awaited on
    # the loop throughout, and the loop required to have kept answering it.
    separator = make_separator()
    ticker = LoopTicker()
    window = StageWindow()
    ticking = asyncio.create_task(ticker.run())

    await separate(tmp_path, separator, stage_callback=window)
    ticker.stop()
    await ticking

    assert window.start is not None and window.end is not None
    stats = separator.runtime_stats()
    assert stats is not None
    chunk_seconds = stats.processing.mean_chunk_seconds
    assert stats.processing.chunks_completed == EXPECTED_CHUNKS
    assert chunk_seconds is not None

    # A guard on the measurement rather than on the code: if a chunk were as
    # cheap as a loop tick there would be no stall to detect and the assertion
    # below would pass for the wrong reason.
    assert chunk_seconds > 0.005, (
        f"a chunk took {chunk_seconds * 1000:.1f} ms — too little work for this "
        "test to be able to see a stall; lengthen the fixture"
    )

    worst, peak = ticker.worst_between(window.start, window.end)
    assert worst < STALL_FRACTION * chunk_seconds, (
        f"the event loop was unavailable for {worst * 1000:.1f} ms (p99; peak "
        f"{peak * 1000:.1f} ms) during the chunk loop, against a mean chunk of "
        f"{chunk_seconds * 1000:.1f} ms — the per-chunk filtering is blocking "
        "the loop"
    )
