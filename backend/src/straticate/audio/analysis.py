"""Wide-stereo detection: the full-band L/R correlation of an upload.

Feature 063, implementing the handoff feature 041 wrote down. 041 measured a
1968 stereo mix whose ``bass`` stem came out effectively silent because its
channels are near-independent -- **full-band Pearson L/R correlation +0.229**,
against 0.7 to 0.95 for the modern productions feature 028 sampled -- and
recorded, in its *Out of scope* table, exactly what a detection feature should
compute and what it may say. This module is that computation and nothing else:

============================  =============================================
041 handed over               what is implemented here
============================  =============================================
signal                        Pearson correlation of the two decoded
                              channels, full band, whole track, one pass
the failing case              +0.23 on that track
a defensible threshold        below ~+0.5 -- :data:`WIDE_STEREO_THRESHOLD`
corroborating signal          **not** computed; see *Out of scope* below
what it must say              a UI concern; see
                              ``frontend/src/components/WideStereoNote.tsx``
what it must not do           apply anything -- nothing here is reachable
                              from the separation path at all
============================  =============================================

**This module measures; it never decides for the user.** Nothing in
:mod:`straticate.inference` imports it, no job configuration is derived from it,
and a job separates identically whether or not anyone ever asked for the
analysis. That is feature 041's rule ("detection must suggest and never apply")
enforced by there being no code path from here to a ``stereo_handling`` value.

Why the numbers are exact
-------------------------

Pearson's coefficient over a whole track is five sums --
``n``, ``sum x``, ``sum y``, ``sum x^2``, ``sum y^2``, ``sum x*y`` -- and every
one of them is an integer when the samples are. They are accumulated as Python
``int``, which is arbitrary precision, so the accumulation cannot overflow and
cannot round: the *only* floating-point operation in the whole measurement is
the final division (and the square root under it). Two consequences worth
having:

- the answer does not depend on the order the audio was summed in, so a run that
  reads the track in 8 192-frame blocks gives **bit-identical** output to one
  that reads it in a single block. ``tests/test_stereo_analysis.py`` asserts
  that equality, which is what keeps the streaming shape below from being a
  second implementation;
- identical channels give exactly ``+1.0`` and inverted channels exactly
  ``-1.0``, decided by an *integer* comparison rather than by whether a float
  square root happened to land on 1.0 -- see :meth:`CorrelationSums.correlation`.

Why it streams, and why not :func:`~straticate.inference.pcm.decode_to_pcm`
--------------------------------------------------------------------------

The obvious implementation decodes the upload with the function that already
exists and correlates the two planes. It was not used, for two reasons.

**Memory.** ``decode_to_pcm`` materialises the whole track: an hour of 44.1 kHz
stereo is 635 MB of raw PCM before the planar split copies it again. Feature 038
spent a whole feature establishing that this application is bounded in the
length of a track, and feature 041 had to re-learn it inside
:mod:`straticate.inference.stereo`; a *third* place that forgets it would be a
pattern rather than an oversight. FFmpeg's stdout is read in blocks and only the
six accumulators survive a block, so peak memory here is flat at any length.

**The sample rate.** ``decode_to_pcm`` resamples to the model's native rate,
which is the right thing for a separator and the wrong thing for this: 041's
number is a *full-band* correlation of the file as released, and a resample is a
filter. The decode below asks for the rate ffprobe reported, so "full band"
means what 041 measured. Feeding 041's own track through a 44.1 kHz-forcing
decode and a native-rate one is the difference between reproducing the published
figure and being close to it.

Blocks, and the latency they owe the event loop
-----------------------------------------------

See :data:`ANALYSIS_BLOCK_FRAMES`. The pattern is feature 045's, applied to a
third loop: read-and-accumulate one block per :func:`asyncio.to_thread` hop,
with the block sized so the GIL-holding part of a hop stays inside the ~1 ms
band 045 established, rather than sized by the memory bound alone (which is what
made feature 041's first blocked fold stall the backend for 121 ms at a time).

Out of scope, deliberately
--------------------------

041 offered two corroborating signals -- the sub-250 Hz L/R correlation and the
low-band level imbalance -- and called neither necessary. Neither is computed.
They would each cost an FFT and a second threshold, and the honest position is
that the full-band number is the one 041 actually measured a distribution for.
There is also a recorded discrepancy between the two features about the
imbalance on the same track (028 reports 5.8 dB, 041's table 7.5 dB), which is a
further reason not to build a decision on it before it is understood.
"""

