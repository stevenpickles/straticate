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

The cost of leaving the bridge is real and was measured, not estimated: **3.9 s**
for a 2:43 track (7.19 M frames), against milliseconds in torch. That is paid
only when a user explicitly asks for the fold -- the default returns without
touching a sample -- it runs in a worker thread, and it sits beside a separation
that takes tens of seconds on a GPU and minutes on a CPU. A correct contract on
every backend is worth more than three seconds on an opt-in path.

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

from array import array
from operator import add

from straticate.inference.pcm import INT16_MAX, PcmAudio
from straticate.schemas.jobs import StereoHandling


def apply_stereo_handling(source: PcmAudio, handling: StereoHandling) -> PcmAudio:
    """Return ``source`` with ``handling`` applied to its stereo image.

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
    if handling is StereoHandling.AS_IS or source.channel_count < 2:
        return source
    return PcmAudio(sample_rate=source.sample_rate, channels=(_fold(source),))


def _fold(source: PcmAudio) -> array[int]:
    """Average ``source``'s channels into one plane of 16-bit samples.

    Ties round to even and the result is clamped to the symmetric 16-bit range
    the rest of the pipeline uses, so ``-32768`` folded with itself yields
    ``-32767`` exactly as
    :func:`straticate.inference.torch_audio.tensor_to_pcm` has always produced
    for that sample. See this module's docstring.
    """
    planes = source.channels
    count = len(planes)
    frames = source.frame_count
    totals = (
        map(add, planes[0][:frames], planes[1][:frames])
        if count == 2
        else map(sum, zip(*(plane[:frames] for plane in planes), strict=True))
    )
    return array(
        "h",
        [
            -INT16_MAX if value < -INT16_MAX else INT16_MAX if value > INT16_MAX else value
            for base, rest in (divmod(total, count) for total in totals)
            for value in (
                base + 1 if (rest * 2 > count or (rest * 2 == count and base & 1)) else base,
            )
        ],
    )


__all__ = ["apply_stereo_handling"]
