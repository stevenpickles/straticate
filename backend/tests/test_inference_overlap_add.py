"""``HostOverlapAdd`` — feature 038's host-resident overlap-add buffer.

Two properties matter here and nothing else does. The buffers must be on the
**host**, because that is the whole point: it is what makes peak device memory a
function of the window rather than of the length of the track. And the result
must be **bit-identical** to the device-resident whole-track accumulation it
replaces, because a memory optimisation that changes the audio is a different
feature, and a silent one.

The second is checked against a reference implementation written the way both
backends were written before 038 — a full-length sum and a full-length weight,
summed with the same operators in the same order — rather than against a
tolerance. ``torch.equal`` or nothing.
"""

from __future__ import annotations

import pytest
import torch
from torch import Tensor

from straticate.inference.torch_overlap_add import HostOverlapAdd

SAMPLES = 5_000
WINDOW = 1_024
STRIDE = 512
OUTPUTS = 4
CHANNELS = 2
EPSILON = 1e-8


def windows(seed: int = 20260038) -> list[tuple[int, Tensor, Tensor]]:
    """One ``(offset, estimate, envelope)`` per overlapping window, deterministic."""
    generator = torch.Generator().manual_seed(seed)
    envelope = torch.rand(WINDOW, generator=generator, dtype=torch.float32) + 0.25
    produced: list[tuple[int, Tensor, Tensor]] = []
    for offset in range(0, SAMPLES, STRIDE):
        length = min(WINDOW, SAMPLES - offset)
        estimate = torch.randn(
            (OUTPUTS, CHANNELS, length), generator=generator, dtype=torch.float32
        )
        produced.append((offset, estimate, envelope[:length]))
    return produced


def whole_track_reference(placed: list[tuple[int, Tensor, Tensor]]) -> Tensor:
    """The pre-038 accumulation: full-length sum, full-length weight, one divide."""
    shape = (OUTPUTS, CHANNELS, SAMPLES)
    accumulator = torch.zeros(shape, dtype=torch.float32)
    weights = torch.zeros(shape, dtype=torch.float32)
    for offset, estimate, envelope in placed:
        length = int(estimate.shape[-1])
        accumulator[..., offset : offset + length] += estimate * envelope
        weights[..., offset : offset + length] += envelope
    return accumulator / weights.clamp(min=EPSILON)


def test_the_whole_track_buffers_live_on_the_host() -> None:
    """The property the feature exists for, asserted directly."""
    accumulator = HostOverlapAdd((OUTPUTS, CHANNELS, SAMPLES), SAMPLES)

    assert accumulator.weighted_sum.device.type == "cpu"
    assert accumulator.weight.device.type == "cpu"
    assert accumulator.weighted_sum.dtype is torch.float32
    assert accumulator.weight.dtype is torch.float32


def test_the_weight_is_one_number_per_sample_not_one_per_output_channel() -> None:
    """Feature 039's recorded lead, taken here.

    RoFormer allocated its weight accumulator at the full ``(stems, channels,
    samples)`` when the same envelope is broadcast into every stem and every
    channel; Demucs already held a vector. The vector is what this class holds,
    and :func:`test_streaming_is_bit_identical_to_the_whole_track_accumulation`
    is what says the divide is unchanged by the narrowing.
    """
    accumulator = HostOverlapAdd((OUTPUTS, CHANNELS, SAMPLES), SAMPLES)

    assert accumulator.weight.shape == (SAMPLES,)
    assert accumulator.weighted_sum.shape == (OUTPUTS, CHANNELS, SAMPLES)


def test_streaming_is_bit_identical_to_the_whole_track_accumulation() -> None:
    """038's non-negotiable constraint, at the seam that changed."""
    placed = windows()
    expected = whole_track_reference(placed)

    accumulator = HostOverlapAdd((OUTPUTS, CHANNELS, SAMPLES), SAMPLES)
    for offset, estimate, envelope in placed:
        accumulator.add(offset, estimate * envelope, envelope)
    resolved = accumulator.resolve(minimum_weight=EPSILON)

    assert torch.equal(resolved, expected), "streaming overlap-add moved the audio"


def test_resolve_divides_in_place_so_the_accumulator_is_never_held_twice() -> None:
    """The largest tensor in a run; there is no reason to have two of it."""
    accumulator = HostOverlapAdd((OUTPUTS, CHANNELS, SAMPLES), SAMPLES)
    for offset, estimate, envelope in windows():
        accumulator.add(offset, estimate * envelope, envelope)

    resolved = accumulator.resolve(minimum_weight=EPSILON)

    assert resolved.data_ptr() == accumulator.weighted_sum.data_ptr()


def test_an_uncovered_sample_is_floored_rather_than_dividing_by_zero() -> None:
    """Upstream's ``assert sum_weight.min() > 0`` restated as a clamp."""
    accumulator = HostOverlapAdd((1, 1, 4), 4)
    accumulator.add(0, torch.ones((1, 1, 2)), torch.ones(2))

    resolved = accumulator.resolve(minimum_weight=EPSILON)

    assert torch.isfinite(resolved).all()
    assert resolved[0, 0, 2] == 0.0


def test_a_window_whose_envelope_does_not_match_its_estimate_is_refused() -> None:
    accumulator = HostOverlapAdd((1, 1, 16), 16)

    with pytest.raises(ValueError, match="envelope covers"):
        accumulator.add(0, torch.ones((1, 1, 8)), torch.ones(4))


def test_a_window_that_runs_off_the_end_is_refused() -> None:
    accumulator = HostOverlapAdd((1, 1, 16), 16)

    with pytest.raises(ValueError, match="falls outside"):
        accumulator.add(12, torch.ones((1, 1, 8)), torch.ones(8))


@pytest.mark.parametrize(
    ("shape", "samples", "fragment"),
    [
        ((1, 1, 16), 0, "must be positive"),
        ((1, 1, 16), 15, "must end in the sample count"),
        ((), 4, "must end in the sample count"),
    ],
)
def test_a_buffer_that_cannot_describe_the_track_is_refused(
    shape: tuple[int, ...], samples: int, fragment: str
) -> None:
    """A silent mismatch here would be a silently truncated stem."""
    with pytest.raises(ValueError, match=fragment):
        HostOverlapAdd(shape, samples)