from __future__ import annotations

import asyncio
import math
import subprocess
import sys
import tempfile
from array import array
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from operator import mul
from pathlib import Path
from typing import IO, Any, Final

from straticate.audio.ffmpeg import FFmpegTimeout
from straticate.schemas.audio import AudioMetadata, StereoAnalysis


class AudioAnalysisError(Exception):
    """FFmpeg could not decode an upload for analysis.

    Distinct from :class:`straticate.inference.pcm.AudioDecodeError`, which
    means the same thing one layer down, because **the audio layer does not
    import the inference layer**: :mod:`straticate.inference.pcm` already
    imports :mod:`straticate.audio.ffmpeg` and :mod:`straticate.audio.probe`, so
    reusing its exception here would close a package-level cycle for the sake of
    one class name. The API maps this onto the same ``audio_not_decodable`` (422)
    the upload path uses, so a client sees one vocabulary either way.

    A decode that ran out of *time* is not this: it raises
    :class:`~straticate.audio.ffmpeg.FFmpegTimeout`, for the reason that module
    documents.
    """


_STEREO_FRAME_BYTES: Final = 4
"""Bytes in one frame of interleaved ``s16le`` stereo: two channels of 16 bits.

The same 16-bit PCM :data:`straticate.inference.pcm.SAMPLE_WIDTH_BYTES` names,
stated here rather than imported for the layering reason on
:class:`AudioAnalysisError`.
"""

WIDE_STEREO_THRESHOLD: Final = 0.5
"""Full-band L/R correlation at or above which a mix is *not* called wide.

**A measured constant of the application, not a dial**, and it is feature 041's
number rather than this feature's: the ``k`` sweep in
``docs/features/041-mono-folddown-option.md`` is the evidence, and it is worth
restating because a threshold with no provenance is a guess.

041 narrowed one real wide-stereo mix through seven values of ``k`` and
separated each with the same model, weights and device. The bass stem is
unrecoverable -- -65.7 to -65.2 dBFS, holding 0.004% of the source's sub-250 Hz
energy -- at every correlation from the track's own **+0.229** up to **+0.858**,
and the model behaves as if the material were in-distribution from about there
on (028 measured 0.7 to 0.95 for modern productions). So the two edges that
matter are +0.86 above and +0.23 below, and **+0.5 sits clear of both**: it
cannot fire on ordinary stereo, and it cannot miss the failure case.

The comparison is strictly below, so a mix measuring exactly +0.5 is not called
wide. Nothing turns on the tie -- it is stated so the boundary has one answer.

**What this number has not been measured for is its false-positive rate**, which
is the one thing 041 asked a detection feature to establish for itself
("everything above is from *one* record"). That measurement needs ordinary
modern tracks that cannot be committed to this repository, and until it exists
the suggestion built on this threshold is held disabled -- see
``WIDE_STEREO_SUGGESTION_ENABLED`` in
``frontend/src/components/WideStereoNote.tsx`` and the protocol in
``docs/features/063-wide-stereo-detection.md``. The endpoint ships because it
asserts a *measurement*; the suggestion does not, because it asserts a judgement.
"""

