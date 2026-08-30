"""Stereo-image preprocessing applied to a mixture before it is separated.

Feature 041, extended by feature 062. Feature 028 measured a real 1968 stereo
mix whose ``bass`` stem came out effectively silent (-66.2 dBFS, peak
176/32767) while the other three were healthy, and chased it to the mix rather
than to the code: near-independent channels (full-band L/R correlation
**+0.23**, against 0.7 to 0.95 for modern productions) and a low end hard-panned
5.8 dB left. Every separation model in use is trained on material where the bass
is essentially always centred, so such a mix is outside the training
distribution -- and folding it to mono recovers the stem entirely.

This module is the two transforms that answer that, and nothing else: the
whole-spectrum fold (:attr:`~straticate.schemas.jobs.StereoHandling.MONO`) and
the band-limited one
(:attr:`~straticate.schemas.jobs.StereoHandling.MONO_BASS`). Both are pure
functions over :class:`~straticate.inference.pcm.PcmAudio`, applied immediately
after the decode and *only* when the job asked for one. Neither is ever applied
on the model's behalf and neither is ever inferred from the audio: see
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

Narrowing *uniformly* fails. Narrowing **only where the problem is** does not
------------------------------------------------------------------------------

That sweep attenuated the side component at every frequency at once, which is
why its useful range collapsed to ``[0, 0.1]``: the stereo image lives almost
entirely above a few hundred hertz, and the model's complaint is entirely below
it, so one knob was being asked to serve two bands with opposite requirements.
Feature 062 gave each band its own answer --

.. code-block:: text

    M = mean(channels)          L' = HP(L) + LP(M)          R' = HP(R) + LP(M)

-- and measured it against 041's published baselines on 041's track, with 041's
two endpoints reproduced first as controls. It wins on every axis 041 recorded:
a ``bass`` stem 0.6 dB **louder** than the full fold's, 19.4% of the source's
sub-250 Hz energy in it against the fold's 16.0%, a third of the fold's cost to
``drums`` and ``other`` (~1 dB each against ~3), the image above the crossover
preserved to within 0.2 dB of as-released -- and stems that are still stereo.
``docs/features/062-band-limited-fold.md`` has the whole table, including the
finding that the *filter* is not neutral here: a brick-wall crossover and a
Linkwitz-Riley one disagree by 10 dB of recovered ``bass`` at 125 Hz, which is
why the shipped crossover is stated in terms of the shipped filter.

Why this is plain Python, and not torch
---------------------------------------

**Because every separator has to be able to call it, including the ones that
cannot import torch.** This applies to both transforms and it is the reason the
band-limited one is a hand-rolled biquad cascade rather than a call into a DSP
library: there is no filtering dependency here to reach for, and adding one for
an opt-in preprocessing step would put the same asterisk back on the contract.
The first version of this module crossed the
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

The band-limited fold obeys the same tie rule -- :func:`round` on a
:class:`float` is round-half-to-even -- but it cannot be *exact* in the way the
plain fold is, because a recursive filter has no integer answer to be exact
about. It is float64 throughout, which is what makes it deterministic instead:
IEEE-754 double arithmetic in a fixed order gives the same bits on every host,
so the same input yields the same int16 output everywhere, and the filter state
carried across block boundaries makes a blocked run **bit-identical** to a
one-shot one rather than merely close. Both properties are asserted in
``tests/test_stereo_handling.py``; the second is the mutation-catcher for a
filter that forgot its state.
"""

from __future__ import annotations

import asyncio
import math
from array import array
from collections.abc import Iterator, Sequence
from operator import add
from typing import Final

from straticate.inference.pcm import INT16_MAX, PcmAudio
from straticate.jobs.cancellation import CancellationToken
from straticate.schemas.jobs import StereoHandling

