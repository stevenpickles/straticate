"""The PCM ↔ tensor bridge shared by every torch-backed separator.

Feature 039 moved :mod:`straticate.inference.torch_audio` out of the two
separators, which had byte-identical copies of it. The round-trip test came with
it — verbatim from ``tests/test_roformer_separator.py``, where feature 026 wrote
it and feature 028 copied it — because the code it guards now has one home.
``SAMPLE_RATE`` is the ``TINY_SAMPLE_RATE`` both fixture modules use; nothing
here depends on its value.
"""

from array import array

from straticate.inference.pcm import PcmAudio, interleave
from straticate.inference.torch_audio import pcm_to_tensor, tensor_to_pcm, to_source_channels

SAMPLE_RATE = 8000


def test_planar_conversion_round_trips_through_the_pcm_module() -> None:
    """Guards the int16-to-float bridge every stem is written through."""
    original = PcmAudio(
        sample_rate=SAMPLE_RATE,
        channels=(array("h", [0, 1000, -1000, 32767, -32768]), array("h", [5, -5, 15, -15, 25])),
    )
    tensor = pcm_to_tensor(original, 2)
    restored = tensor_to_pcm(tensor, SAMPLE_RATE)
    # -32768 clamps to -32767: one LSB, and the only value that cannot survive a
    # symmetric float scaling. Everything else is exact.
    assert list(interleave(restored))[:8] == list(interleave(original))[:8]
    assert restored.channels[0][4] == -32767


def test_a_mono_source_is_widened_and_folded_back() -> None:
    """The channel folding both backends rely on, checked in one place.

    A stereo-only checkpoint must not change how many channels a job returns:
    the bridge widens a mono source on the way in and
    :func:`~straticate.inference.torch_audio.to_source_channels` folds it back on
    the way out. Both separators' "a mono source yields mono stems" tests prove
    that end to end; this proves the conversion itself, which is now shared code.
    """
    mono = PcmAudio(sample_rate=SAMPLE_RATE, channels=(array("h", [0, 1000, -1000, 500]),))
    widened = pcm_to_tensor(mono, 2)
    assert widened.shape == (2, 4)
    assert bool((widened[0] == widened[1]).all())

    folded = to_source_channels(widened, 1)
    assert folded.shape == (1, 4)
    assert list(interleave(tensor_to_pcm(folded, SAMPLE_RATE))) == [0, 1000, -1000, 500]
