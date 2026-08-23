"""Tests for the shared contract schemas (entities and WebSocket events)."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from straticate.schemas import (
    AudioFile,
    ComputeDevice,
    ErrorEnvelope,
    Job,
    JobCancelledEvent,
    JobCompletedEvent,
    JobCreatedEvent,
    JobFailedEvent,
    JobProgressEvent,
    JobStageChangedEvent,
    JobStartedEvent,
    JobState,
    Model,
    RuntimeMetricsEvent,
    SeparationMode,
    WebSocketEvent,
)

EVENT_ADAPTER: TypeAdapter[Any] = TypeAdapter(WebSocketEvent)

AUDIO_FILE_JSON: dict[str, Any] = {
    "id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
    "filename": "Midnight Train.flac",
    "size_bytes": 44771328,
    "uploaded_at": "2026-08-23T12:00:00Z",
    "metadata": {
        "duration_seconds": 227.4,
        "container": "flac",
        "codec": "flac",
        "channels": 2,
        "sample_rate_hz": 44100,
        "bit_depth": 24,
        "bit_rate_bps": 1411000,
    },
}

MODEL_JSON: dict[str, Any] = {
    "id": "vocals-hq-001",
    "display_name": "Vocals — High Quality",
    "architecture": "mel_band_roformer",
    "version": "1.0",
    "separation_mode": "vocals",
    "stems": ["vocals", "instrumental"],
    "sample_rate": 44100,
    "requirements": {"recommended_vram_mb": 8192},
    "capabilities": {"cuda": True, "cpu": True},
}

SEPARATION_MODE_JSON: dict[str, Any] = {
    "id": "vocals",
    "display_name": "Vocal Isolation",
    "stems": ["vocals", "instrumental"],
    "quality_options": [
        {"id": "fast", "display_name": "Fast", "model_id": "vocals-fast-001"},
        {"id": "high_quality", "display_name": "High Quality", "model_id": "vocals-hq-001"},
    ],
}

COMPUTE_DEVICE_JSON: dict[str, Any] = {
    "id": "cuda:0",
    "backend": "cuda",
    "name": "NVIDIA GeForce RTX 5090",
    "memory_total_bytes": 34359738368,
}

RESULT_JSON: dict[str, Any] = {
    "job_id": "01BX5ZZKBKACTAV9WEVGEMMVRZ",
    "model_id": "vocals-hq-001",
    "stems": [
        {"name": "vocals", "duration_seconds": 227.4, "sample_rate_hz": 44100, "channels": 2},
        {
            "name": "instrumental",
            "duration_seconds": 227.4,
            "sample_rate_hz": 44100,
            "channels": 2,
        },
    ],
    "metrics": {"processing_seconds": 29.0, "realtime_factor": 7.83},
}

JOB_JSON: dict[str, Any] = {
    "id": "01BX5ZZKBKACTAV9WEVGEMMVRZ",
    "audio_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
    "configuration": {
        "audio_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "mode_id": "vocals",
        "quality_id": "high_quality",
        "device_id": "cuda:0",
    },
    "model_id": "vocals-hq-001",
    "state": "separating",
    "progress": 0.65,
    "created_at": "2026-08-23T12:00:00Z",
    "started_at": "2026-08-23T12:00:05Z",
    "finished_at": None,
    "error": None,
    "result": None,
}

ERROR_ENVELOPE_JSON: dict[str, Any] = {
    "error": {
        "code": "audio_not_decodable",
        "message": "The uploaded file could not be decoded as audio.",
        "detail": {},
    }
}


class TestRoundTrips:
    """model → JSON → model round-trips for representative entities."""

    @pytest.mark.parametrize(
        ("model_type", "payload"),
        [
            (AudioFile, AUDIO_FILE_JSON),
            (Model, MODEL_JSON),
            (SeparationMode, SEPARATION_MODE_JSON),
            (ComputeDevice, COMPUTE_DEVICE_JSON),
            (Job, JOB_JSON),
            (ErrorEnvelope, ERROR_ENVELOPE_JSON),
        ],
        ids=["audio_file", "model", "separation_mode", "compute_device", "job", "error_envelope"],
    )
    def test_json_round_trip(self, model_type: type[BaseModel], payload: dict[str, Any]) -> None:
        instance = model_type.model_validate(payload)
        assert model_type.model_validate_json(instance.model_dump_json()) == instance
        assert model_type.model_validate(instance.model_dump(mode="json")) == instance

    def test_datetimes_serialize_as_iso8601_utc(self) -> None:
        audio = AudioFile.model_validate(AUDIO_FILE_JSON)
        assert audio.uploaded_at == datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
        dumped = audio.model_dump(mode="json")
        assert dumped["uploaded_at"] == "2026-08-23T12:00:00Z"

    def test_naive_datetime_is_rejected(self) -> None:
        payload = {**AUDIO_FILE_JSON, "uploaded_at": "2026-08-23T12:00:00"}
        with pytest.raises(ValidationError):
            AudioFile.model_validate(payload)

    def test_job_progress_out_of_range_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Job.model_validate({**JOB_JSON, "progress": 1.5})
        with pytest.raises(ValidationError):
            Job.model_validate({**JOB_JSON, "progress": -0.1})


class TestJobState:
    """Terminal/non-terminal helper on the job state machine."""

    def test_terminal_states(self) -> None:
        terminal = {state for state in JobState if state.is_terminal}
        assert terminal == {JobState.COMPLETED, JobState.CANCELLED, JobState.FAILED}

    def test_non_terminal_states(self) -> None:
        for state in (
            JobState.QUEUED,
            JobState.PREPARING,
            JobState.DECODING,
            JobState.LOADING_MODEL,
            JobState.SEPARATING,
            JobState.POST_PROCESSING,
            JobState.ENCODING,
        ):
            assert not state.is_terminal

    def test_values_are_plain_strings(self) -> None:
        assert JobState.LOADING_MODEL == "loading_model"


# Documented examples from docs/contracts/websocket-events.md (the abridged
# `job` / `result` placeholders replaced with full valid objects).
EVENT_EXAMPLES: list[tuple[dict[str, Any], type[BaseModel]]] = [
    (
        {"type": "job_created", "job_id": JOB_JSON["id"], "job": JOB_JSON},
        JobCreatedEvent,
    ),
    (
        {
            "type": "job_started",
            "job_id": JOB_JSON["id"],
            "started_at": "2026-08-23T12:00:05Z",
        },
        JobStartedEvent,
    ),
    (
        {
            "type": "job_stage_changed",
            "job_id": JOB_JSON["id"],
            "stage": "separating",
            "previous_stage": "loading_model",
        },
        JobStageChangedEvent,
    ),
    (
        {
            "type": "job_progress",
            "job_id": JOB_JSON["id"],
            "stage": "separating",
            "progress": 0.65,
            "chunks_completed": 31,
            "chunks_total": 48,
            "elapsed_seconds": 18.2,
            "audio_processed_seconds": 148.0,
            "audio_total_seconds": 227.4,
        },
        JobProgressEvent,
    ),
    (
        {
            "type": "runtime_metrics",
            "job_id": JOB_JSON["id"],
            "model": {
                "id": "vocals-hq-001",
                "display_name": "Vocals — High Quality",
                "architecture": "mel_band_roformer",
                "version": "1.0",
                "separation_mode": "vocals",
                "stem_count": 2,
            },
            "gpu": {
                "device_id": "cuda:0",
                "name": "NVIDIA GeForce RTX 5090",
                "backend": "cuda",
                "memory_allocated_bytes": 9234179686,
                "memory_peak_bytes": 10133099161,
                "memory_total_bytes": 34359738368,
                "utilization": 0.91,
                "temperature_celsius": 63,
            },
            "processing": {
                "stage": "separating",
                "chunks_completed": 31,
                "chunks_total": 48,
                "elapsed_seconds": 18.2,
                "audio_processed_seconds": 148.0,
                "realtime_factor": 7.9,
            },
        },
        RuntimeMetricsEvent,
    ),
    (
        {"type": "job_completed", "job_id": JOB_JSON["id"], "result": RESULT_JSON},
        JobCompletedEvent,
    ),
    (
        {
            "type": "job_cancelled",
            "job_id": JOB_JSON["id"],
            "stage_at_cancellation": "separating",
        },
        JobCancelledEvent,
    ),
    (
        {
            "type": "job_failed",
            "job_id": JOB_JSON["id"],
            "error": {"code": "cuda_out_of_memory", "message": "CUDA out of memory.", "detail": {}},
        },
        JobFailedEvent,
    ),
]


class TestWebSocketEventUnion:
    """Discriminated union parsing of every documented event type."""

    @pytest.mark.parametrize(
        ("payload", "expected_type"),
        EVENT_EXAMPLES,
        ids=[str(payload["type"]) for payload, _ in EVENT_EXAMPLES],
    )
    def test_parses_documented_example(
        self, payload: dict[str, Any], expected_type: type[BaseModel]
    ) -> None:
        event = EVENT_ADAPTER.validate_python(payload)
        assert type(event) is expected_type
        # Round-trip through JSON preserves the payload semantics.
        assert EVENT_ADAPTER.validate_json(EVENT_ADAPTER.dump_json(event)) == event

    def test_unknown_type_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EVENT_ADAPTER.validate_python({"type": "totally_unknown", "job_id": "01X"})

    def test_missing_type_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EVENT_ADAPTER.validate_python({"job_id": "01X"})

    def test_invalid_payload_for_known_type_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EVENT_ADAPTER.validate_python(
                {"type": "job_progress", "job_id": "01X", "stage": "separating", "progress": 2.0}
            )

    def test_gpu_block_may_be_null(self) -> None:
        payload, _ = EVENT_EXAMPLES[4]
        event = EVENT_ADAPTER.validate_python({**payload, "gpu": None})
        assert isinstance(event, RuntimeMetricsEvent)
        assert event.gpu is None