FOLD_BLOCK_FRAMES: Final = 1 << 12
"""Frames folded per unit of work -- about 93 ms of 44.1 kHz audio.

**Three** things are sized by this, and the third is what feature 045 corrected.
It bounds the transient cost of the fold: only one block's worth of intermediate
Python objects is ever alive, so peak memory is flat however long the track is.
It bounds how long cancellation can go unobserved, because the token is checked
between blocks. And it bounds **how long the event loop waits for its turn**,
because each block is one :func:`asyncio.to_thread` hop of pure Python that
holds the GIL for its whole duration (see
:ref:`the fake separator's Threading section <fake-threading>`, which has the
measurements and the reason a thread hop alone does not help).

This was ``1 << 19`` -- 524 288 frames, 12 s of audio -- sized by the first two
bounds alone, which were feature 041's concerns. Feature 044 then measured what
a blocked loop costs, and by 045's own definition the old value was the same
defect this pair of features exists to remove: **120.9 ms of GIL-holding work
per block** (median, 181 s track, 16 blocks), so a mono job stalled the backend
sixteen times for a tenth of a second each -- and by the 5-17x contention 044
measured on a busy developer machine, for whole seconds each.

Measured, folding a 181 s track, median per block over three runs:

============  ==============  ==========
block frames  per block       whole fold
============  ==============  ==========
524288 (old)  120.85 ms       1.85 s
131072        26.89 ms        1.69 s
32768         6.64 ms         1.68 s
8192          1.64 ms         1.70 s
**4096**      **0.80 ms**     **1.69 s**
2048          0.40 ms         1.70 s
============  ==============  ==========

The whole-fold column is the answer to 041's reasoning that a smaller block
"would pay another thread hop for it": across a 256x range of block sizes the
fold costs the same 1.69 s, and the *largest* block is the slowest of the set.
The hops are free at this scale, so the size is set by the latency owed to the
loop and nothing else. 4096 is ~1 ms, matching
:data:`straticate.inference.fake.FILTER_BLOCK_FRAMES`, which was chosen by
probing until a request served during a job landed in the same band as one
served while idle.

The band-limited fold has its own value for the same reason -- see
:data:`BASS_FOLD_BLOCK_FRAMES`.
"""

BASS_FOLD_BLOCK_FRAMES: Final = 1 << 10
"""Frames per unit of work for the band-limited fold -- about 23 ms at 44.1 kHz.

:data:`FOLD_BLOCK_FRAMES` divided by four, because the band-limited fold costs
about four times as much per frame and this constant is a **latency** bound, not
a frame count. Feature 045 sized 4096 by probing until a request served during a
job landed in the same latency band as one served while idle, which put the plain
fold's GIL hold at ~0.80 ms per block; the same 4096 frames of biquad cascade
holds it for **3.74 ms**. Keeping the number and letting the hold grow with the
work would be reading 045's constant instead of its argument.

The whole-fold cost is flat across block size, exactly as 045 measured for the
plain fold, so this is free. Median per block and total, folding the 163 s track:

============  ==============  ==========
block frames  per block       whole fold
============  ==============  ==========
32768         30.11 ms        6.91 s
8192          7.40 ms         6.98 s
4096          3.74 ms         7.18 s
2048          1.86 ms         7.12 s
**1024**      **0.97 ms**     **7.17 s**
============  ==============  ==========

Memory and cancellation are bounded by whichever value is smaller, so nothing is
given up either: peak overhead falls from 2.14 MB to **0.37 MB** on that track,
and is flat in its length at both.
"""

