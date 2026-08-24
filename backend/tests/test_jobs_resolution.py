"""Tests for the pure job-request resolvers (audio, model, device).

These are deliberately HTTP-free: the resolution rules and the error codes they
produce are what feature 015's endpoint promises, and they are cheaper and
clearer to pin down here than through a client.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from straticate.audio import AudioStore
from straticate.errors import ApplicationError
from straticate.jobs import resolve_audio, resolve_device, resolve_model
from straticate.models import ModelCatalog
from straticate.schemas import (
    AudioFile,
    AudioMetadata,
    ComputeDevice,
    Model,
    QualityOption,
    SeparationMode,
)
from straticate.system import CPU_BACKEND, CPU_DEVICE_ID, CUDA_BACKEND, DeviceDetector

FAKE_GPU = ComputeDevice(
    id="cuda:0",
    backend=CUDA_BACKEND,
    name="NVIDIA GeForce RTX 5090",
    memory_total_bytes=34359738368,
)


class StaticProbe:
    """Probe reporting a fixed device list, standing in for real CUDA."""

    backend: str = CUDA_BACKEND

    def detect(self) -> list[ComputeDevice]:
        return [FAKE_GPU]


def make_catalog_model(model_id: str, **overrides: object) -> Model:
    """A minimal valid :class:`Model`, with ``overrides`` applied."""
    fields: dict[str, object] = {
        "id": model_id,
        "display_name": model_id,
        "architecture": "fake",
        "version": "1.0",
        "separation_mode": "vocals",
        "stems": ["vocals", "instrumental"],
        "sample_rate": 44100,
        "capabilities": {"cpu": True},
    }
    fields.update(overrides)
    return Model.model_validate(fields)


@pytest.fixture
def catalog() -> ModelCatalog:
    """Two modes, the first with two quality tiers."""
    return ModelCatalog(
        [
            make_catalog_model("vocals-fast", quality_tier="fast"),
            make_catalog_model("vocals-hq", quality_tier="high_quality"),
            make_catalog_model(
                "quad-001",
                separation_mode="standard_stems",
                stems=["vocals", "drums", "bass", "other"],
            ),
        ]
    )


def register_audio(store: AudioStore, *, filename: str = "song.wav") -> tuple[str, Path]:
    """Register an audio record with a real (empty) file on disk."""
    audio_id = store.new_id()
    path = store.prepare_original_path(audio_id, filename)
    path.write_bytes(b"not really audio, but it exists")
    store.register(
        AudioFile(
            id=audio_id,
            filename=filename,
            size_bytes=path.stat().st_size,
            uploaded_at=datetime.now(UTC),
            metadata=AudioMetadata(
                duration_seconds=1.0,
                container="wav",
                codec="pcm_s16le",
                channels=2,
                sample_rate_hz=44100,
                bit_depth=16,
                bit_rate_bps=1411000,
            ),
        )
    )
    return audio_id, path


# -- model resolution -------------------------------------------------------


def test_resolve_model_returns_the_model_behind_mode_and_tier(catalog: ModelCatalog) -> None:
    model = resolve_model(catalog, "vocals", "high_quality")
    assert model.id == "vocals-hq"
    assert model.separation_mode == "vocals"


def test_resolve_model_resolves_a_four_stem_mode(catalog: ModelCatalog) -> None:
    """Nothing in resolution is specific to a stem count or a mode name."""
    model = resolve_model(catalog, "standard_stems", "balanced")
    assert model.id == "quad-001"
    assert model.stems == ["vocals", "drums", "bass", "other"]


def test_resolve_model_rejects_an_unknown_mode(catalog: ModelCatalog) -> None:
    with pytest.raises(ApplicationError) as excinfo:
        resolve_model(catalog, "karaoke", "balanced")
    assert excinfo.value.code == "separation_mode_not_found"
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == {"mode_id": "karaoke"}


def test_resolve_model_rejects_a_tier_the_mode_does_not_offer(catalog: ModelCatalog) -> None:
    with pytest.raises(ApplicationError) as excinfo:
        resolve_model(catalog, "vocals", "balanced")
    assert excinfo.value.code == "quality_option_not_found"
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == {"mode_id": "vocals", "quality_id": "balanced"}


def test_resolve_model_propagates_a_dangling_model_reference() -> None:
    """A mode whose option names an absent model is the catalog's 404, not ours."""

    class DanglingCatalog(ModelCatalog):
        def list_separation_modes(self) -> list[SeparationMode]:
            return [
                SeparationMode(
                    id="vocals",
                    display_name="Vocals",
                    stems=["vocals", "instrumental"],
                    quality_options=[
                        QualityOption(id="fast", display_name="Fast", model_id="ghost-001")
                    ],
                )
            ]

    with pytest.raises(ApplicationError) as excinfo:
        resolve_model(DanglingCatalog([]), "vocals", "fast")
    assert excinfo.value.code == "model_not_found"
    assert excinfo.value.status_code == 404


