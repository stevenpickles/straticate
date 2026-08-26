"""Feature 045: pure-Python audio work must not occupy the event loop.

Feature 044 diagnosed a stall that had been read as Playwright flakiness for
three waves: ``FakeSeparator._run_chunks`` filtered every chunk inline, and the
only yield was the ``asyncio.sleep`` that came *after* all of it, so for the
length of a chunk the backend served nothing — not REST, not the feature 013
WebSocket hub, not the progress of the job doing the filtering. It measured
0.37 s per chunk on a quiet machine and 8.1 s at 17x CPU contention.

**There are two such places, and this module covers both.** The chunk loop is
one; :func:`straticate.inference.stereo.apply_stereo_handling_async` is the
other, and its 12-second fold block was the same defect on the ``mono`` path —
120.9 ms of GIL-holding work per hop, sixteen times over a three-minute track.
Both are sized by the same rule now: a block is about a millisecond, because a
millisecond is what the event loop can be asked to wait.

Three of these tests are **structural or near-structural** and pin the
mechanism: the filtering happens on a thread that is not the loop's; it is
handed over in units no larger than :data:`FILTER_BLOCK_FRAMES`; and no single
fold hop is a meaningful share of the fold. The unit-size half is load-bearing —
feature 045 measured that a thread hop *per chunk*, which is what
:class:`~straticate.inference.torch_separator.TorchSeparator` does, leaves the
stall almost exactly where it was (a 271 ms 95th percentile became 266 ms),
because pure-Python arithmetic holds the GIL for the whole hop. It is the return
to the loop between blocks that serves requests, so a test that only asks
"did it leave the loop?" passes for a version that is no better.

The fourth is the **behavioural** form the feature brief asks for: something
cheap is awaited on the loop throughout a real separation, and the loop is
required to have stayed responsive while chunks were being filtered. It measures
itself against *this machine's* chunk time rather than against a constant,
because both the stall and the tolerance scale with how busy the machine is — a
fixed millisecond budget would be either meaningless on a fast host or flaky on
a loaded one. For the same reason the fold test compares one hop against the
whole fold rather than against a number of milliseconds.

Every one fails against the code it was written for; the mutation output is
recorded in ``docs/features/045-fake-separator-event-loop.md``.
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
from straticate.inference import stereo as stereo_module
from straticate.inference.fake import FILTER_BLOCK_FRAMES
from straticate.inference.pcm import PcmAudio
from straticate.inference.stereo import FOLD_BLOCK_FRAMES, apply_stereo_handling_async
from straticate.jobs import CancellationToken
from straticate.schemas.jobs import JobState, SeparationConfiguration, StereoHandling
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
    to be scheduled again. A *timed* sleep would measure the platform's timer
    granularity as much as the loop's health — 15.6 ms of it on Windows,
    comparable to a chunk at this fixture size — whereas a bare yield goes
    straight through the ready queue and is limited only by whether the loop is
    running at all.

    It records **only while armed**, and drops everything else as it arrives.
    An earlier version stamped every sample and filtered at the end, which on
    the three-second fixture collected ~101 000 samples to read 3 300 of them:
    97% of the memory, and all of the CPU behind it, spent on the ffmpeg decode
    that runs before the window opens. That cost scales with the fixture, and
    this test's own failure message suggests lengthening the fixture.
    """

    def __init__(self) -> None:
        self.gaps: list[float] = []
        self._armed = False
        self._previous = 0.0
        self._stop = asyncio.Event()

    async def run(self) -> None:
        self._previous = time.perf_counter()
        while not self._stop.is_set():
            await asyncio.sleep(0)
            now = time.perf_counter()
            if self._armed:
                self.gaps.append(now - self._previous)
            self._previous = now

    def arm(self) -> None:
        """Begin recording. The first gap is measured from this call."""
        self._previous = time.perf_counter()
        self._armed = True

    def disarm(self) -> None:
        """Stop recording, keeping what was collected."""
        self._armed = False

    def stop(self) -> None:
        self._stop.set()

    def worst(self) -> tuple[float, float]:
        """The 99th percentile and the maximum wait recorded while armed.

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
        window = sorted(self.gaps)
        assert window, "the ticker recorded nothing — was it ever armed?"
        index = min(len(window) - 1, int(0.99 * len(window)))
        return window[index], window[-1]


class SeparatingWindow:
    """A stage callback that arms a ticker for the ``separating`` stage only."""

    def __init__(self, ticker: LoopTicker) -> None:
        self._ticker = ticker
        self.seen = False

    def __call__(self, stage: JobState) -> None:
        if stage is JobState.SEPARATING:
            self.seen = True
            self._ticker.arm()
        elif self.seen:
            self._ticker.disarm()


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
    window = SeparatingWindow(ticker)
    ticking = asyncio.create_task(ticker.run())

    await separate(tmp_path, separator, stage_callback=window)
    ticker.stop()
    await ticking

    assert window.seen, "the separating stage was never announced"
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

    worst, peak = ticker.worst()
    assert worst < STALL_FRACTION * chunk_seconds, (
        f"the event loop was unavailable for {worst * 1000:.1f} ms (p99; peak "
        f"{peak * 1000:.1f} ms) during the chunk loop, against a mean chunk of "
        f"{chunk_seconds * 1000:.1f} ms — the per-chunk filtering is blocking "
        "the loop"
    )


# -- the mono fold ----------------------------------------------------------


FOLD_SECONDS = 4.0
"""Fixture length for the fold. Enough audio for many blocks at the shipped size.