BASS_FOLD_CROSSOVER_HZ: Final = 500
"""Crossover of :attr:`~straticate.schemas.jobs.StereoHandling.MONO_BASS`, in hertz.

**A measured constant of the application, not a dial.** Feature 062 swept it and
this is the value the sweep chose; the whole table is in
``docs/features/062-band-limited-fold.md``. In summary, on 041's track with
041's model and 041's two endpoints reproduced as controls, and with the shipped
Linkwitz-Riley filter:

============  =================  ======================  ===================
crossover     ``bass`` stem rms  source's <250 Hz in it  side/mid, full band
============  =================  ======================  ===================
(none)        -65.67 dBFS        0.002%                  -2.03 dB
125 Hz        -41.99 dBFS        1.6%                    -2.85 dB
250 Hz        -36.21 dBFS        7.4%                    -3.50 dB
350 Hz        -35.89 dBFS        8.0%                    -3.69 dB
**500 Hz**    **-31.96 dBFS**    **19.4%**               **-3.89 dB**
750 Hz        -30.24 dBFS        28.4%                   -4.21 dB
1000 Hz       -29.65 dBFS        32.3%                   -4.62 dB
(full fold)   -32.55 dBFS        16.0%                   -inf
============  =================  ======================  ===================

**500 Hz is the cheapest value that wins outright.** It is the first crossover
that beats the full fold on *both* of the numbers the fold was adopted for -- a
louder ``bass`` stem (-31.96 against -32.55) and more of the source's low band in
it (19.4% against 16.0%) -- so it delivers everything ``mono`` delivers while
still leaving an image. 350 Hz does not (8.0% recovery at -35.89 dBFS, which is
2.6 dB *short* of the fold).

Higher crossovers keep buying recovery, and the last column is what they buy it
with. That trade already has a control: its far end is ``mono``, and a user who
does not want the image can pick it. The value of *this* control is the image it
keeps, so the crossover is the lowest one that does not ask them to give
anything up in exchange -- not the one that maximises a single column.

**Why it is not user-tunable**, and why it is not catalog data either, is
feature 041's ARCHITECTURE.md §1 argument applied one level down. The *choice*
this control offers is a statement about the user's recording ("the low end of
this mix is not centred"), and that is what the enum value says. The crossover
is not: it is a property of where a stereo image lives in human hearing and of
where these models' training distribution stops, and a user has no way to
answer it. Exposing it would be a slider whose useful range this sweep already
searched -- 041's own reason for refusing the mid/side ``k``. It is not
``default_inference_parameters`` either, because it is not a hyperparameter of
any network: it is applied to the mixture before a model is chosen, exactly like
the fold, and would mean the same thing to a different backend.

**One track.** This is the same honest limitation 041 carries: a real,
reproduced measurement on the failure case, not a survey.
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
        of every channel at the same sample rate. For
        :attr:`~straticate.schemas.jobs.StereoHandling.MONO_BASS`, audio with
        **the same channel count** as ``source``, each channel high-passed at
        :data:`BASS_FOLD_CROSSOVER_HZ` and summed with the low-passed mean of
        all of them.

        A source that is *already* one channel is returned unchanged by every
        value: there is no image to fold, so both transforms would be a copy
        with nothing to do, and identity says so at no cost.
    """
    if _is_identity(source, handling):
        return source
    planes = _result_planes(source, handling)
    for block in _transform_blocks(source, handling):
        _extend(planes, block)
    return PcmAudio(sample_rate=source.sample_rate, channels=planes)


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

    **And the loop waits for exactly one block, so a block has to be short.**
    That is feature 045's correction, and the reasoning above is why it was
    needed: a per-block hop was chosen here to bound *cancellation*, and a
    12-second block bounds cancellation perfectly well while holding the GIL for
    120.9 ms at a time. Nothing served a request for the length of each of those
    hops, sixteen times over a three-minute track -- the same defect 044 measured
    in the fake separator's chunk loop, on the ``mono`` path, in a function that
    already looked like the fix. :data:`FOLD_BLOCK_FRAMES` is now sized by the
    latency owed to the loop, which is the tightest of the three bounds it
    serves; measured, that took a mono job's during-job 95th percentile from
    64.0 ms to 17.8 ms against an idle 17.4 ms.

    The identity paths never reach a thread at all: no hop, no copy, nothing.

    All of that applies unchanged to the band-limited fold, which is the more
    expensive of the two -- **2.67 s per minute of audio against 0.73**, best of
    three runs on the same 163 s track -- and therefore has more of both to lose. It pays
    the latency bound with a smaller block rather than a longer hold (see
    :data:`BASS_FOLD_BLOCK_FRAMES`); what it adds is that the block boundary is
    now load-bearing for *correctness* as well, because the filter state crosses
    it. :func:`bass_fold_blocks` owns that, and this driver is identical for
    both.

    Raises:
        JobCancelled: Cancellation was observed at a block boundary.
    """
    if _is_identity(source, handling):
        return source
    planes = _result_planes(source, handling)
    blocks = _transform_blocks(source, handling)
    while True:
        cancellation_token.raise_if_cancelled()
        block = await asyncio.to_thread(next, blocks, None)
        if block is None:
            return PcmAudio(sample_rate=source.sample_rate, channels=planes)
        _extend(planes, block)


def _is_identity(source: PcmAudio, handling: StereoHandling) -> bool:
    """Whether ``handling`` leaves ``source`` untouched, so it can be returned as-is.

    The channel-count clause covers **every** non-default value rather than
    ``MONO`` alone, and deliberately: a one-channel source has no stereo image,
    so neither transform has anything to do to it. The band-limited fold would
    otherwise put a mono file through a crossover and hand back an allpassed
    copy -- audibly the same, not bit-identical, and pure cost.
    """
    return handling is StereoHandling.AS_IS or source.channel_count < 2


def _result_planes(source: PcmAudio, handling: StereoHandling) -> tuple[array[int], ...]:
    """The empty output planes ``handling`` will fill: one per output channel."""
    count = source.channel_count if handling is StereoHandling.MONO_BASS else 1
    return tuple(array("h") for _ in range(count))


def _transform_blocks(
    source: PcmAudio, handling: StereoHandling
) -> Iterator[tuple[array[int], ...]]:
    """One unit of work at a time, as a tuple of planes, whichever transform it is.

    The two generators differ in how many planes they produce, so this is where
    that difference stops: from here out the sync and async drivers are one
    piece of code accumulating tuples, which is what keeps
    :func:`apply_stereo_handling` and :func:`apply_stereo_handling_async`
    provably the same transform rather than two that agree today.
    """
    if handling is StereoHandling.MONO_BASS:
        yield from bass_fold_blocks(source)
    else:
        for block in fold_blocks(source):
            yield (block,)


def _extend(planes: tuple[array[int], ...], block: tuple[array[int], ...]) -> None:
    """Append one block's planes to the result's."""
    for plane, part in zip(planes, block, strict=True):
        plane.extend(part)


def fold_blocks(source: PcmAudio, *, block_frames: int = FOLD_BLOCK_FRAMES) -> Iterator[array[int]]:
    """Yield ``source``'s channels averaged into one plane, ``block_frames`` at a time.

    Ties round to even and the result is clamped to the symmetric 16-bit range
    the rest of the pipeline uses, so ``-32768`` folded with itself yields
    ``-32767`` exactly as
    :func:`straticate.inference.torch_audio.tensor_to_pcm` has always produced
    for that sample. See this module's docstring.

    **Blocking is not an optimisation here, it is the memory bound** -- and,
    since feature 045, the event loop's latency bound too; see
    :data:`FOLD_BLOCK_FRAMES`, which is what sets the size. Feeding
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


