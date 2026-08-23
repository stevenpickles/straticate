"""Model catalog endpoints: available models and derived separation modes.

``GET /separation-modes`` is what the frontend renders its separation choices
from: modes, stem lists, and quality tiers all come from the catalog, so the UI
never hardcodes stems or architectures (AGENTS.md principle 6).
"""

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request

from straticate.models import ModelCatalog
from straticate.schemas import Model, SeparationMode

router = APIRouter(tags=["models"])


def get_model_catalog(request: Request) -> ModelCatalog:
    """Dependency accessor for the application's :class:`ModelCatalog`."""
    return cast(ModelCatalog, request.app.state.model_catalog)


CatalogDep = Annotated[ModelCatalog, Depends(get_model_catalog)]


@router.get("/models")
async def list_models(catalog: CatalogDep) -> list[Model]:
    """List every logical model in the catalog, in catalog order."""
    return catalog.list_models()


@router.get("/models/{model_id}")
async def get_model(model_id: str, catalog: CatalogDep) -> Model:
    """Fetch one model; 404 ``model_not_found`` if the ID is unknown."""
    return catalog.get_model(model_id)


@router.get("/separation-modes")
async def list_separation_modes(catalog: CatalogDep) -> list[SeparationMode]:
    """List the separation modes derived from the catalog's model capabilities."""
    return catalog.list_separation_modes()
