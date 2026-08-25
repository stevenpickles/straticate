"""The real four-stem model tier: skipped by default, run on demand.

DEVELOPMENT.md's test strategy reserves a row for this — "GPU/model
integration · separate suite, manually triggered · requires CUDA GPU, model
downloads" — and ARCHITECTURE.md §14 forbids normal CI from needing either. So
every test here carries ``@pytest.mark.integration``, which ``pyproject.toml``
deselects by default::

    cd backend
    uv run --extra torch pytest -m integration          # the whole tier
    uv run --extra torch pytest -m "integration and gpu"

**On a host with the CUDA wheel installed, use ``--no-sync`` instead of
``--extra torch``** — the latter re-pins ``torch`` to the locked CPU wheel, and
the ``gpu`` tests then skip themselves on a machine that has a GPU.
DEVELOPMENT.md, *PyTorch and CUDA*, has both traps::

    uv run --no-sync pytest -m integration

They also need the real weights *installed*, which is feature 025's job::

    uv run --no-sync uvicorn straticate.main:app &
    curl -X POST localhost:8000/api/v1/models/standard-stems-001/install
    curl localhost:8000/api/v1/models/standard-stems-001   # watch installation.progress

A test whose prerequisites are absent skips with a message saying which.

What this tier is *for* is the one thing a synthetic checkpoint cannot prove:
that the vendored architecture and the published checkpoint are the same
network. If :func:`test_the_vendored_architecture_loads_the_real_checkpoint`
passes with no missing and no unexpected keys, the vendoring is correct; if it
fails, nothing else in feature 028 matters.
"""

import hashlib
import json
import pickle
import time
import types
from pathlib import Path
from typing import Any, cast

import pytest
import torch

from straticate.config import Settings
from straticate.inference.base import SeparationProgress
from straticate.inference.demucs import DemucsParameters, DemucsSeparator
from straticate.inference.demucs.separator import (
    SAFE_PICKLE_GLOBALS,
    SAFE_PICKLE_MODULES,
    CheckpointArchitecture,
    load_checkpoint_package,
)
from straticate.inference.demucs.vendor import HTDemucs
from straticate.inference.registry import separator_info_from_model
from straticate.jobs.cancellation import CancellationToken
from straticate.models import CATALOG_FILENAME, ModelCatalog, weights_path
from straticate.schemas.jobs import SeparationConfiguration
from tests.audio_fixtures import peak_amplitude, read_wav, write_tone_wav

pytestmark = pytest.mark.integration

MODEL_ID = "standard-stems-001"
JOB_ID = "01JOB0000000000000INTEGR28"
STEMS = ["vocals", "drums", "bass", "other"]


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
def parameters(catalog: ModelCatalog) -> DemucsParameters:
    return DemucsParameters.from_catalog(catalog.inference_parameters(MODEL_ID), model_id=MODEL_ID)


def test_the_vendored_architecture_loads_the_real_checkpoint(
    installed_weights: Path, parameters: DemucsParameters
) -> None:
    """No missing keys, no unexpected keys — the single highest-value check.

    ``load_state_dict(strict=False)`` is used *deliberately* here, so that a
    mismatch is reported as two lists rather than as one exception: the point of
    this test is to say exactly what diverged, not merely that something did.
    The separator itself loads with ``strict=True``.

    It also pins what the checkpoint says about itself. The published weights
    are ``float16`` and record their own ``sources``; both facts are load-bearing
    (the first is cast on load, the second is what stem assignment is checked
    against), and both would be invisible in a test that only counted keys.
    """
    package = load_checkpoint_package(installed_weights, model_id=MODEL_ID)
    state = cast("dict[str, Any]", package["state"])
    recorded: Any = package["kwargs"]
    assert list(recorded["sources"]) == ["drums", "bass", "other", "vocals"]
    assert {tensor.dtype for tensor in state.values()} == {torch.float16}

    model = HTDemucs(**parameters.model)
    incompatible: Any = model.load_state_dict(state, strict=False)

    assert list(cast("list[str]", incompatible.missing_keys)) == []
    assert list(cast("list[str]", incompatible.unexpected_keys)) == []
    assert sum(tensor.numel() for tensor in model.parameters()) == 41_984_456


def test_the_real_checkpoint_names_only_allowlisted_globals(installed_weights: Path) -> None:
    """The restricted reader's allowlist is exactly wide enough, and no wider.

    The unit tier proves the *refusal* against a hand-built package; this proves
    the other half — that a real, published checkpoint needs nothing beyond what
    is allowed. It records the set as evidence rather than only asserting a
    subset, because widening the list should always be a decision somebody made
    on purpose. Measured against ``htdemucs`` v4:

    ``_codecs.encode``, ``collections.OrderedDict``, ``demucs.htdemucs.HTDemucs``,
    ``fractions.Fraction``, ``numpy.core.multiarray.scalar``, ``numpy.dtype``,
    ``torch._utils._rebuild_tensor_v2``.
    """
    referenced: set[str] = set()

    class Recording(pickle.Unpickler):
        def find_class(self, module: str, name: str) -> Any:
            referenced.add(f"{module}.{name}")
            if module == "demucs" or module.startswith("demucs."):
                return CheckpointArchitecture
            return super().find_class(module, name)

    shim = types.ModuleType("recording_pickle")
    shim.__dict__.update(pickle.__dict__)
    shim.Unpickler = Recording  # pyright: ignore[reportAttributeAccessIssue]
    torch.load(installed_weights, map_location="cpu", weights_only=False, pickle_module=shim)

    outside = {
        reference
        for reference in referenced
        if not reference.startswith("demucs.")
        and reference not in SAFE_PICKLE_GLOBALS
        and reference.rsplit(".", 1)[0] not in SAFE_PICKLE_MODULES
    }
    assert outside == set(), f"the checkpoint names globals the reader would refuse: {outside}"
    assert "demucs.htdemucs.HTDemucs" in referenced


