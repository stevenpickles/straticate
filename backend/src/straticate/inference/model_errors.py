"""The three failures a real inference backend meets before it can run at all.

Feature 039. A backend that loads published weights into vendored architecture
code can fail in exactly three ways that are worth telling somebody about, and
each has an envelope that reviews have already settled:

``model_weights_missing`` (409)
    Catalogued, not installed. **The only one a user can act on** — feature
    025's installer is where they act — so it is checked before anything else,
    and because ``SeparatorRegistry.aget`` is awaited inside ``POST /jobs``, it
    is the status that request answers with.
``model_weights_invalid`` (500)
    Installed, but not loadable into this architecture. A deployment fault.
``model_parameters_invalid`` (500)
    The catalog entry itself cannot be run as configured. Also a deployment
    fault: nobody typed it but a maintainer.

They are here, once, because they were written twice and must stay
byte-identical across backends — the API contract does not vary by architecture,
and an envelope that drifts is a client that has to branch on which model it
asked for. Torch-free on purpose: this is about a catalog entry and a file on
disk, and nothing here needs to know what a tensor is.
"""

from __future__ import annotations

from pathlib import Path

from straticate.errors import ApplicationError
from straticate.inference.base import SeparatorInfo


def require_installed_weights(info: SeparatorInfo, weights_file: Path) -> None:
    """Fail with ``model_weights_missing`` (409) when nothing is installed yet.

    Called before anything else a separator checks: this is the one failure a
    *user* can fix, and reporting a manifest fault instead of a missing download
    would send them somewhere they cannot go.
    """
    if not weights_file.is_file():
        raise ApplicationError(
            "model_weights_missing",
            f"Model {info.model_id!r} is catalogued but its weights are not installed.",
            status_code=409,
            detail={"model_id": info.model_id},
        )


def weights_not_loadable(model_id: str, reason: str) -> ApplicationError:
    """The ``model_weights_invalid`` (500) for "installed, but not this network".

    Args:
        model_id: For the envelope.
        reason: A *type name*, never a raw exception message — the detail block
            reaches a client, and a torch traceback is not something to publish.
    """
    return ApplicationError(
        "model_weights_invalid",
        (
            f"The installed weights for model {model_id!r} could not be loaded "
            f"into its architecture."
        ),
        status_code=500,
        detail={"model_id": model_id, "reason": reason},
    )


def parameters_invalid(model_id: str, reason: str) -> ApplicationError:
    """The one error for "this catalog entry cannot be run as configured"."""
    return ApplicationError(
        "model_parameters_invalid",
        f"Model {model_id!r} has unusable inference parameters: {reason}.",
        status_code=500,
        detail={"model_id": model_id, "reason": reason},
    )


def positive_int(value: object, model_id: str) -> int:
    """Coerce a catalog number to a positive int, or fail loudly.

    ``bool`` is rejected explicitly: it is an ``int`` in Python, and a catalog
    that says ``"chunk_size": true`` is a typo, not a chunk size of one.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise parameters_invalid(model_id, f"expected a positive integer, got {value!r}")
    return value


__all__ = [
    "parameters_invalid",
    "positive_int",
    "require_installed_weights",
    "weights_not_loadable",
]
