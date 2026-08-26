"""Stereo-image preprocessing applied to a mixture before it is separated.

Feature 041. Feature 028 measured a real 1968 stereo mix whose ``bass`` stem
came out effectively silent (-66.2 dBFS, peak 176/32767) while the other three
were healthy, and chased it to the mix rather than to the code: near-independent
channels (full-band L/R correlation **+0.23**, against 0.7 to 0.95 for modern
productions) and a low end hard-panned 5.8 dB left. Every separation model in
use is trained on material where the bass is essentially always centred, so such
a mix is outside the training distribution — and folding it to mono recovers the
stem entirely.

This module is that fold, and nothing else. It is one pure function over
:class:`~straticate.inference.pcm.PcmAudio`, applied by
:class:`~straticate.inference.torch_separator.TorchSeparator` immediately after
the decode and *only* when the job asked for it. It is never applied on the
model's behalf and never inferred from the audio: see
:class:`~straticate.schemas.jobs.StereoHandling`.

Why the fold, and not a partial narrowing
-----------------------------------------

The obvious alternative is mid/side with the side component **attenuated**
rather than removed — ``L' = M + kS``, ``R' = M - kS`` — which would keep some
stereo image. Feature 041 measured it across ``k ∈ {1, 0.75, 0.5, 0.35, 0.25,
0.1, 0}`` on the same track, same model, same settings, and it does not work:
the bass stem is unchanged (-65.7 → -65.4 dBFS) all the way down to ``k = 0.35``,
by which point the input's L/R correlation is already **+0.86** — inside the band
the model is trained on — and the first meaningful recovery arrives at
``k = 0.10``, where the correlation is +0.988 and the side component is 22 dB
below the mid. There is no ``k`` that both recovers the stem and leaves an
audible stereo image, so there is no partial setting worth exposing. The numbers
are in ``docs/features/041-mono-folddown-option.md``.

Why torch
---------

The arithmetic is a mean over two planes, which is trivial — but the planes are
16-bit integers and a 2:43 track is 7.2 million frames per channel, where the
straightforward ``array`` comprehension costs seconds of pure Python per job.
Crossing the bridge :mod:`straticate.inference.torch_audio` already owns makes it
milliseconds, and gets the project's rounding convention for free (round to
nearest, once, at the end — not the floor a ``//`` would give). Nothing outside
:class:`~straticate.inference.torch_separator.TorchSeparator` calls this, and
that class is torch by definition, so the dependency costs nothing that was not
already paid.
"""

from __future__ import annotations

from straticate.inference.pcm import PcmAudio
from straticate.inference.torch_audio import pcm_to_tensor, tensor_to_pcm
from straticate.schemas.jobs import StereoHandling


def apply_stereo_handling(source: PcmAudio, handling: StereoHandling) -> PcmAudio:
    """Return ``source`` with ``handling`` applied to its stereo image.

    Args:
        source: The decoded mixture.
        handling: What the job asked for.

    Returns:
        For :attr:`~straticate.schemas.jobs.StereoHandling.AS_IS`, **the very
        object passed in** — the default path must be bit-for-bit what it was
        before this feature existed, and identity is the only way to promise
        that. For :attr:`~straticate.schemas.jobs.StereoHandling.MONO`, a
        one-channel :class:`~straticate.inference.pcm.PcmAudio` holding
        ``(L + R) / 2`` at the same sample rate; a source that is *already*
        one channel is likewise returned unchanged, because folding it would
        be a copy with nothing to fold.
    """
    if handling is StereoHandling.AS_IS or source.channel_count < 2:
        return source
    planes = pcm_to_tensor(source, source.channel_count)
    return tensor_to_pcm(planes.mean(dim=0, keepdim=True), source.sample_rate)


__all__ = ["apply_stereo_handling"]
