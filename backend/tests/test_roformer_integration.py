"""The real-model tier: skipped by default, run on demand.

DEVELOPMENT.md's test strategy has always reserved a row for this — "GPU/model
integration · separate suite, manually triggered · requires CUDA GPU, model
downloads" — and ARCHITECTURE.md §14 forbids normal CI from needing either. So
every test here carries ``@pytest.mark.integration``, which ``pyproject.toml``
deselects by default::

    cd backend
    uv run --extra torch pytest -m integration          # the whole tier
    uv run --extra torch pytest -m "integration and gpu"

**On a host with the CUDA wheel installed, use ``--no-sync`` instead of
``--extra torch``** — the latter re-pins ``torch`` to the locked CPU wheel, and
the ``gpu`` test then skips itself on a machine that has a GPU. DEVELOPMENT.md,
*PyTorch and CUDA*, has both traps::

    uv run --no-sync pytest -m integration

They also need the real weights *installed*, which is feature 025's job::

    uv run --no-sync uvicorn straticate.main:app &
    curl -X POST localhost:8000/api/v1/models/vocals-hq-001/install
    curl localhost:8000/api/v1/models/vocals-hq-001    # watch installation.progress

A test whose prerequisites are absent skips with a message saying which.

What this tier is *for* is the one thing a synthetic checkpoint cannot prove:
that the vendored architecture and the published checkpoint are the same
network. If :func:`test_the_vendored_architecture_loads_the_real_checkpoint`
passes with no missing and no unexpected keys, the vendoring is correct; if it
fails, nothing else in feature 026 matters.
"""

import hashlib
import json
import time
from pathlib import Path
from typing import Any, cast

import pytest
import torch

from straticate.config import Settings
from straticate.inference.base import SeparationProgress
from straticate.inference.registry import separator_info_from_model
from straticate.inference.roformer import RoFormerParameters, RoFormerSeparator
from straticate.inference.roformer.vendor import MelBandRoformer
from straticate.jobs.cancellation import CancellationToken
from straticate.models import CATALOG_FILENAME, ModelCatalog, weights_path
from straticate.schemas.jobs import SeparationConfiguration
from tests.audio_fixtures import peak_amplitude, read_wav, write_tone_wav

pytestmark = pytest.mark.integration

MODEL_ID = "vocals-hq-001"
JOB_ID = "01JOB0000000000000INTEGR8"


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings()


@pytest.fixture(scope="module")
def catalog(settings: Settings) -> ModelCatalog:
    return ModelCatalog.from_directory(settings.models_dir)


@pytest.fixture(scope="module")
def installed_weights(settings: Settings) -> Path:
    path = weights_path(settings.models_dir, MODEL_ID)
    if not path.is_file():
        pytest.skip(
            f"{MODEL_ID} weights are not installed at {path}. "
            f"Install them with POST /api/v1/models/{MODEL_ID}/install (feature 025)."
        )
    return path


@pytest.fixture(scope="module")
def parameters(catalog: ModelCatalog) -> RoFormerParameters:
    return RoFormerParameters.from_catalog(
        catalog.inference_parameters(MODEL_ID), model_id=MODEL_ID
    )


def test_the_vendored_architecture_loads_the_real_checkpoint(
    installed_weights: Path, parameters: RoFormerParameters
) -> None:
    """No missing keys, no unexpected keys — the single highest-value check.

    ``load_state_dict(strict=False)`` is used *deliberately* here, so that a
    mismatch is reported as two lists rather than as one exception: the point of
    this test is to say exactly what diverged, not merely that something did.
    The separator itself loads with ``strict=True``.
    """
    model = MelBandRoformer(**parameters.model)
    state = torch.load(installed_weights, map_location="cpu", weights_only=True)
    incompatible: Any = model.load_state_dict(state, strict=False)

    assert list(cast("list[str]", incompatible.missing_keys)) == []
    assert list(cast("list[str]", incompatible.unexpected_keys)) == []
    assert sum(tensor.numel() for tensor in model.parameters()) > 200_000_000


