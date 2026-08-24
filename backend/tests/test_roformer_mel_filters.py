"""Tests for the vendored architecture's mel filter bank.

This is a small module with an outsized consequence. ``MelBandRoformer`` derives
the input width of every band-split and mask-estimator layer from
``mel_filter_bank(...) > 0`` — *which FFT bins land in which mel band* — so a
filter bank that disagrees with librosa's by a single bin builds a network whose
parameter shapes no longer match the published checkpoint, and the state dict
stops loading. Straticate replaced the librosa call (see
``src/straticate/inference/roformer/vendor/README.md``), so the replacement needs
a guard that fails in **normal CI**, with no weights present and no librosa
installed.

The guard is the shape of the real thing: ``KIM_VOCAL_2_BAND_WIDTHS`` below was
read out of the actual Kim Vocal 2 checkpoint
(``band_split.to_features.{i}.1.weight``'s input width, divided by four for
complex-times-stereo), so these numbers are not a snapshot of what this code
currently produces — they are what the checkpoint *requires*. The integration
tier (``test_roformer_integration.py``) closes the loop by loading the real
weights.
"""

import pytest

from straticate.inference.roformer.vendor.mel_filters import (
    fft_frequencies,
    hz_to_mel,
    mel_filter_bank,
    mel_frequencies,
    mel_to_hz,
)

try:  # pragma: no cover - numpy is a declared dependency; the guard is for clarity
    import numpy as np
except ImportError:  # pragma: no cover
    pytest.skip("numpy is required", allow_module_level=True)


KIM_VOCAL_2_SAMPLE_RATE = 44100
KIM_VOCAL_2_N_FFT = 2048
KIM_VOCAL_2_NUM_BANDS = 60

KIM_VOCAL_2_BAND_WIDTHS = [
    7, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 7, 7, 7, 9, 9,
    9, 10, 10, 11, 13, 13, 13, 15, 16, 17, 19, 20, 20, 22, 24, 26, 28, 29, 31, 33,
    36, 39, 41, 44, 47, 50, 54, 57, 61, 66, 71, 76, 80, 86, 93, 99, 105, 113, 122, 130,
]  # fmt: skip
"""FFT bins per mel band, read from the real checkpoint's layer shapes.

Do not "update" these to match a change in :mod:`mel_filters`. If they stop
matching, the filter bank changed and the checkpoint will no longer load — the
test is the alarm, not the record.
"""


def band_widths(sample_rate: int, n_fft: int, n_mels: int) -> list[int]:
    """Bins covered by each band — exactly what the architecture measures."""
    bank = mel_filter_bank(sample_rate=sample_rate, n_fft=n_fft, n_mels=n_mels)
    # The architecture forces these two entries positive before taking ``> 0``;
    # mirroring that here keeps this test measuring what the network measures.
    bank[0][0] = 1.0
    bank[-1, -1] = 1.0
    return [int(count) for count in (bank > 0).sum(axis=1)]


def test_band_widths_match_the_real_checkpoint() -> None:
    widths = band_widths(KIM_VOCAL_2_SAMPLE_RATE, KIM_VOCAL_2_N_FFT, KIM_VOCAL_2_NUM_BANDS)
    assert widths == KIM_VOCAL_2_BAND_WIDTHS
    assert sum(widths) == 1979


def test_every_frequency_is_covered_by_some_band() -> None:
    """The architecture asserts this; a filter bank that failed it would not build."""
    bank = mel_filter_bank(
        sample_rate=KIM_VOCAL_2_SAMPLE_RATE,
        n_fft=KIM_VOCAL_2_N_FFT,
        n_mels=KIM_VOCAL_2_NUM_BANDS,
    )
    bank[0][0] = 1.0
    bank[-1, -1] = 1.0
    assert bool((bank > 0).any(axis=0).all())


def test_shape_and_dtype_match_librosa_s_contract() -> None:
    bank = mel_filter_bank(sample_rate=22050, n_fft=512, n_mels=16)
    assert bank.shape == (16, 512 // 2 + 1)
    assert bank.dtype == np.float32
    assert bool((bank >= 0).all())


def test_bands_ascend_in_centre_frequency() -> None:
    """Band ``i`` peaks below band ``i + 1`` — the mel scale is monotonic."""
    bank = mel_filter_bank(sample_rate=44100, n_fft=2048, n_mels=60)
    peaks = [int(row.argmax()) for row in bank]
    assert peaks == sorted(peaks)


def test_hz_and_mel_round_trip() -> None:
    frequencies = np.asarray([0.0, 100.0, 999.0, 1000.0, 1001.0, 8000.0, 22050.0])
    assert np.allclose(mel_to_hz(hz_to_mel(frequencies.copy())), frequencies)


def test_the_scale_switches_from_linear_to_logarithmic_at_1_khz() -> None:
    """Below 1 kHz the Slaney scale is 3 mels per 200 Hz; above it, logarithmic."""
    linear = hz_to_mel(np.asarray([0.0, 200.0, 400.0, 800.0]))
    assert np.allclose(linear, [0.0, 3.0, 6.0, 12.0])
    assert float(hz_to_mel(np.asarray([1000.0]))[0]) == pytest.approx(15.0)
    # Two octaves above the knee are equally spaced in mels, which a linear
    # scale would not be.
    above = hz_to_mel(np.asarray([1000.0, 2000.0, 4000.0]))
    assert float(above[1] - above[0]) == pytest.approx(float(above[2] - above[1]))


def test_mel_frequencies_span_the_requested_range() -> None:
    centres = mel_frequencies(62, fmin=0.0, fmax=22050.0)
    assert len(centres) == 62
    assert float(centres[0]) == pytest.approx(0.0)
    assert float(centres[-1]) == pytest.approx(22050.0)
    assert list(centres) == sorted(centres)


def test_fft_frequencies_are_the_rfft_bin_centres() -> None:
    bins = fft_frequencies(44100, 2048)
    assert len(bins) == 1025
    assert float(bins[0]) == 0.0
    assert float(bins[-1]) == pytest.approx(22050.0)
    assert float(bins[1]) == pytest.approx(44100 / 2048)


@pytest.mark.parametrize(
    ("sample_rate", "n_fft", "n_mels"),
    [(44100, 2048, 60), (48000, 4096, 64), (8000, 64, 8), (16000, 128, 16)],
)
def test_widths_sum_to_at_least_the_bin_count(sample_rate: int, n_fft: int, n_mels: int) -> None:
    """Overlapping bands cover every bin at least once, usually about twice."""
    widths = band_widths(sample_rate, n_fft, n_mels)
    assert len(widths) == n_mels
    assert sum(widths) >= n_fft // 2 + 1
    assert all(width > 0 for width in widths)
