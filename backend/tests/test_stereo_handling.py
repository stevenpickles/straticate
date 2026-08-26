"""Feature 041 — the stereo-handling preprocessing choice attached to a job.

Two things are under test, and deliberately only two. **The transform**: a known
input folds to a known output, the default is identity, and an already-mono
source is left alone. **The wiring**: the choice on
:class:`~straticate.schemas.jobs.SeparationConfiguration` reaches the separator
and changes what it separates, on both torch backends, without changing anything
for a job that did not ask for it.

What is *not* here is separation quality. Whether folding to mono recovers a
stem a wide stereo image loses needs real weights and real music; that
measurement is recorded in ``docs/features/041-mono-folddown-option.md`` and
belongs to the integration tier, not to a suite that runs with no GPU, no
network and a ~22 000-parameter stand-in whose audio is meaningless by design.
"""

import hashlib
from array import array
from pathlib import Path
from typing import Any

import pytest

from straticate.inference.pcm import PcmAudio
from straticate.inference.stereo import apply_stereo_handling
from straticate.schemas.jobs import SeparationConfiguration, StereoHandling
from tests.audio_fixtures import read_wav, write_tone_wav

SAMPLE_RATE = 8000


def pcm(*planes: list[int], sample_rate: int = SAMPLE_RATE) -> PcmAudio:
    return PcmAudio(sample_rate=sample_rate, channels=tuple(array("h", plane) for plane in planes))


def samples(audio: PcmAudio, channel: int = 0) -> list[int]:
    return list(audio.channels[channel])


# --------------------------------------------------------------------------
# The transform
# --------------------------------------------------------------------------


def test_as_is_returns_the_very_object_it_was_given() -> None:
    """The default must be *identity*, not merely equality.

    "Existing jobs separate exactly as before" is this feature's hardest
    constraint, and the cheapest way to keep it true forever is for the default
    path to touch nothing at all — no round trip through float, no requantizing,
    no copy. ``is`` is the assertion that says so.
    """
    source = pcm([0, 1000, -1000, 32767], [5, -5, 15, -15])
    assert apply_stereo_handling(source, StereoHandling.AS_IS) is source


def test_the_field_defaults_to_as_is_when_a_request_omits_it() -> None:
    """A client written before this feature keeps its old behaviour."""
    configuration = SeparationConfiguration(
        audio_id="01AUDIO0000000000000000000", mode_id="vocals", quality_id="high_quality"
    )
    assert configuration.stereo_handling is StereoHandling.AS_IS
    assert (
        apply_stereo_handling((source := pcm([1, 2], [3, 4])), configuration.stereo_handling)
        is source
    )


def test_mono_folds_two_channels_to_their_mean() -> None:
    """``(L + R) / 2``, one channel out, sample rate untouched."""
    folded = apply_stereo_handling(
        pcm([1000, -1000, 0, 20000], [3000, 1000, 0, 10000]), StereoHandling.MONO
    )
    assert folded.channel_count == 1
    assert folded.sample_rate == SAMPLE_RATE
    assert folded.frame_count == 4
    assert samples(folded) == [2000, 0, 0, 15000]


def test_mono_rounds_to_nearest_rather_than_flooring() -> None:
    """The half-LSB that separates a mean from an integer division.

    ``(1 + 2) // 2`` is ``1`` and ``(-1 + -2) // 2`` is ``-2``: a floor biases
    every odd-sum frame downwards, which on a full track is a DC offset. The
    module goes through the shared float bridge precisely so the project's one
    rounding rule applies here too.
    """
    folded = apply_stereo_handling(pcm([1, -1, 3], [2, -2, 4]), StereoHandling.MONO)
    assert samples(folded) == [2, -2, 4]


def test_mono_cannot_overflow_at_full_scale() -> None:
    """A mean of two in-range samples is in range; the extremes prove it."""
    folded = apply_stereo_handling(
        pcm([32767, -32768, 32767], [32767, -32768, -32768]), StereoHandling.MONO
    )
    # -32768 is the one value a symmetric float scaling cannot represent; it
    # clamps to -32767, exactly as the shared bridge has always done.
    assert samples(folded) == [32767, -32767, 0]


def test_mono_leaves_an_already_mono_source_untouched() -> None:
    """Nothing to fold, so nothing is copied or requantized."""
    source = pcm([0, 1000, -1000, 500])
    assert apply_stereo_handling(source, StereoHandling.MONO) is source


def test_mono_is_the_mid_component_of_the_mid_side_pair() -> None:
    """States the identity the feature doc's measurement swept.

    ``L' = M + kS`` at ``k = 0`` is ``M``, so the fold-down is the ``k = 0``
    end of the mid/side narrowing that was measured against it. If this ever
    stopped being true the recorded comparison would no longer describe the
    shipped behaviour.
    """
    left, right = [900, -1200, 30000], [-300, 400, 10000]
    mid = [round((one + other) / 2) for one, other in zip(left, right, strict=True)]
    assert samples(apply_stereo_handling(pcm(left, right), StereoHandling.MONO)) == mid


