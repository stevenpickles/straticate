"""Stereo-image preprocessing applied to a mixture before it is separated.

Feature 041. Feature 028 measured a real 1968 stereo mix whose ``bass`` stem
came out effectively silent (-66.2 dBFS, peak 176/32767) while the other three
were healthy, and chased it to the mix rather than to the code: near-independent
channels (full-band L/R correlation **+0.23**, against 0.7 to 0.95 for modern
productions) and a low end hard-panned 5.8 dB left. Every separation model in
use is trained on material where the bass is essentially always centred, so such
a mix is outside the training distribution -- and folding it to mono recovers the
stem entirely.

This module is that fold, and nothing else. It is one pure function over
:class:`~straticate.inference.pcm.PcmAudio`, applied immediately after the decode
and *only* when the job asked for it. It is never applied on the model's behalf
and never inferred from the audio: see
:class:`~straticate.schemas.jobs.StereoHandling`.

Why the fold, and not a partial narrowing
-----------------------------------------

The obvious alternative is mid/side with the side component **attenuated**
rather than removed -- ``L' = M + kS``, ``R' = M - kS`` -- which would keep some
stereo image. Feature 041 measured it across ``k`` in ``{1, 0.75, 0.5, 0.35,
0.25, 0.1, 0}`` on the same track, same model, same settings, and it does not
work: the bass stem is unchanged (-65.7 to -65.4 dBFS) all the way down to
``k = 0.35``, by which point the input's L/R correlation is already **+0.86** --
inside the band the model is trained on -- and the first meaningful recovery
arrives at ``k = 0.10``, where the correlation is +0.988 and the side component
is 22 dB below the mid. There is no ``k`` that both recovers the stem and leaves
an audible stereo image, so there is no partial setting worth exposing. The
numbers are in ``docs/features/041-mono-folddown-option.md``.

Why this is plain integer arithmetic, and not torch
---------------------------------------------------

**Because every separator has to be able to call it, including the ones that
cannot import torch.** The first version of this module crossed the
:mod:`straticate.inference.torch_audio` bridge, which made the fold reachable
only from :class:`~straticate.inference.torch_separator.TorchSeparator`. That
left :class:`~straticate.inference.fake.FakeSeparator` accepting
``stereo_handling: "mono"``, echoing it back on the job, and returning
two-channel stems -- the application asserting something about its own behaviour
that was not so, which is the very thing feature 032 exists to stop. Torch is an
*optional* extra (feature 034) and a fake-only deployment has none, so the fold
had to come back out from behind it. It is now the same function on every
backend, and the contract in ``docs/contracts/rest-api.md`` is true everywhere
rather than true-with-an-asterisk.

The cost of leaving the bridge is real and was measured, not estimated: **2.1 s**
for a 2:43 track (7.19 M frames), or 0.77 s per minute of audio, against
milliseconds in torch. That is paid only when a user explicitly asks for the
fold -- the default returns without touching a sample -- it runs in a worker
thread, and it sits beside a separation that takes tens of seconds on a GPU and
minutes on a CPU. A correct contract on every backend is worth more than two
seconds on an opt-in path.

What that path *does* owe is the long-track discipline feature 038 established,
and the first draft of this module did not pay it: it built the whole result as
one Python list (44.3 bytes per frame -- about 10.5 GB on a 90-minute track),
copied every channel in full, and ran as one uninterruptible thread hop. All
three are the same mistake -- treating the track as a single unit -- and
:func:`fold_blocks` is the single fix. See its docstring and
:func:`apply_stereo_handling_async`.

Rounding, and why it is stated rather than left to a float
----------------------------------------------------------

The mean of two 16-bit samples lands exactly half way between two integers
whenever their sum is odd, which for real audio is about half of all frames. The
tie is broken **to even**, matching :func:`torch.round` and therefore
:func:`straticate.inference.torch_audio.tensor_to_pcm`, which quantizes every
stem this application writes -- so the mixture and the stems are quantized by one
rule. Doing it in integers additionally makes the result *exact* and
platform-independent: the float32 version this replaced disagreed with the exact
mean by one LSB on about 9% of frames, always on those ties, resolved by whichever
way float error happened to fall.
"""

from __future__ import annotations

import asyncio
from array import array
from collections.abc import Iterator
from operator import add
from typing import Final

from straticate.inference.pcm import INT16_MAX, PcmAudio
from straticate.jobs.cancellation import CancellationToken
from straticate.schemas.jobs import StereoHandling

FOLD_BLOCK_FRAMES: Final = 1 << 19
"""Frames folded per unit of work -- about 12 s of 44.1 kHz audio.

Two things are sized by this and they pull the same way, which is why one
constant serves both. It bounds the **transient** cost of the fold: only one
block's worth of intermediate Python objects is ever alive, so peak memory is a
little over a megabyte however long the track is. And it bounds how long
cancellation can go unobserved, because the token is checked between blocks --
roughly 0.14 s at the measured rate. Smaller would buy nothing a user could
perceive and would pay another thread hop for it.
"""