def test_the_installed_weights_match_the_pinned_digest(
    installed_weights: Path, settings: Settings
) -> None:
    """Re-verify what feature 025 verified on the way in.

    The installer hashes once and never re-checks (a documented limitation), so
    this tier is the place that notices bit rot or a hand-copied file.
    """
    raw: dict[str, Any] = json.loads(
        (settings.models_dir / CATALOG_FILENAME).read_text(encoding="utf-8")
    )
    entry = next(item for item in raw["models"] if item["id"] == MODEL_ID)
    expected: str = entry["artifact"]["sha256"]

    digest = hashlib.sha256()
    with installed_weights.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)

    assert digest.hexdigest() == expected
    assert installed_weights.stat().st_size == entry["artifact"]["size_bytes"]


async def test_a_real_separation_produces_genuinely_separated_stems(
    catalog: ModelCatalog,
    installed_weights: Path,
    parameters: RoFormerParameters,
    tmp_path: Path,
) -> None:
    """End to end on a short clip, on whatever device this host offers.

    The assertions are the ones that hold for *any* input: two stems, both
    playable, not identical to one another, and summing back to the mixture. A
    musical judgement ("the vocal is isolated") is not something a generated
    tone can make — that is what listening to the output is for, and the feature
    document records what was heard.
    """
    device_id = "cuda:0" if torch.cuda.is_available() else "cpu"
    source = write_tone_wav(tmp_path / "clip.wav", seconds=10.0, channels=2, sample_rate=44100)
    output = tmp_path / "stems"

    separator = RoFormerSeparator(
        separator_info_from_model(catalog.get_model(MODEL_ID)),
        weights_file=installed_weights,
        parameters=parameters,
    )
    reports: list[SeparationProgress] = []

    started = time.monotonic()
    result = await separator.separate(
        source,
        SeparationConfiguration(
            audio_id="01AUDIO0000000000000000000",
            mode_id="vocals",
            quality_id="high_quality",
            device_id=device_id,
        ),
        reports.append,
        CancellationToken(),
        job_id=JOB_ID,
        output_dir=output,
    )
    elapsed = time.monotonic() - started

    print(
        f"\n[026] {device_id}: 10.0 s of audio in {elapsed:.1f} s "
        f"(RTF {result.metrics.realtime_factor:.3f}, "
        f"{reports[-1].chunks_total} chunks)"
    )

    assert [stem.name for stem in result.stems] == ["vocals", "instrumental"]
    vocals = output / "vocals.wav"
    instrumental = output / "instrumental.wav"
    assert vocals.read_bytes() != instrumental.read_bytes()
    for path in (vocals, instrumental):
        channels, rate, frames, samples = read_wav(path)
        assert (channels, rate) == (2, 44100)
        assert frames == pytest.approx(10.0 * 44100, rel=0.01)
        assert peak_amplitude(samples) >= 0
    assert reports[-1].chunks_completed == reports[-1].chunks_total > 1
    assert result.metrics.realtime_factor > 0.0


@pytest.mark.gpu
async def test_cuda_runtime_stats_report_real_memory(
    catalog: ModelCatalog,
    installed_weights: Path,
    parameters: RoFormerParameters,
    tmp_path: Path,
) -> None:
    """The CUDA telemetry path. Skips on a CPU-only host; **has now run.**

    Feature 026 shipped with this test never executed, because the host it was
    developed on had no GPU. It first ran on **2026-08-25**, on an NVIDIA
    GeForce RTX 4060 Laptop GPU (8,188 MiB, driver 610.47 / CUDA 13.3) with
    ``torch 2.13.0+cu130``, and passed — with the whole tier at 4/4.

    What it measured on that run: ``cuda:0``, the real device name,
    ``memory_total_bytes`` 8,585,281,536, and ``memory_peak_bytes`` of
    **1,531.7 MiB** for the 5 s clip below at this model's
    ``chunk_size: 352800`` (2 chunks). NVML was installed (``nvidia-ml-py``,
    never ``pynvml`` — see DEVELOPMENT.md), so the two optional fields were
    populated rather than ``None``: utilization 1.0 at 59 °C. Both branches of
    the last two assertions are therefore real, not hypothetical.

    Feature 036 measured the memory behaviour properly and corrected the
    catalog's ``recommended_vram_mb`` from it; the peak here is **not** bounded
    by chunking, and grows with the length of the input.

    On a GPU host, run this with ``uv run --no-sync`` or it will not be a GPU
    run — see DEVELOPMENT.md, *PyTorch and CUDA*.
    """
    if not torch.cuda.is_available():
        pytest.skip("no CUDA device is available")

    separator = RoFormerSeparator(
        separator_info_from_model(catalog.get_model(MODEL_ID)),
        weights_file=installed_weights,
        parameters=parameters,
    )
    source = write_tone_wav(tmp_path / "clip.wav", seconds=5.0, channels=2, sample_rate=44100)
    await separator.separate(
        source,
        SeparationConfiguration(
            audio_id="01AUDIO0000000000000000000",
            mode_id="vocals",
            quality_id="high_quality",
            device_id="cuda:0",
        ),
        lambda _: None,
        CancellationToken(),
        job_id=JOB_ID,
        output_dir=tmp_path / "stems",
    )

    stats = separator.runtime_stats()
    assert stats is not None
    device = stats.device
    assert device is not None, "a CUDA run must report a device block"
    assert device.backend == "cuda"
    assert device.device_id == "cuda:0"
    assert device.memory_total_bytes > 0
    assert device.memory_peak_bytes > 0
    assert device.memory_peak_bytes >= device.memory_allocated_bytes
    # NVML is optional (ARCHITECTURE.md §12): present or absent, both are legal.
    assert device.utilization is None or 0.0 <= device.utilization <= 1.0
    assert device.temperature_celsius is None or device.temperature_celsius > 0.0


