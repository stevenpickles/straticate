"""``HostOverlapAdd`` — the overlap-add accumulator that lives on the host.

Feature 038. Both torch backends cut the mixture into overlapping windows, run
one forward pass per window and sum the results back into a full-length buffer
that is finally divided by the accumulated window weight. Until this feature
that buffer — and the weight beside it, and the decoded mixture it was cut from
— sat on the **compute device** for the length of the run, so peak VRAM grew
linearly with the track: ≈1.35 MiB per second of audio for RoFormer and
≈1.85 for Demucs (features 036 and 028 measured both). A 4 GiB card therefore
exhausted at roughly nine minutes of audio, part-way through a job the user had
already waited minutes for.

Chunking never bounded that, and could not: it bounds the *working* set, not the
total. What bounds the total is where the whole-track tensors live. This class
is that decision, made once for both backends — the sum and the weight are
``torch.float32`` tensors on the **CPU**, and the only things that ever reach
the device are one window of mixture and the estimate that comes back. Peak
device memory becomes a function of the window, not of the duration.

Why the two-tensor split, and why the weight is a vector
-------------------------------------------------------

The weight is ``(samples,)`` — one number per sample — not the accumulator's
full ``(outputs, channels, samples)``. Every output and every channel of a given
sample accumulates the *same* window envelope, so the larger shape stored the
same value ``outputs x channels`` times and then broadcast it back out at the
divide. Demucs already did it the small way; RoFormer did not, and feature 039
recorded that as a lead rather than taking it. It is taken here, and it is the
one arithmetic-shape change in this feature: broadcasting a ``(samples,)``
divisor over ``(outputs, channels, samples)`` reads exactly the same values the
expanded tensor held, so the quotient is bit-identical.

Bit-identity
------------

The output of a separation must not move by one bit (038's non-negotiable
constraint), which decides precisely *which* arithmetic may cross to the host.
Every operation here is **element-wise** — a multiply, an add, a clamp, a
divide — and element-wise ``float32`` arithmetic is correctly rounded under
IEEE 754 on both CPU and CUDA, so the same inputs give the same bits wherever
they run. A **reduction** is the operation that does not have that property: a
CUDA tree reduction and a CPU cascade sum over a million elements disagree in
the last bits, which is why anything that reduces over the whole track (Demucs'
normalization statistics) stays on the device rather than moving here.

The per-chunk device→host copy is synchronous for pageable destination memory,
so :meth:`HostOverlapAdd.add` needs no explicit stream synchronization.
"""

from __future__ import annotations

import torch
from torch import Tensor

HOST: torch.device = torch.device("cpu")
"""Where the whole-track buffers live, whatever device the network runs on."""


class HostOverlapAdd:
    """A full-length overlap-add buffer on the host, fed one window at a time.

    Args:
        shape: Shape of the accumulator, ``(outputs, channels, samples)`` for
            both current backends. Its last dimension is the sample axis.
        samples: Length of the sample axis — the weight vector's length. Passed
            explicitly rather than read off ``shape`` so that a caller cannot
            silently accumulate against a different track length than it thinks.

    Raises:
        ValueError: ``samples`` is not positive, or does not match ``shape``'s
            last dimension.
    """

    __slots__ = ("_weight", "_weighted_sum")

    def __init__(self, shape: tuple[int, ...], samples: int) -> None:
        if samples <= 0:
            raise ValueError(f"samples must be positive, got {samples}")
        if not shape or shape[-1] != samples:
            raise ValueError(f"shape {shape} must end in the sample count {samples}")
        self._weighted_sum = torch.zeros(shape, dtype=torch.float32, device=HOST)
        self._weight = torch.zeros(samples, dtype=torch.float32, device=HOST)

    @property
    def weighted_sum(self) -> Tensor:
        """The accumulated ``estimate x envelope`` sum, on the host."""
        return self._weighted_sum

    @property
    def weight(self) -> Tensor:
        """The accumulated per-sample envelope weight, on the host."""
        return self._weight

    def add(self, offset: int, weighted: Tensor, envelope: Tensor) -> None:
        """Add one window's contribution at ``offset``.

        Both tensors are copied off the compute device here and nowhere else,
        which is what keeps device residency bounded by the window.

        Args:
            offset: First sample this window covers, in accumulator coordinates.
            weighted: ``estimate x envelope`` for this window, shaped like the
                accumulator with a shorter sample axis. Multiplying by the
                envelope on the *device* — where the estimate already is — is
                deliberate: it is one device-side element-wise multiply that the
                previous implementation also performed, so the bits crossing to
                the host are the bits the previous implementation accumulated.
            envelope: The window envelope itself, ``(length,)``, whose sum over
                overlapping windows is the divisor :meth:`resolve` applies.

        Raises:
            ValueError: The two lengths disagree, or the window runs past the
                end of the accumulator.
        """
        length = int(weighted.shape[-1])
        if int(envelope.shape[-1]) != length:
            raise ValueError(
                f"envelope covers {int(envelope.shape[-1])} samples, weighted covers {length}"
            )
        if offset < 0 or offset + length > int(self._weight.shape[-1]):
            raise ValueError(
                f"window [{offset}, {offset + length}) falls outside the "
                f"{int(self._weight.shape[-1])}-sample accumulator"
            )
        self._weighted_sum[..., offset : offset + length] += weighted.to(HOST, torch.float32)
        self._weight[offset : offset + length] += envelope.to(HOST, torch.float32)

    def resolve(self, *, minimum_weight: float) -> Tensor:
        """Divide the accumulated sum by the accumulated weight, in place.

        The division is in place because the accumulator is the largest tensor
        in the run and there is no reason to hold two of it; the values are
        those of an out-of-place divide either way.

        Args:
            minimum_weight: Floor applied to the divisor. Every sample is
                covered by at least one window and every envelope is positive,
                so this only ever guards against a degenerate configuration —
                it is upstream's ``assert sum_weight.min() > 0`` restated as a
                clamp rather than as a crash.

        Returns:
            The accumulator, on the host, in ``torch.float32``.
        """
        return self._weighted_sum.div_(self._weight.clamp(min=minimum_weight))


__all__ = ["HOST", "HostOverlapAdd"]
