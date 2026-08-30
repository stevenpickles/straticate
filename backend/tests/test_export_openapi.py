"""Tests for the OpenAPI export script."""

import json
from pathlib import Path
from typing import Any

from straticate.scripts.export_openapi import build_openapi_document, main

EXPECTED_COMPONENTS = {
    # Entities
    "ErrorInfo",
    "ErrorEnvelope",
    "HealthStatus",
    "VersionInfo",
    "AudioMetadata",
    "AudioFile",
    "StereoAnalysis",
    "ModelRequirements",
    "Model",
    "QualityOption",
    "SeparationMode",
    "ComputeDevice",
    "JobState",
    "SeparationConfiguration",
    "Stem",
    "SeparationResultMetrics",
    "SeparationResult",
    "Job",
    # WebSocket events
    "ModelInfo",
    "GpuMetrics",
    "ProcessingMetrics",
    "JobCreatedEvent",
    "JobStartedEvent",
    "JobStageChangedEvent",
    "JobProgressEvent",
    "RuntimeMetricsEvent",
    "JobCompletedEvent",
    "JobCancelledEvent",
    "JobFailedEvent",
    "WebSocketEvent",
}

EXPECTED_EVENT_TYPES = {
    "job_created",
    "job_started",
    "job_stage_changed",
    "job_progress",
    "runtime_metrics",
    "job_completed",
    "job_cancelled",
    "job_failed",
}


def _collect_refs(node: Any, refs: set[str]) -> None:
    """Recursively collect every ``$ref`` value in the document."""
    if isinstance(node, dict):
        for key, value in node.items():  # pyright: ignore[reportUnknownVariableType]
            if key == "$ref" and isinstance(value, str):
                refs.add(value)
            else:
                _collect_refs(value, refs)
    elif isinstance(node, list):
        for item in node:  # pyright: ignore[reportUnknownVariableType]
            _collect_refs(item, refs)


def test_export_script_writes_valid_json(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "openapi.json"
    main([str(output)])

    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["openapi"].startswith("3.")
    component_schemas = document["components"]["schemas"]
    assert set(component_schemas) >= EXPECTED_COMPONENTS


def test_websocket_union_has_discriminator_mapping() -> None:
    document = build_openapi_document()
    union = document["components"]["schemas"]["WebSocketEvent"]
    mapping: dict[str, str] = union["discriminator"]["mapping"]
    assert union["discriminator"]["propertyName"] == "type"
    assert set(mapping) == EXPECTED_EVENT_TYPES
    for target in mapping.values():
        name = target.rsplit("/", 1)[-1]
        assert name in document["components"]["schemas"]


def test_all_refs_resolve_within_components() -> None:
    document = build_openapi_document()
    component_schemas = document["components"]["schemas"]
    refs: set[str] = set()
    _collect_refs(document, refs)
    assert refs, "the document should contain schema references"
    for ref in refs:
        assert ref.startswith("#/components/schemas/"), ref
        assert ref.rsplit("/", 1)[-1] in component_schemas, ref


def test_route_schemas_are_preserved() -> None:
    document = build_openapi_document()
    health = document["paths"]["/api/v1/health"]["get"]["responses"]["200"]
    ref = health["content"]["application/json"]["schema"]["$ref"]
    assert ref == "#/components/schemas/HealthStatus"