@pytest.mark.gpu
async def test_peak_device_memory_is_flat_across_track_lengths(
    catalog: ModelCatalog,
    installed_weights: Path,
    parameters: RoFormerParameters,
    tmp_path: Path,
) -> None:
    """Feature 038's acceptance criterion, on the hardware it is a claim about.

    Twelve times the audio, twelve times the chunks, the *same* peak. Before 038
    the decoded mixture, the accumulator and the weight tensor were all
    device-resident and whole-track, so the peak grew at ≈1.35 MiB per second of
    audio (feature 036's sweep) — over the 220 s of extra audio below, ≈297 MiB.
    The tolerance is a small fraction of that, so this test fails if the
    accumulator ever goes back on the card.

    Measured on **2026-08-25**, RTX 4060 Laptop GPU, ``torch 2.13.0+cu130``:
    1,526.1 MiB for both clips, and for 6-minute and 10-minute clips too — the
    same figure to the byte, because what is on the card no longer depends on
    the length of the track at all. Feature 038's document has the full sweep and
    the ``requirements`` re-derived from it.

    Both clips run in **one process on one separator**, deliberately: the
    skeleton resets the CUDA peak per run, and comparing two runs that share a
    resident network is what isolates the part of the peak that scales.

    On a GPU host, run this with ``uv run --no-sync`` or it will not be a GPU
    run — see DEVELOPMENT.md, *PyTorch and CUDA*.
    """
    if not torch.cuda.is_available():
        pytest.skip("no CUDA device is available")

    separator = RoFormerSeparator(
        separator_info_from_model(catalog.get_model(MODEL_ID)),
        weights_file=installed_weights,
        parameters=parameters,
    )
    peaks: dict[float, int] = {}
    chunks: dict[float, int] = {}
    for seconds in (20.0, 240.0):
        reports: list[SeparationProgress] = []
        source = write_tone_wav(
            tmp_path / f"clip-{seconds:.0f}.wav", seconds=seconds, channels=2, sample_rate=44100
        )
        await separator.separate(
            source,
            SeparationConfiguration(
                audio_id="01AUDIO0000000000000000000",
                mode_id="vocals",
                quality_id="high_quality",
                device_id="cuda:0",
            ),
            reports.append,
            CancellationToken(),
            job_id=JOB_ID,
            output_dir=tmp_path / f"stems-{seconds:.0f}",
        )
        stats = separator.runtime_stats()
        assert stats is not None and stats.device is not None
        peaks[seconds] = stats.device.memory_peak_bytes
        chunks[seconds] = reports[-1].chunks_total

    growth = peaks[240.0] - peaks[20.0]
    print(
        f"\n[038] cuda:0 peak {peaks[20.0] / 1024**2:.1f} MiB at 20 s "
        f"({chunks[20.0]} chunks) → {peaks[240.0] / 1024**2:.1f} MiB at 4 min "
        f"({chunks[240.0]} chunks); growth {growth / 1024**2:+.1f} MiB"
    )
    assert chunks[240.0] > 10 * chunks[20.0], "the longer clip must really be more chunks"
    assert growth < 64 * 1024**2, (
        f"peak grew by {growth / 1024**2:.1f} MiB over 220 s of extra audio; "
        f"something whole-track is back on the device"
    )