Chosen so the test's own premise is visible: at ``FOLD_BLOCK_FRAMES`` this is
tens of blocks, and at the ``1 << 19`` the constant used to be it is a single
one — which is exactly the state the assertion below rejects.
"""

FOLD_HOP_FRACTION = 0.25
"""Share of the whole fold that one thread hop may account for.

Relative, for the reason the chunk test's tolerance is relative: the absolute
number is a property of the machine, but "one hop is a quarter of the work" is a
property of the code. Before the constant was resized a four-second fixture
folded in **one** hop — a share of 1.0 — and a three-minute track in sixteen, at
120.9 ms each. After, a hop is ~0.5% of the fold.
"""


def wide_stereo(seconds: float, sample_rate: int = 44100) -> PcmAudio:
    """Two decorrelated channels of full-scale audio — the fold's real input.

    Deterministic and cheap to build: the fold's cost is per frame and does not
    care what the samples are, but the values still cross zero, hit both rails
    and land on odd sums, so a rounding change would show up as a different
    result rather than as a lucky match.
    """
    frames = int(seconds * sample_rate)
    left = array("h", ((index * 37) % 65536 - 32768 for index in range(frames)))
    right = array("h", ((index * 53) % 65536 - 32768 for index in range(frames)))
    return PcmAudio(sample_rate=sample_rate, channels=(left, right))


async def test_no_single_fold_hop_occupies_the_loop_for_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``apply_stereo_handling_async`` awaits one ``asyncio.to_thread`` per block
    # that ``fold_blocks`` yields, so the longest block *is* the longest wait it
    # imposes on the event loop. Timing them from inside the generator measures
    # the hop as the loop experiences it, and counting them ties the timing to
    # the async path rather than to the iterator in isolation.
    hops: list[float] = []
    sizes: list[int] = []
    original = stereo_module.fold_blocks

    def recording(source: PcmAudio, **kwargs: Any) -> Any:
        blocks = original(source, **kwargs)
        while True:
            started = time.perf_counter()
            block = next(blocks, None)
            if block is None:
                return
            hops.append(time.perf_counter() - started)
            sizes.append(len(block))
            yield block

    monkeypatch.setattr(stereo_module, "fold_blocks", recording)

    source = wide_stereo(FOLD_SECONDS)
    folded = await apply_stereo_handling_async(source, StereoHandling.MONO, CancellationToken())

    assert folded.channel_count == 1
    assert folded.frame_count == source.frame_count
    assert sum(sizes) == source.frame_count, "every frame must be folded exactly once"
    assert max(sizes) <= FOLD_BLOCK_FRAMES

    total = sum(hops)
    assert total > 0.005, (
        f"the whole fold took {total * 1000:.1f} ms — too little work for this "
        "test to be able to see a stall; lengthen the fixture"
    )
    assert max(hops) < FOLD_HOP_FRACTION * total, (
        f"one fold hop held the event loop for {max(hops) * 1000:.1f} ms of a "
        f"{total * 1000:.1f} ms fold ({len(hops)} hops) — a job that asked for "
        "the mono fold stalls the backend for that long, as many times"
    )
