"""Attend -- attention core with automatic flash/math/mem-efficient backend selection.

VENDORED CODE -- see README.md in this directory for provenance and for the
complete list of Straticate's modifications.

Adapted from lucidrains-style attention wrappers: picks PyTorch's SDPA backend based
on GPU compute capability at construction time (A100 gets flash-only; other CUDA
devices get math/mem-efficient) rather than letting PyTorch guess per call, and falls
back to a plain einsum attention path when `flash=False` so behavior matches the
non-flash checkpoints this package loads. The optional `scale` constructor arg lets a
caller override the default `head_dim ** -0.5` softmax scale (defaults to None, which
preserves existing behavior). Requires PyTorch >= 2.0 when flash is enabled.

Reads: torch (nn, scaled_dot_product_attention, nn.attention.sdpa_kernel), logging
"""

import logging

import torch
from torch import nn, einsum
import torch.nn.functional as F
from torch.nn.attention import SDPBackend, sdpa_kernel

# Straticate modification: the upstream file selects SDPA backends through
# ``torch.backends.cuda.sdp_kernel(enable_flash=..., enable_math=...,
# enable_mem_efficient=...)``, which torch deprecated in favour of
# ``torch.nn.attention.sdpa_kernel([SDPBackend, ...])`` and which now emits a
# FutureWarning (the backend suite treats a warning as a finding -- see
# DEVELOPMENT.md). The backend *selection* below is unchanged; only the API
# used to express it is. The ``packaging``-based "torch >= 2.0" assertion is
# gone too: ``backend/pyproject.toml`` declares a torch floor of 2.4.

_ALL_BACKENDS = (SDPBackend.FLASH_ATTENTION, SDPBackend.EFFICIENT_ATTENTION, SDPBackend.MATH)
_MATH_AND_MEM_EFFICIENT = (SDPBackend.EFFICIENT_ATTENTION, SDPBackend.MATH)
_FLASH_ONLY = (SDPBackend.FLASH_ATTENTION,)

logger = logging.getLogger(__name__)

# helpers

def exists(val):
    return val is not None

def default(v, d):
    return v if exists(v) else d

# main class

class Attend(nn.Module):
    def __init__(
        self,
        dropout = 0.,
        flash = False,
        scale = None
    ):
        super().__init__()
        self.scale = scale
        self.dropout = dropout
        self.attn_dropout = nn.Dropout(dropout)

        self.flash = flash

        # determine efficient attention configs for cuda and cpu

        # Straticate modification: ``print_once`` wrote the backend choice to
        # stdout, which has no place in a server process; it is a debug log now.
        self.cpu_config = _ALL_BACKENDS
        self.cuda_config = None

        if not torch.cuda.is_available() or not flash:
            return

        device_properties = torch.cuda.get_device_properties(torch.device('cuda'))

        if device_properties.major == 8 and device_properties.minor == 0:
            logger.debug('A100 GPU detected, using flash attention if input tensor is on cuda')
            self.cuda_config = _FLASH_ONLY
        else:
            logger.debug('Non-A100 GPU detected, using math or mem efficient attention if input tensor is on cuda')
            self.cuda_config = _MATH_AND_MEM_EFFICIENT

    def flash_attn(self, q, k, v):
        _, heads, q_len, _, k_len, is_cuda, device = *q.shape, k.shape[-2], q.is_cuda, q.device

        if exists(self.scale):
            default_scale = q.shape[-1] ** -0.5
            q = q * (self.scale / default_scale)

        # Check if there is a compatible device for flash attention

        config = default(self.cuda_config, self.cpu_config) if is_cuda else self.cpu_config

        # pytorch 2.0 flash attn: q, k, v, mask, dropout, softmax_scale

        with sdpa_kernel(list(config)):
            out = F.scaled_dot_product_attention(
                q, k, v,
                dropout_p = self.dropout if self.training else 0.
            )

        return out

    def forward(self, q, k, v):
        """
        einstein notation
        b - batch
        h - heads
        n, i, j - sequence length (base sequence length, source, target)
        d - feature dimension
        """

        q_len, k_len, device = q.shape[-2], k.shape[-2], q.device

        scale = default(self.scale, q.shape[-1] ** -0.5)

        if self.flash:
            return self.flash_attn(q, k, v)

        # similarity

        sim = einsum(f"b h i d, b h j d -> b h i j", q, k) * scale

        # attention

        attn = sim.softmax(dim=-1)
        attn = self.attn_dropout(attn)

        # aggregate values

        out = einsum(f"b h i j, b h j d -> b h i d", attn, v)

        return out