ANALYSIS_BLOCK_FRAMES: Final = 1 << 13
"""Frames read and accumulated per unit of work -- about 186 ms at 44.1 kHz.

Sized by the latency owed to the event loop, exactly as feature 045 sized
:data:`straticate.inference.stereo.FOLD_BLOCK_FRAMES`: each block is one
:func:`asyncio.to_thread` hop, and the accumulation inside it is pure Python
that holds the GIL for its whole duration. 045 established the target by probing
until a request served during a job landed in the same latency band as one
served while idle, which put a block at roughly 1 ms.

Measured here on 4 M stereo frames (90.7 s of 44.1 kHz audio), median per block
and total over three runs:

============  ==============  ==========
block frames  per block       whole pass
============  ==============  ==========
262144        39.29 ms        0.61 s
65536          9.29 ms        0.58 s
32768          4.91 ms        0.60 s
16384          2.36 ms        0.59 s
**8192**      **1.20 ms**     **0.62 s**
4096           0.58 ms        0.64 s
2048           0.29 ms        0.59 s
1024           0.15 ms        0.69 s
============  ==============  ==========

The total column is flat across a 256x range -- the hops are free at this scale,
which is what 045 found for the fold too -- so the size is set by the per-block
column alone, and 8192 is the largest block still inside the ~1 ms band. Memory
is bounded by it as well, but not tightly: even the largest row here is a
megabyte of transient objects, so the latency bound is the binding one.

That pass rate is **0.39 s of CPU per minute of audio**, so a 3-minute track is
about a second and an hour-long one about 23 s, plus FFmpeg's own decode. See
:func:`analyse_stereo` on why that is acceptable for this endpoint.
"""


@dataclass(slots=True)
class CorrelationSums:
    """The six exact integer accumulators a Pearson coefficient needs.

    Fed with :meth:`add`, which takes interleaved little-endian signed-16-bit
    stereo PCM -- FFmpeg's ``s16le`` output, unchanged. Every field is a Python
    ``int``, so nothing here overflows and nothing rounds; see the module
    docstring.

    Attributes:
        frames: Number of sample frames accumulated (``n``).
        sum_left: Sum of the left channel's samples.
        sum_right: Sum of the right channel's samples.
        sum_left_squared: Sum of the squares of the left channel's samples.
        sum_right_squared: Sum of the squares of the right channel's samples.
        sum_product: Sum of the per-frame products of the two channels.
    """

    frames: int = 0
    sum_left: int = 0
    sum_right: int = 0
    sum_left_squared: int = 0
    sum_right_squared: int = 0
    sum_product: int = 0

    def add(self, block: bytes) -> None:
        """Accumulate one block of interleaved stereo ``s16le`` samples.

        A trailing partial frame is ignored, which can only happen on the last
        block of a truncated stream (every read asks for a whole number of
        frames). ``PcmAudio``'s decode does the same thing for the same reason.
        """
        usable = len(block) - (len(block) % _STEREO_FRAME_BYTES)
        if usable == 0:
            return
        samples: array[int] = array("h")
        samples.frombytes(block[:usable])
        if sys.byteorder != "little":  # pragma: no cover - little-endian CI/dev hosts
            samples.byteswap()
        left = samples[0::2]
        right = samples[1::2]
        self.frames += len(left)
        self.sum_left += sum(left)
        self.sum_right += sum(right)
        self.sum_left_squared += sum(map(mul, left, left))
        self.sum_right_squared += sum(map(mul, right, right))
        self.sum_product += sum(map(mul, left, right))

    @property
    def correlation(self) -> float | None:
        """The Pearson coefficient of the two channels, or ``None``.

        ``None`` means there is no coefficient to report rather than a low one,
        and there are two ways to get there: fewer than two frames (nothing to
        correlate) and a channel with **zero variance** -- digital silence, or a
        constant offset -- for which Pearson's denominator is zero and the
        quantity is undefined. Callers distinguish the two; see
        :func:`analysis_from_sums`.

        Otherwise this is the textbook computational form,

        .. code-block:: text

            r = (n*Sxy - Sx*Sy) / sqrt((n*Sxx - Sx^2) * (n*Syy - Sy^2))

        with the numerator and both bracketed terms evaluated as **exact
        integers**, so the single division (and the square root under it) is the
        only place a float appears at all.

        ``+1.0`` and ``-1.0`` are decided by the integer identity
        ``numerator^2 == variance_left * variance_right`` rather than by what the
        division rounds to, which is what makes identical and inverted channels
        come back exactly rather than nearly. The result is clamped to
        ``[-1, 1]`` for the remaining cases, where the exact ratio is strictly
        inside the interval but its double-precision image need not be.
        """
        if self.frames < 2:
            return None
        count = self.frames
        variance_left = count * self.sum_left_squared - self.sum_left * self.sum_left
        variance_right = count * self.sum_right_squared - self.sum_right * self.sum_right
        if variance_left == 0 or variance_right == 0:
            return None
        numerator = count * self.sum_product - self.sum_left * self.sum_right
        product = variance_left * variance_right
        if numerator * numerator == product:
            return 1.0 if numerator > 0 else -1.0
        return max(-1.0, min(1.0, numerator / math.sqrt(product)))