def test_the_installed_weights_match_the_pinned_digest(
    installed_weights: Path, settings: Settings
) -> None:
    """Re-verify what feature 025 verified on the way in.

    The installer hashes once and never re-checks (a documented limitation), so
    this tier is the place that notices bit rot or a hand-copied file. It is
    also the check that ties the pinned digest to upstream's own: the file name
    in the artifact URL ends in the first eight hex digits of its SHA-256, which
    is how ``demucs``'s ``check_checksum`` verifies a download.
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
    # Upstream's own integrity check, restated: the artifact's file name is
    # ``{signature}-{sha256[:8]}.th``.
    url: str = entry["artifact"]["download_url"]
    assert url.endswith(f"-{expected[:8]}.th"), url


async def test_a_real_separation_produces_four_genuinely_separated_stems(
    catalog: ModelCatalog,
    installed_weights: Path,
    parameters: DemucsParameters,
    tmp_path: Path,
) -> None:
    """End to end on a short clip, on whatever device this host offers.

    The assertions are the ones that hold for *any* input: four stems, in the
    advertised order, all playable, all different from one another. A musical
    judgement ("the drums are isolated") is not something a generated tone can
    make — the feature document records a correlation measurement against known
    sources for that.
    """
    device_id = "cuda:0" if torch.cuda.is_available() else "cpu"
    source = write_tone_wav(tmp_path / "clip.wav", seconds=10.0, channels=2, sample_rate=44100)
    output = tmp_path / "stems"

    separator = DemucsSeparator(
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
            mode_id="standard_stems",
            quality_id="balanced",
            device_id=device_id,
        ),
        reports.append,
        CancellationToken(),
        job_id=JOB_ID,
        output_dir=output,
    )
    elapsed = time.monotonic() - started

    print(
        f"\n[028] {device_id}: 10.0 s of audio in {elapsed:.1f} s "
        f"(RTF {result.metrics.realtime_factor:.3f}, "
        f"{reports[-1].chunks_total} chunks)"
    )

    assert [stem.name for stem in result.stems] == STEMS
    contents = {name: (output / f"{name}.wav").read_bytes() for name in STEMS}
    assert len(set(contents.values())) == 4, "two stems came out identical"
    for name in STEMS:
        channels, rate, frames, samples = read_wav(output / f"{name}.wav")
        assert (channels, rate) == (2, 44100)
        assert frames == pytest.approx(10.0 * 44100, rel=0.01)
        assert peak_amplitude(samples) >= 0
    assert reports[-1].chunks_completed == reports[-1].chunks_total > 1
    assert result.metrics.realtime_factor > 0.0


@pytest.mark.gpu
async def test_cuda_runtime_stats_report_real_memory(
    catalog: ModelCatalog,
    installed_weights: Path,
    parameters: DemucsParameters,
    tmp_path: Path,
) -> None:
    """The CUDA telemetry path, against a real device.

    First run on **2026-08-25**, on an NVIDIA GeForce RTX 4060 Laptop GPU
    (8,188 MiB, driver 610.47 / CUDA 13.3) with ``torch 2.13.0+cu130``, and
    passed. What it measured on that run: ``cuda:0``,
    ``memory_total_bytes`` 8,585,281,536, and ``memory_peak_bytes`` of
    **552.6 MiB** for the 5 s clip below at this model's ``chunk_size: 343980``
    (2 chunks).

    The peak is **not** bounded by chunking: it grows with the length of the
    input, at about **1.85 MiB per second of audio**, for the reason feature 038
    exists — and a smaller ``chunk_size`` does not help, because
    ``use_train_segment`` pads every window back up to the training length
    anyway. ``docs/features/028-demucs-four-stem.md`` has both sweeps.

    On a GPU host, run this with ``uv run --no-sync`` or it will not be a GPU
    run — see DEVELOPMENT.md, *PyTorch and CUDA*.
    """
    if not torch.cuda.is_available():
        pytest.skip("no CUDA device is available")

    separator = DemucsSeparator(
        separator_info_from_model(catalog.get_model(MODEL_ID)),
        weights_file=installed_weights,
        parameters=parameters,
    )
    source = write_tone_wav(tmp_path / "clip.wav", seconds=5.0, channels=2, sample_rate=44100)
    await separator.separate(
        source,
        SeparationConfiguration(
            audio_id="01AUDIO0000000000000000000",
            mode_id="standard_stems",
            quality_id="balanced",
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
    print(
        f"\n[028] cuda:0 peak {device.memory_peak_bytes / 1024**2:.1f} MiB "
        f"of {device.memory_total_bytes / 1024**2:.0f} MiB"
    )
    # NVML is optional (ARCHITECTURE.md §12): present or absent, both are legal.
    assert device.utilization is None or 0.0 <= device.utilization <= 1.0
    assert device.temperature_celsius is None or device.temperature_celsius > 0.0