# -- audio resolution -------------------------------------------------------


def test_resolve_audio_returns_the_record_and_its_source_path(tmp_path: Path) -> None:
    store = AudioStore(tmp_path)
    audio_id, path = register_audio(store, filename="Midnight Train.flac")

    record, source = resolve_audio(store, audio_id)

    assert record.id == audio_id
    assert record.filename == "Midnight Train.flac"
    assert source == path
    assert source.is_file()


def test_resolve_audio_rejects_an_unknown_id(tmp_path: Path) -> None:
    with pytest.raises(ApplicationError) as excinfo:
        resolve_audio(AudioStore(tmp_path), "01NOPE")
    assert excinfo.value.code == "audio_not_found"
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == {"audio_id": "01NOPE"}


def test_resolve_audio_rejects_a_registered_record_whose_file_is_gone(tmp_path: Path) -> None:
    store = AudioStore(tmp_path)
    audio_id, path = register_audio(store)
    path.unlink()

    with pytest.raises(ApplicationError) as excinfo:
        resolve_audio(store, audio_id)
    assert excinfo.value.code == "audio_not_found"
    assert excinfo.value.status_code == 404


def test_resolve_audio_creates_nothing_on_disk(tmp_path: Path) -> None:
    """The resolvers are documented as pure; a lookup must leave no trace.

    ``AudioStore.original_path`` used to ``mkdir`` unconditionally, so probing
    for audio that was not there recreated an empty
    ``{data_dir}/audio/{audio_id}/`` and *then* returned 404 — an orphan
    directory per failed lookup, on a read-only path.
    """
    store = AudioStore(tmp_path)
    audio_id, path = register_audio(store)
    path.unlink()
    path.parent.rmdir()
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))

    with pytest.raises(ApplicationError):
        resolve_audio(store, audio_id)
    with pytest.raises(ApplicationError):
        resolve_audio(store, "01NEVER-REGISTERED")

    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before


def test_original_path_is_pure_and_prepare_creates_the_directory(tmp_path: Path) -> None:
    store = AudioStore(tmp_path)
    audio_id = store.new_id()

    path = store.original_path(audio_id, "song.wav")
    assert not path.parent.exists()
    assert list(tmp_path.iterdir()) == []

    prepared = store.prepare_original_path(audio_id, "song.wav")
    assert prepared == path
    assert prepared.parent.is_dir()
    assert not prepared.exists(), "the directory is created, the file is not"


# -- device resolution ------------------------------------------------------


def test_resolve_device_honours_an_explicit_id() -> None:
    detector = DeviceDetector(probes=[StaticProbe()])
    assert resolve_device(detector, "cuda:0") == FAKE_GPU
    assert resolve_device(detector, CPU_DEVICE_ID).backend == CPU_BACKEND


def test_resolve_device_falls_back_to_the_default_selection() -> None:
    """``None`` means "let the backend pick" — the detector's preferred device."""
    assert resolve_device(DeviceDetector(probes=[StaticProbe()]), None) == FAKE_GPU
    assert resolve_device(DeviceDetector(probes=[]), None).backend == CPU_BACKEND


def test_resolve_device_rejects_an_unknown_id() -> None:
    with pytest.raises(ApplicationError) as excinfo:
        resolve_device(DeviceDetector(probes=[]), "cuda:7")
    assert excinfo.value.code == "device_not_found"
    assert excinfo.value.status_code == 404