def analysis_from_sums(sums: CorrelationSums) -> StereoAnalysis:
    """Turn accumulated sums into the record the API serves.

    **The single place ``wide_stereo`` is derived**, so a client never has to
    re-apply :data:`WIDE_STEREO_THRESHOLD` (and never has to be told what it is)
    to agree with the server about what the server measured.

    The two undefined-correlation cases are answered differently, and neither is
    an accident:

    - **a channel with zero variance** -- one side silent, or constant -- is
      reported ``wide_stereo=True``. A one-sided track is the *extreme* of the
      failure mode 041 documented, not an absence of it: whatever is only on one
      channel is exactly what a model trained on centred material will lose. The
      correlation stays ``None`` because there genuinely is not one.
    - **fewer than two frames** is reported ``wide_stereo=False``. There is
      nothing to correlate and no evidence of anything; saying "wide" would be a
      claim about audio that is not there.

    A mono source never reaches here at all (:func:`analyse_stereo` answers it
    without decoding), and gets the same ``{None, False}``.
    """
    correlation = sums.correlation
    if correlation is None:
        # Zero variance on a real, non-empty stream is the one-sided case.
        return StereoAnalysis(l_r_correlation=None, wide_stereo=sums.frames >= 2)
    return StereoAnalysis(
        l_r_correlation=correlation, wide_stereo=correlation < WIDE_STEREO_THRESHOLD
    )


async def analyse_stereo(
    path: Path,
    metadata: AudioMetadata,
    *,
    timeout_seconds: float,
    block_frames: int = ANALYSIS_BLOCK_FRAMES,
) -> StereoAnalysis:
    """Measure ``path``'s full-band L/R correlation in one streaming pass.

    A **single-channel source is answered without decoding anything**: there is
    no image to correlate, so the answer is ``{None, False}`` and no subprocess
    runs. Wider-than-stereo layouts are downmixed to two channels by FFmpeg,
    matching :data:`straticate.inference.pcm.MAX_OUTPUT_CHANNELS` -- the
    separator sees a stereo mixture too, so the measurement describes the audio
    the model would actually be given.

    **This holds the request open for the length of the pass**, which is the
    known cost of doing it honestly: roughly a second for a 3-minute track and
    something over half a minute for an hour-long one (see
    :data:`ANALYSIS_BLOCK_FRAMES`, plus FFmpeg's decode). It is acceptable here
    and would not be for anything else in this application, because this
    endpoint **gates nothing**: it is an enrichment the UI fires and forgets,
    every control works while it is outstanding, and a client that never asks
    for it loses no function. ARCHITECTURE.md's rule is that *inference* never
    runs in a request handler; a bounded measurement that blocks no user does
    not need the job machinery, and giving it a job would put a progress bar and
    a WebSocket channel around a number.

    Args:
        path: The stored upload.
        metadata: Its probed metadata. Two fields are read, and both matter:
            ``channels`` decides whether there is anything to measure, and
            ``sample_rate_hz`` is what the decode is asked for -- **no
            resample**, because 041's figure is a full-band one (see the module
            docstring).
        timeout_seconds: Bound on the whole decode, from the caller's
            ``Settings.ffmpeg_timeout_seconds``. Required, like every other
            FFmpeg call site in this application.
        block_frames: Frames per unit of work. Changes nothing but the working
            set and the event loop's latency; it is a parameter so the tests can
            assert a blocked pass equals a single-block one, and for no other
            reason.

    Returns:
        The analysis, with ``wide_stereo`` derived server-side.

    Raises:
        AudioAnalysisError: FFmpeg could not decode the file.
        FFmpegTimeout: the decode exceeded ``timeout_seconds``. Deliberately not
            folded into :class:`AudioAnalysisError` -- a tool that ran out of time
            made no claim about the media (see :mod:`straticate.audio.ffmpeg`).
    """
    if metadata.channels < 2:
        return StereoAnalysis(l_r_correlation=None, wide_stereo=False)
    sums = await _accumulate_stream(
        path,
        sample_rate=metadata.sample_rate_hz,
        timeout_seconds=timeout_seconds,
        block_frames=block_frames,
    )
    return analysis_from_sums(sums)


