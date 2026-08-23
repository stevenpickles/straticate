"""Shared primitives: the error envelope and simple system responses.

The error envelope is the single shape of every error response body::

    {"error": {"code": "...", "message": "...", "detail": {...}}}
"""

from typing import Any

from pydantic import BaseModel, Field


class ErrorInfo(BaseModel):
    """Machine-readable error information carried by every error response."""

    code: str = Field(description="Stable machine-readable error code (snake_case).")
    message: str = Field(description="Human-readable description of the failure.")
    detail: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional structured context for the error (empty object when absent).",
    )


class ErrorEnvelope(BaseModel):
    """Top-level shape of every error response body."""

    error: ErrorInfo


class HealthStatus(BaseModel):
    """Response of ``GET /api/v1/health``."""

    status: str = Field(description='Service liveness indicator; "ok" when healthy.')


class VersionInfo(BaseModel):
    """Response of ``GET /api/v1/version``."""

    version: str = Field(description="Version of the running backend application.")