def apply_stereo_handling(source: PcmAudio, handling: StereoHandling) -> PcmAudio:
    """Return ``source`` with ``handling`` applied to its stereo image.

    Synchronous and whole-track. A separator wants
    :func:`apply_stereo_handling_async` instead, which does the same work in
    cancellable blocks; this is the plain definition the tests pin and the one
    to read when asking what the transform *is*.

    Args:
        source: The decoded mixture.
        handling: What the job asked for.

    Returns:
        For :attr:`~straticate.schemas.jobs.StereoHandling.AS_IS`, **the very
        object passed in** -- the default path must be bit-for-bit what it was
        before this feature existed, and identity is the only way to promise
        that. For :attr:`~straticate.schemas.jobs.StereoHandling.MONO`, a
        one-channel :class:`~straticate.inference.pcm.PcmAudio` holding the mean
        of every channel at the same sample rate; a source that is *already* one
        channel is likewise returned unchanged, because folding it would be a
        copy with nothing to fold.
    """
    if _is_identity(source, handling):
        return source
    plane: array[int] = array("h")
    for block in fold_blocks(source):
        plane.extend(block)
    return PcmAudio(sample_rate=source.sample_rate, channels=(plane,))


async def apply_stereo_handling_async(
    source: PcmAudio,
    handling: StereoHandling,
    cancellation_token: CancellationToken,
) -> PcmAudio:
    """:func:`apply_stereo_handling`, off the event loop and cancellable.

    **This is what a separator calls.** The fold is pure Python over every
    sample of the track (see this module's docstring), which the measurement
    puts at roughly 0.69 s per minute of audio -- a minute or more on the long
    material feature 038 made survivable. Handing that to a single
    :func:`asyncio.to_thread` would make it exactly the kind of step this
    project does not ship: one where the user presses Cancel, the job goes on
    saying ``decoding``, and nothing happens for a minute or two. Every other
    long-running step in both separators -- ``_run_chunks``, ``_encode`` --
    checks the token per unit of work, and so does this one.

    The identity paths never reach a thread at all: no hop, no copy, nothing.

    Raises:
        JobCancelled: Cancellation was observed at a block boundary.
    """
    if _is_identity(source, handling):
        return source
    plane: array[int] = array("h")
    blocks = fold_blocks(source)
    while True:
        cancellation_token.raise_if_cancelled()
        block = await asyncio.to_thread(next, blocks, None)
        if block is None:
            return PcmAudio(sample_rate=source.sample_rate, channels=(plane,))
        plane.extend(block)


def _is_identity(source: PcmAudio, handling: StereoHandling) -> bool:
    """Whether ``handling`` leaves ``source`` untouched, so it can be returned as-is."""
    return handling is StereoHandling.AS_IS or source.channel_count < 2


def fold_blocks(source: PcmAudio, *, block_frames: int = FOLD_BLOCK_FRAMES) -> Iterator[array[int]]:
    """Yield ``source``'s channels averaged into one plane, ``block_frames`` at a time.

    Ties round to even and the result is clamped to the symmetric 16-bit range
    the rest of the pipeline uses, so ``-32768`` folded with itself yields
    ``-32767`` exactly as
    :func:`straticate.inference.torch_audio.tensor_to_pcm` has always produced
    for that sample. See this module's docstring.

    **Blocking is not an optimisation here, it is the memory bound.** Feeding
    ``array("h", ...)`` a list comprehension builds one Python ``int`` object
    per frame before the array exists: measured at **44.3 bytes per frame**
    against **2.12** for the same expression as a generator, which at 90 minutes
    of audio -- the length feature 038 measured out to -- is the difference
    between ~10.5 GB of Python heap and a rounding error. A generator alone
    fixes that; blocking additionally keeps every intermediate slice small, so
    nothing here is proportional to the length of the track except the result.
    """
    planes = source.channels
    count = len(planes)
    frames = source.frame_count
    for start in range(0, frames, block_frames):
        stop = min(start + block_frames, frames)
        # Slices are per block and therefore small. Slicing the *whole* plane
        # here -- even the no-op ``plane[:frames]`` when the planes are already
        # equal length, which is what ``PcmAudio`` documents -- copied every
        # channel in full for nothing: ~950 MB on a 90-minute stereo track.
        if count == 2:
            totals = map(add, planes[0][start:stop], planes[1][start:stop])
        else:
            totals = map(sum, zip(*(plane[start:stop] for plane in planes), strict=True))
        yield array(
            "h",
            (
                -INT16_MAX if value < -INT16_MAX else INT16_MAX if value > INT16_MAX else value
                for base, rest in (divmod(total, count) for total in totals)
                for value in (
                    base + 1 if (rest * 2 > count or (rest * 2 == count and base & 1)) else base,
                )
            ),
        )


__all__ = ["FOLD_BLOCK_FRAMES", "apply_stereo_handling", "apply_stereo_handling_async"]