def bass_fold_blocks(
    source: PcmAudio,
    *,
    block_frames: int = BASS_FOLD_BLOCK_FRAMES,
    crossover_hz: float = BASS_FOLD_CROSSOVER_HZ,
) -> Iterator[tuple[array[int], ...]]:
    """Yield ``source`` with only its low end folded, ``block_frames`` at a time.

    Each block is ``HP(channel) + LP(mean of all channels)``, one output plane
    per input plane, where ``HP`` and ``LP`` are the two branches of a
    Linkwitz-Riley 4th-order crossover at ``crossover_hz``. Rounding, clamping
    and blocking follow :func:`fold_blocks` exactly; what is new is the filter
    state, and it is what the rest of this docstring is about.

    **Linkwitz-Riley, because its two branches sum back to unity.** An LR4
    branch is two identical Butterworth sections in cascade, and the defining
    property of the pair is that ``LP + HP`` is an *allpass*: flat in magnitude
    at every frequency, with only a phase rotation. That is exactly the
    guarantee this transform needs, because on material that is already centred
    (``L == R``, so ``mean == L``) it makes the whole thing ``LP(L) + HP(L)`` --
    the input back, unchanged in level, everywhere. A crossover without that
    property would notch or bump the region around ``crossover_hz`` on *every*
    centred mix, which is a defect on the majority of material to fix a minority
    of it. Measured on a centred signal, the round trip is 0.0003 dB. It is
    **not** bit-identity, and the difference from ``mono``'s exactness is
    deliberate and tested: a filter has phase, so the samples move even though
    the magnitude does not.

    **The state crosses the block boundary, and that is the whole correctness
    argument.** A biquad's output depends on the two frames before it; a run
    that reset the filter every block would emit a discontinuity at every
    boundary -- 4096 frames apart, which is a 10.8 Hz buzz on top of the music,
    audible and completely wrong, from code that looks right. The four float64
    values per branch persist across the whole generator, so a blocked run is
    **bit-identical** to a one-shot one. ``tests/test_stereo_handling.py``
    asserts that equality over several block sizes, and it is the test that
    fails if the state is ever moved inside the loop.

    Args:
        source: The decoded mixture; two or more channels (a single-channel
            source never reaches here -- see :func:`_is_identity`).
        block_frames: Frames per unit of work. Changes nothing but the working
            set and the latency; see :data:`BASS_FOLD_BLOCK_FRAMES`.
        crossover_hz: Fold everything below this. Defaults to the measured
            :data:`BASS_FOLD_CROSSOVER_HZ`; it is a parameter here so the tests
            can put the crossover where a synthesized tone is, and for no other
            reason. Nothing in the application passes it.
    """
    planes = source.channels
    count = len(planes)
    frames = source.frame_count
    low_pass = _lr4_coefficients(crossover_hz, source.sample_rate, highpass=False)
    high_pass = _lr4_coefficients(crossover_hz, source.sample_rate, highpass=True)
    low_state = [0.0, 0.0, 0.0, 0.0]
    high_states = [[0.0, 0.0, 0.0, 0.0] for _ in range(count)]
    for start in range(0, frames, block_frames):
        stop = min(start + block_frames, frames)
        columns = [plane[start:stop] for plane in planes]
        if count == 2:
            mid = [(one + other) * 0.5 for one, other in zip(columns[0], columns[1], strict=True)]
        else:
            mid = [sum(frame) / count for frame in zip(*columns, strict=True)]
        low = _lr4_branch(mid, low_pass, low_state)
        yield tuple(
            array(
                "h",
                (
                    -INT16_MAX if value < -INT16_MAX else INT16_MAX if value > INT16_MAX else value
                    for value in (
                        # ``round`` on a float is round-half-to-even, the same
                        # tie rule ``fold_blocks`` implements in integers and
                        # ``tensor_to_pcm`` applies to every stem.
                        round(high + band)
                        for high, band in zip(
                            _lr4_branch(column, high_pass, high_states[index]), low, strict=True
                        )
                    )
                ),
            )
            for index, column in enumerate(columns)
        )