class StereoAnalysisCache:
    """Per-application, in-process memoisation of :func:`analyse_stereo`.

    An upload's correlation is a property of bytes that never change, so it is
    computed **once per audio ID** and held for the life of the process. There
    is no persistence and deliberately no sidecar: the whole record is two
    scalars, recomputing it costs one bounded pass, and a durable copy would be
    a second thing to keep in step with the file (feature 056 pays that price
    for something that a re-upload cannot reproduce; this is not that).

    **Concurrent readers share one computation.** The entry is the
    :class:`asyncio.Task`, not the result, so a second ``GET`` arriving while
    the first is still decoding awaits the same task rather than starting a
    second FFmpeg over the same file. Waiters :func:`asyncio.shield` it, so a
    client that disconnects mid-pass cancels only its own request -- without
    that, the first browser to give up would cancel the work every other waiter
    is blocked on.

    A task that fails or is cancelled **drops out of the cache**, so the next
    request retries rather than inheriting a permanent error. A successful one
    stays.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[StereoAnalysis]] = {}

    async def get(
        self,
        audio_id: str,
        loader: Callable[[], Coroutine[Any, Any, StereoAnalysis]],
    ) -> StereoAnalysis:
        """Return ``audio_id``'s analysis, computing it with ``loader`` at most once.

        ``loader`` is called only when nothing is cached and nothing is already
        in flight for this ID.
        """
        task = self._tasks.get(audio_id)
        if task is None:
            task = asyncio.create_task(loader())
            self._tasks[audio_id] = task
            task.add_done_callback(self._settle)
        return await asyncio.shield(task)

    def discard(self, audio_id: str) -> None:
        """Forget ``audio_id``'s analysis; called when its upload is deleted.

        A computation still in flight is **left to finish** rather than
        cancelled. Its result is thrown away either way, it is bounded by the
        FFmpeg timeout, and cancelling it would surface as a cancelled request
        to a client that asked before the delete arrived -- an error about
        someone else's action.
        """
        self._tasks.pop(audio_id, None)

    def _settle(self, task: asyncio.Task[StereoAnalysis]) -> None:
        """Drop a failed or cancelled task so the next request may retry.

        Also retrieves the exception, which is what stops asyncio logging an
        "exception was never retrieved" warning for a task nobody awaited (every
        waiter having disconnected).
        """
        if not task.cancelled() and task.exception() is None:
            return
        for audio_id, held in list(self._tasks.items()):
            if held is task:
                del self._tasks[audio_id]


async def _accumulate_stream(
    path: Path, *, sample_rate: int, timeout_seconds: float, block_frames: int
) -> CorrelationSums:
    """Drive one bounded decode, one block per thread hop, into fresh sums."""
    sums = CorrelationSums()
    decoder = _Decoder(path, sample_rate=sample_rate)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    await asyncio.to_thread(decoder.start)
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise FFmpegTimeout("ffmpeg", timeout_seconds)
            try:
                more = await asyncio.wait_for(
                    asyncio.to_thread(_pump, decoder, sums, block_frames), remaining
                )
            except TimeoutError as exc:
                raise FFmpegTimeout("ffmpeg", timeout_seconds) from exc
            if not more:
                break
        # Inside the ``try``, and before ``close`` — a clean end of stream does
        # not mean FFmpeg has *exited* yet, so the exit status has to be waited
        # for rather than sampled. Sampling it (``poll()``) and killing on
        # ``None`` would turn every successful pass into a decode failure.
        await asyncio.to_thread(decoder.check)
    finally:
        # Killing the process is what releases a worker thread still blocked on
        # a read: ``wait_for`` abandons the ``to_thread`` wrapper but cannot
        # interrupt the thread, and a dead process closes the pipe.
        decoder.close()
    return sums


def _pump(decoder: _Decoder, sums: CorrelationSums, block_frames: int) -> bool:
    """Read one block and fold it into ``sums``; ``False`` at end of stream.

    Read and accumulate share one thread hop deliberately. The read releases the
    GIL while it waits on the pipe and the accumulation holds it for about a
    millisecond (:data:`ANALYSIS_BLOCK_FRAMES`), so splitting them would double
    the hops to bound something that is already bounded.
    """
    block = decoder.read(block_frames)
    if not block:
        return False
    sums.add(block)
    return True


class _Decoder:
    """One FFmpeg decode to raw ``s16le`` stereo on stdout, read in blocks.

    Follows :mod:`straticate.audio.ffmpeg`'s conventions -- the same argument
    vector shape as :func:`straticate.inference.pcm._decode_sync`, stderr kept
    as bytes, a non-zero exit interpreted by the caller -- but cannot use
    :func:`~straticate.audio.ffmpeg.run_ffmpeg` itself, because that captures
    the whole of stdout in memory and this pass exists to not do that.

    stderr goes to a **temporary file** rather than a pipe. With a pipe and no
    reader, an FFmpeg that wrote more than the pipe buffer would deadlock
    against a stdout read that is waiting for output it will never produce; a
    file cannot fill, and the message is still there to put in the exception.
    """

    def __init__(self, path: Path, *, sample_rate: int) -> None:
        self._path = path
        self._sample_rate = sample_rate
        self._process: subprocess.Popen[bytes] | None = None
        self._errors: IO[bytes] | None = None

    def start(self) -> None:
        """Launch FFmpeg. Blocking; call from a worker thread."""
        # SIM115 asks for a context manager, and this file deliberately outlives
        # the frame that opens it: it is FFmpeg's stderr for the whole decode and
        # is closed by :meth:`close`, which is the ``finally`` of the driver
        # loop. A ``with`` here would close it before the first block is read.
        self._errors = tempfile.TemporaryFile()  # noqa: SIM115
        self._process = subprocess.Popen(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(self._path),
                "-map",
                "0:a:0",
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "-ar",
                str(self._sample_rate),
                "-ac",
                "2",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=self._errors,
            stdin=subprocess.DEVNULL,
        )

    def read(self, block_frames: int) -> bytes:
        """Read up to ``block_frames`` whole frames; ``b""`` at end of stream."""
        process = self._process
        if process is None or process.stdout is None:  # pragma: no cover - start() precedes
            return b""
        return process.stdout.read(block_frames * _STEREO_FRAME_BYTES)

    def close(self) -> None:
        """Kill the process if it is still running and release everything it held.

        Idempotent, and safe to call whether the pass ended cleanly, failed or
        timed out — which is why it is the ``finally`` of the driver loop.
        """
        process = self._process
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()
        if self._errors is not None:
            self._errors.close()
            self._errors = None

    def check(self) -> None:
        """Wait for FFmpeg to exit and raise if it failed, quoting what it said.

        Blocking; called from a worker thread after a clean end of stream.

        Raises:
            AudioAnalysisError: FFmpeg reported a failure.
        """
        process = self._process
        if process is None:  # pragma: no cover - start() precedes
            return
        returncode = process.wait()
        if returncode == 0:
            return
        message = ""
        if self._errors is not None:
            self._errors.seek(0)
            message = self._errors.read().decode("utf-8", "replace").strip()
        raise AudioAnalysisError(f"FFmpeg could not decode the file: {message}")


__all__ = [
    "ANALYSIS_BLOCK_FRAMES",
    "WIDE_STEREO_THRESHOLD",
    "AudioAnalysisError",
    "CorrelationSums",
    "StereoAnalysisCache",
    "analyse_stereo",
    "analysis_from_sums",
]
