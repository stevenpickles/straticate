"""Slaney mel filter bank — a dependency-free stand-in for ``librosa.filters.mel``.

**This module is not upstream code.** It is the one substitution Straticate makes
inside the vendored Mel-Band RoFormer architecture (see ``README.md`` in this
directory), and it exists for exactly one reason: the architecture calls
``librosa.filters.mel(sr=…, n_fft=…, n_mels=…)`` once, at construction, to decide
which FFT bins belong to which mel band — and pulling librosa in for that single
pure function would add numba, llvmlite, scipy, soundfile, audioread, pooch and
friends to an install that ARCHITECTURE.md §14 requires to stay lean.

**Exactness is load-bearing, not cosmetic.** ``MelBandRoformer`` derives the input
width of every ``BandSplit``/``MaskEstimator`` layer from ``mel_filter_bank > 0``,
so a filter bank that differs from librosa's by a single bin produces a network
whose parameter shapes no longer match the published checkpoint and the state dict
stops loading. The functions below are therefore a faithful transcription of
librosa's own implementation (``librosa.filters.mel`` /
``librosa.mel_frequencies`` / ``librosa.hz_to_mel`` / ``librosa.mel_to_hz`` /
``librosa.fft_frequencies``, licensed ISC), restricted to the defaults the
architecture actually uses — ``fmin=0``, ``fmax=sr/2``, ``htk=False``,
``norm="slaney"``, ``dtype=float32`` — and computed with the same NumPy
primitives, in the same order, at the same precisions.

Two tests keep it honest: ``tests/test_roformer_mel_filters.py`` pins the band
widths this produces for the shipped Kim Vocal 2 configuration against the widths
read out of the real checkpoint's layer shapes (so drift fails in normal CI, with
no weights present), and the manually-triggered integration test loads the real
state dict and asserts no missing or unexpected keys.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

_F_SP = 200.0 / 3
"""Hz per mel in the linear (below 1 kHz) region of the Slaney scale."""

_MIN_LOG_HZ = 1000.0
"""Frequency at which the Slaney scale switches from linear to logarithmic."""

_MIN_LOG_MEL = _MIN_LOG_HZ / _F_SP
"""``_MIN_LOG_HZ`` expressed in mels (``f_min`` is 0 for every use here)."""

_LOGSTEP = float(np.log(6.4) / 27.0)
"""Mels per log-unit above ``_MIN_LOG_HZ`` — 6.4 octaves-ish over 27 mels."""


def hz_to_mel(frequencies: NDArray[np.float64]) -> NDArray[np.float64]:
    """Convert Hz to Slaney mels (``htk=False``), elementwise."""
    mels = frequencies / _F_SP
    log_t = frequencies >= _MIN_LOG_HZ
    mels[log_t] = _MIN_LOG_MEL + np.log(frequencies[log_t] / _MIN_LOG_HZ) / _LOGSTEP
    return mels


def mel_to_hz(mels: NDArray[np.float64]) -> NDArray[np.float64]:
    """Convert Slaney mels back to Hz (``htk=False``), elementwise."""
    freqs = _F_SP * mels
    log_t = mels >= _MIN_LOG_MEL
    freqs[log_t] = _MIN_LOG_HZ * np.exp(_LOGSTEP * (mels[log_t] - _MIN_LOG_MEL))
    return freqs


def mel_frequencies(n_mels: int, fmin: float, fmax: float) -> NDArray[np.float64]:
    """``n_mels`` centre frequencies in Hz, evenly spaced on the Slaney mel scale."""
    min_mel = float(hz_to_mel(np.asarray([fmin], dtype=np.float64))[0])
    max_mel = float(hz_to_mel(np.asarray([fmax], dtype=np.float64))[0])
    return mel_to_hz(np.linspace(min_mel, max_mel, n_mels, dtype=np.float64))


def fft_frequencies(sample_rate: int, n_fft: int) -> NDArray[np.float64]:
    """Centre frequency in Hz of each of the ``1 + n_fft // 2`` rFFT bins."""
    return np.fft.rfftfreq(n=n_fft, d=1.0 / sample_rate)


def mel_filter_bank(*, sample_rate: int, n_fft: int, n_mels: int) -> NDArray[np.float32]:
    """Return the ``(n_mels, 1 + n_fft // 2)`` Slaney-normalized mel filter bank.

    Equivalent to ``librosa.filters.mel(sr=sample_rate, n_fft=n_fft,
    n_mels=n_mels)`` with librosa's defaults (``fmin=0``, ``fmax=sample_rate/2``,
    ``htk=False``, ``norm="slaney"``, ``dtype=float32``).

    Args:
        sample_rate: Sample rate the STFT was taken at, in Hz.
        n_fft: FFT size of that STFT.
        n_mels: Number of mel bands.

    Returns:
        The filter bank as ``float32``, one row per band. The architecture uses
        only ``bank > 0`` (which bins a band covers), but the magnitudes are
        computed anyway so this stays a drop-in replacement.
    """
    fmax = float(sample_rate) / 2
    weights = np.zeros((n_mels, int(1 + n_fft // 2)), dtype=np.float32)

    fftfreqs = fft_frequencies(sample_rate, n_fft)
    mel_f = mel_frequencies(n_mels + 2, fmin=0.0, fmax=fmax)

    fdiff = np.diff(mel_f)
    ramps = np.subtract.outer(mel_f, fftfreqs)

    for index in range(n_mels):
        # Lower and upper slopes of band ``index``, in units of the mel spacing
        # either side of its centre; their pointwise minimum, floored at zero,
        # is the triangular response.
        lower = -ramps[index] / fdiff[index]
        upper = ramps[index + 2] / fdiff[index + 1]
        weights[index] = np.maximum(0, np.minimum(lower, upper))

    # Slaney normalization: each band integrates to the same area, so wide
    # high-frequency bands are not louder than narrow low-frequency ones.
    enorm = 2.0 / (mel_f[2 : n_mels + 2] - mel_f[:n_mels])
    weights *= enorm[:, np.newaxis]

    return weights


__all__ = [
    "fft_frequencies",
    "hz_to_mel",
    "mel_filter_bank",
    "mel_frequencies",
    "mel_to_hz",
]