def _lr4_coefficients(
    crossover_hz: float, sample_rate: int, *, highpass: bool
) -> tuple[float, float, float, float, float]:
    """One Butterworth section of a Linkwitz-Riley branch, as ``(b0, b1, b2, a1, a2)``.

    The standard bilinear-transform biquad at ``Q = 1/sqrt(2)``, normalised by
    ``a0`` so the recursion in :func:`_lr4_branch` needs no division. Cascading
    this section with itself is what makes the branch 4th-order Linkwitz-Riley
    rather than 2nd-order Butterworth, and it is what makes ``LP + HP`` sum to
    an allpass -- see :func:`bass_fold_blocks`.

    Args:
        crossover_hz: The -6 dB point of the pair.
        sample_rate: Sample rate of the audio the coefficients will filter.
            The coefficients are rate-dependent, which is why they are derived
            per call from :attr:`PcmAudio.sample_rate` rather than precomputed
            for 44.1 kHz and reused.
        highpass: Which branch.
    """
    angular = 2.0 * math.pi * crossover_hz / sample_rate
    cosine = math.cos(angular)
    alpha = math.sin(angular) * math.sqrt(0.5)
    scale = 1.0 + alpha
    if highpass:
        numerator = (1.0 + cosine) / 2.0
        b1 = -(1.0 + cosine)
    else:
        numerator = (1.0 - cosine) / 2.0
        b1 = 1.0 - cosine
    return (
        numerator / scale,
        b1 / scale,
        numerator / scale,
        (-2.0 * cosine) / scale,
        (1.0 - alpha) / scale,
    )


def _lr4_branch(
    samples: Sequence[float] | Sequence[int],
    coefficients: tuple[float, float, float, float, float],
    state: list[float],
) -> list[float]:
    """Run both sections of one Linkwitz-Riley branch over ``samples``.

    Transposed direct form II, float64, with the two sections fused into a
    single pass: their coefficients are identical, so running them in one loop
    saves a loop dispatch and the intermediate list that would otherwise be
    built between them. Measured on the same audio, bit-for-bit the same result:
    **2.99 s per minute as two passes against 2.69 as one**, an 11% saving. It is
    written this way because it is also *shorter*; if it ever needs to be two
    functions for clarity, 11% is what that costs. See the module docstring on
    why this is pure Python at all.

    ``state`` is the four delay elements, in place, so a caller that keeps one
    list per branch for the length of a track gets a continuous filter across
    however many blocks it chooses to run. It is mutated rather than returned
    because forgetting to reassign a returned state is precisely the bug
    :func:`bass_fold_blocks`'s block-invariance test exists to catch, and a
    mutating call has no way to express it.
    """
    b0, b1, b2, a1, a2 = coefficients
    first_z1, first_z2, second_z1, second_z2 = state
    filtered: list[float] = []
    append = filtered.append
    for sample in samples:
        once = b0 * sample + first_z1
        first_z1 = b1 * sample - a1 * once + first_z2
        first_z2 = b2 * sample - a2 * once
        twice = b0 * once + second_z1
        second_z1 = b1 * once - a1 * twice + second_z2
        second_z2 = b2 * once - a2 * twice
        append(twice)
    state[0], state[1], state[2], state[3] = first_z1, first_z2, second_z1, second_z2
    return filtered


__all__ = [
    "BASS_FOLD_BLOCK_FRAMES",
    "BASS_FOLD_CROSSOVER_HZ",
    "FOLD_BLOCK_FRAMES",
    "apply_stereo_handling",
    "apply_stereo_handling_async",
]
