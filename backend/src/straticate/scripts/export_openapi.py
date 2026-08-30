"""Export the OpenAPI document, including all shared contract schemas.

FastAPI's generated document only contains schemas referenced by REST routes.
This script additionally injects every schema in ``straticate.schemas`` —
including the WebSocket event models, which no REST route returns — into
``components.schemas`` so the frontend can generate TypeScript types for the
complete contract (``openapi-typescript``).

Usage::

    uv run python -m straticate.scripts.export_openapi [output_path]

The default output path is ``backend/openapi.json`` (gitignored; regenerate on
demand).
"""

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter
from pydantic.json_schema import JsonSchemaMode, models_json_schema

from straticate import schemas
from straticate.main import create_app

_REF_TEMPLATE = "#/components/schemas/{model}"
_SCHEMA_MODE: JsonSchemaMode = "validation"

#: Root models injected into ``components.schemas``. Nested models (and the
#: ``JobState`` enum) are pulled in transitively via ``$defs``.
_ROOT_MODELS: tuple[type[BaseModel], ...] = (
    schemas.ErrorEnvelope,
    schemas.HealthStatus,
    schemas.VersionInfo,
    schemas.AudioFile,
    schemas.Model,
    schemas.SeparationMode,
    schemas.ComputeDevice,
    schemas.StorageReport,
    schemas.DiskUsageReport,
    schemas.Job,
)

#: Name under which the WebSocket event discriminated union is exported.
_WS_UNION_NAME = "WebSocketEvent"


def build_openapi_document() -> dict[str, Any]:
    """Build the complete OpenAPI document for the application.

    Returns:
        The FastAPI-generated document with all shared contract schemas
        (entities and WebSocket events) merged into ``components.schemas``.
        Schemas already registered by a route keep their route-generated
        definition.
    """
    document = create_app().openapi()
    components: dict[str, Any] = document.setdefault("components", {}).setdefault("schemas", {})

    _, definitions = models_json_schema(
        [(model, _SCHEMA_MODE) for model in _ROOT_MODELS],
        ref_template=_REF_TEMPLATE,
    )
    injected: dict[str, Any] = dict(definitions.get("$defs", {}))

    ws_schema = TypeAdapter(schemas.WebSocketEvent).json_schema(
        ref_template=_REF_TEMPLATE, mode=_SCHEMA_MODE
    )
    injected.update(ws_schema.pop("$defs", {}))
    injected[_WS_UNION_NAME] = ws_schema

    for name, schema in sorted(injected.items()):
        components.setdefault(name, schema)

    return document


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: write the OpenAPI document as JSON.

    Args:
        argv: Optional argument list (used by tests); defaults to
            ``sys.argv[1:]``.
    """
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=_default_output_path(),
        help="Output path for the JSON document (default: backend/openapi.json).",
    )
    args = parser.parse_args(argv)
    output: Path = args.output

    document = build_openapi_document()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote OpenAPI document to {output}")


def _default_output_path() -> Path:
    """Resolve ``backend/openapi.json`` relative to this file (cwd-independent)."""
    return Path(__file__).resolve().parents[3] / "openapi.json"


if __name__ == "__main__":
    main()