# --------------------------------------------------------------------------
# The wiring: the configuration reaches the separator
# --------------------------------------------------------------------------


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """Two seconds of stereo tone; the two channels carry different tones."""
    return write_tone_wav(tmp_path / "source.wav", seconds=2.0, channels=2, sample_rate=22050)


def configuration(mode_id: str, quality_id: str, **overrides: Any) -> SeparationConfiguration:
    return SeparationConfiguration(
        audio_id="01AUDIO0000000000000000000",
        mode_id=mode_id,
        quality_id=quality_id,
        **overrides,
    )


async def separate(separator: Any, source: Path, output_dir: Path, **overrides: Any) -> Any:
    from straticate.inference.base import SeparationProgress
    from straticate.jobs.cancellation import CancellationToken

    def ignore(_progress: SeparationProgress) -> None:
        return None

    return await separator.separate(
        source,
        configuration(separator.info.separation_mode, "balanced", **overrides),
        ignore,
        CancellationToken(),
        job_id="01JOB00000000000000000041",
        output_dir=output_dir,
    )


def demucs_separator(tmp_path: Path) -> Any:
    from straticate.inference.demucs import DemucsSeparator
    from tests.demucs_fixtures import tiny_info, tiny_parameters, write_tiny_weights

    weights = write_tiny_weights(tmp_path / "weights" / "tiny-standard-001" / "weights.bin")
    return DemucsSeparator(tiny_info(), weights_file=weights, parameters=tiny_parameters())


def roformer_separator(tmp_path: Path) -> Any:
    from straticate.inference.roformer import RoFormerSeparator
    from tests.roformer_fixtures import tiny_info, tiny_parameters, write_tiny_weights

    weights = write_tiny_weights(tmp_path / "weights" / "tiny-vocals-001" / "weights.bin")
    return RoFormerSeparator(tiny_info(), weights_file=weights, parameters=tiny_parameters())


@pytest.mark.parametrize("build", [demucs_separator, roformer_separator])
async def test_mono_handling_yields_mono_stems(build: Any, source: Path, tmp_path: Path) -> None:
    """The choice reaches the separator and changes what comes out.

    A job that asks for the fold gets stems in the layout it actually
    separated: one channel, and a result record that says so rather than
    advertising a stereo file with two identical planes.
    """
    output = tmp_path / "stems"
    result = await separate(build(tmp_path), source, output, stereo_handling=StereoHandling.MONO)

    assert result.stems, "the separator produced no stems"
    for stem in result.stems:
        assert stem.channels == 1
        channels, rate, _frames, _samples = read_wav(output / f"{stem.name}.wav")
        assert (channels, rate) == (1, stem.sample_rate_hz)


@pytest.mark.parametrize("build", [demucs_separator, roformer_separator])
async def test_the_default_separates_exactly_as_before(
    build: Any, source: Path, tmp_path: Path
) -> None:
    """The acceptance criterion, asserted on bytes.

    A configuration that never mentions ``stereo_handling`` and one that names
    ``as_is`` explicitly must produce **the same files**, and they must still be
    stereo. The separator is rebuilt from the same synthetic weights for each
    run, so the comparison is of the two code paths and not of one cached
    network's mood.
    """
    implicit, explicit = tmp_path / "implicit", tmp_path / "explicit"
    result = await separate(build(tmp_path), source, implicit)
    await separate(build(tmp_path), source, explicit, stereo_handling=StereoHandling.AS_IS)

    for stem in result.stems:
        assert stem.channels == 2
        assert digest(implicit / f"{stem.name}.wav") == digest(explicit / f"{stem.name}.wav")


async def test_a_folded_roformer_run_still_reconstructs_its_mixture(
    source: Path, tmp_path: Path
) -> None:
    """The residual stem is derived from the audio that was *actually* separated.

    RoFormer's ``instrumental`` is the mixture minus ``vocals``, computed in
    :meth:`RoFormerSeparator._finish_stems` from the ``source`` the skeleton
    hands it. Folding after that, or folding a copy, would leave the residual
    subtracting a stereo mixture from a mono estimate. Summing the two stems
    back is what catches it.
    """
    from straticate.inference.pcm import decode_to_pcm
    from tests.roformer_fixtures import TINY_SAMPLE_RATE

    output = tmp_path / "stems"
    result = await separate(
        roformer_separator(tmp_path), source, output, stereo_handling=StereoHandling.MONO
    )

    stems = [read_wav(output / f"{stem.name}.wav")[3] for stem in result.stems]
    total = [sum(values) for values in zip(*stems, strict=True)]

    # The mixture the run actually separated: decoded at the model's rate, then
    # folded by the very function under test.
    decoded = await decode_to_pcm(source, sample_rate=TINY_SAMPLE_RATE, timeout_seconds=30.0)
    mixture = samples(apply_stereo_handling(decoded, StereoHandling.MONO))

    assert len(result.stems) == 2, "the residual construction is what is under test"
    assert len(total) == len(mixture)
    # Quantization happens once, at the end, so a two-stem sum reconstructs the
    # mixture to within a rounding step — not a fresh error per stem.
    assert max(abs(one - other) for one, other in zip(total, mixture, strict=True)) <= 2
