"""Model catalog endpoints: models, their installation state, separation modes.

``GET /separation-modes`` is what the frontend renders its separation choices
from: modes, stem lists, and quality tiers all come from the catalog, so the UI
never hardcodes stems or architectures (AGENTS.md principle 6).

``GET /models`` answers a second question the catalog alone cannot: whether a
model's **weights are on disk**. Being catalogued and being ready to run are
different facts, so every model here is served through
:meth:`~straticate.models.installer.ModelInstaller.describe`, which overlays the
live installation state on the catalog's static baseline. ``POST
/models/{model_id}/install`` starts a download and returns at once; ``DELETE
/models/{model_id}/weights`` puts the model back to ``available``.

**Which models a mode offers is unchanged.** ``GET /separation-modes`` still
lists every catalogued model's tier, installed or not. Feature 010 left open
whether uninstalled tiers should be hidden; that question only becomes real in
feature 026, when an uninstalled model can actually be selected for a job, and
it is better answered with that in hand than guessed now.
"""

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request

from straticate.models import ModelCatalog, ModelInstaller
from straticate.schemas import Model, SeparationMode

router = APIRouter(tags=["models"])


def get_model_catalog(request: Request) -> ModelCatalog:
    """Dependency accessor for the application's :class:`ModelCatalog`."""
    return cast(ModelCatalog, request.app.state.model_catalog)


def get_model_installer(request: Request) -> ModelInstaller:
    """Dependency accessor for the application's :class:`ModelInstaller`."""
    return cast(ModelInstaller, request.app.state.model_installer)


CatalogDep = Annotated[ModelCatalog, Depends(get_model_catalog)]
InstallerDep = Annotated[ModelInstaller, Depends(get_model_installer)]


@router.get("/models")
async def list_models(installer: InstallerDep) -> list[Model]:
    """List every logical model in the catalog, in catalog order.

    Served by the installer rather than the catalog directly: it holds the same
    catalog and adds each model's live ``installation`` block, so a client can
    tell an offered model from a ready one without a second request.
    """
    return installer.describe_all()


@router.get("/models/{model_id}")
async def get_model(model_id: str, catalog: CatalogDep, installer: InstallerDep) -> Model:
    """Fetch one model, including installation state and download progress.

    While an install runs, ``installation`` reports ``downloading`` with
    ``downloaded_bytes`` and ``progress``; this route is where that progress is
    read (see ``docs/features/025-model-download-manager.md`` for why it is not
    a WebSocket event). 404 ``model_not_found`` if the ID is unknown.
    """
    return installer.describe(catalog.get_entry(model_id))


@router.post("/models/{model_id}/install", status_code=202)
async def install_model(model_id: str, installer: InstallerDep) -> Model:
    """Start downloading a model's weights; return immediately.

    Answers ``202`` with the model in state ``downloading`` — the transfer is
    hundreds of megabytes and never holds the request open (AGENTS.md principle
    4). Progress and the final outcome are read from
    ``GET /models/{model_id}``; a failed install reports ``failed`` with the
    reason in ``installation.error``.

    Installing a model whose weights are already present is an idempotent no-op
    that reports ``installed``. Errors:
    ``model_not_found`` (404), ``model_not_downloadable`` (409) for a built-in
    model with no artifact, ``model_busy`` (409) when an install is already
    running for this model.
    """
    return installer.start_install(model_id)


@router.delete("/models/{model_id}/weights")
async def remove_model_weights(model_id: str, installer: InstallerDep) -> Model:
    """Delete a model's weights, returning it to ``available``.

    **A running install is cancelled first**, and this is also how a download
    that will not finish is escaped: the network bound is per-operation, not a
    total budget, so a trickling host could otherwise hold a model in
    ``downloading`` indefinitely. The cancelled download removes its own partial
    file before this responds.

    Idempotent: removing weights that are not installed succeeds. The updated
    model is returned rather than ``204`` so the caller sees the state it just
    produced, as ``POST /jobs/{job_id}/cancel`` does. Errors:
    ``model_not_found`` (404), ``model_not_downloadable`` (409) for a built-in
    model.
    """
    return await installer.remove(model_id)


@router.get("/separation-modes")
async def list_separation_modes(catalog: CatalogDep) -> list[SeparationMode]:
    """List the separation modes derived from the catalog's model capabilities."""
    return catalog.list_separation_modes()
