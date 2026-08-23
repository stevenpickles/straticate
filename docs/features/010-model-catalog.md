# [010] Model catalog + capabilities backend

Branch: `010-model-catalog`
Status: PR OPEN
Dependencies: 005
PR: #…

## Objective

The backend loads `models/catalog.json` at startup and serves it, deriving the
user-facing separation modes, stem lists, and quality tiers from model
capabilities — so the frontend (011) renders separation choices from backend
data instead of hardcoding stems or architectures.

## Scope

- `backend/src/straticate/models/` — the model *catalog* package (not to be
  confused with `schemas/models.py`, the API contract):
  - `catalog.py`: `ModelCatalog` — loads and fully validates the catalog file,
    `list_models()`, `get_model()`, `list_separation_modes()`; `ModelCatalogError`
    for a missing/malformed/inconsistent catalog.
- `backend/src/straticate/api/models.py` — `GET /models`, `GET /models/{model_id}`,
  `GET /separation-modes`, plus the `get_model_catalog` dependency accessor.
- `Settings.models_dir` (`STRATICATE_MODELS_DIR`), defaulting to the
  repository's `models/` directory.
- Catalog wired into `create_app()` as `app.state.model_catalog`.
- Shared-contract change: optional `quality_tier` on the model manifest
  (`models/schemas/model-manifest.schema.json`) and on
  `straticate.schemas.Model`, with the new `QualityTier` enum.

## Out of scope

- Model downloads, SHA-256 verification, install/update/remove (025).
- Real model weights, PyTorch, any separator implementation (014, 026).
- Job creation and mode/quality → model resolution at job time (015).
- Frontend consumption of these endpoints (011).

## Expected modules/files

- `backend/src/straticate/models/__init__.py`, `catalog.py`
- `backend/src/straticate/api/models.py`
- `backend/src/straticate/schemas/models.py` (added `QualityTier`,
  `Model.quality_tier`)
- `backend/src/straticate/config.py` (added `models_dir`)
- `backend/src/straticate/main.py` (catalog construction + router registration)
- `models/catalog.json`, `models/schemas/model-manifest.schema.json`
- `backend/tests/test_model_catalog.py`, `backend/tests/test_models_api.py`

## Acceptance criteria

- [x] The catalog is loaded and validated once, at application startup; a
      malformed catalog raises `ModelCatalogError` naming the file and the
      offending field, instead of serving an empty list.
- [x] `GET /models` and `GET /models/{model_id}` serve catalog entries; an
      unknown ID returns 404 `model_not_found` in the standard envelope.
- [x] `GET /separation-modes` is derived from model capabilities: `vocals` →
      2 stems, `standard_stems` → 4 stems, each with a non-empty
      `quality_options` list.
- [x] Architecture-specific manifest fields never reach the API.
- [x] No mode, stem list, tier, or display label is hardcoded in application
      code.
- [x] `ruff format --check`, `ruff check`, `pyright` (strict), `pytest` green.

## Required tests

- `test_model_catalog.py`: real catalog loads; missing file / malformed JSON /
  invalid entry / missing required field / duplicate model ID all fail loudly;
  manifest-only fields dropped on load; `get_model` 404; modes derived from the
  two fake models; single untiered model → one `balanced` option; three models
  in one mode → options ordered `fast → balanced → high_quality`; humanized vs.
  catalog-supplied mode labels; inconsistent stems in a mode fail loudly;
  duplicate tier within a mode fails loudly; the same tier in different modes is
  fine.
- `test_models_api.py`: all three endpoints through `httpx.ASGITransport`,
  asserting the documented JSON shapes and the 404 envelope; a synthetic catalog
  (tmp_path + `Settings(models_dir=...)`) proves `default_inference_parameters`
  is exposed by neither `/models` nor `/separation-modes`.

## Notes / decisions

### Quality-tier mapping

A model manifest may declare an optional `quality_tier`
(`fast` | `balanced` | `high_quality`). The catalog maps a mode's models onto
user-facing options with three rules:

1. **A missing tier means `balanced`.** The two fake dev models declare no tier,
   so each mode still yields exactly one sensible option ("Balanced") rather
   than an empty list.
2. **A tier is unique within a separation mode.** `quality_id` in a job request
   selects a tier, so a tier must identify exactly one model. Two models of one
   mode claiming the same tier (including two untiered models both defaulting to
   `balanced`) is a catalog error and fails at startup. The same tier in
   *different* modes is fine.
3. **Order is the `QualityTier` declaration order** (`fast → balanced →
   high_quality`), so the enum itself is the ordering table — there is no second
   list to keep in sync.

Alternatives rejected: inferring tiers from `requirements.recommended_vram_mb`
(fragile, and meaningless for the 0-VRAM fake models) and hardcoding a
model-ID → tier map in application code (couples code to catalog contents).

### Display names

No label dictionary exists in application code.

- **Tier labels** are humanized tier IDs: `high_quality` → "High Quality". The
  tier set is a closed enum, so the label is fully derivable.
- **Mode labels** come from the catalog file's optional `separation_modes`
  table, since modes are an *open* set that the catalog owns:

  ```json
  { "catalog_version": 1,
    "separation_modes": { "vocals": { "display_name": "Vocal Isolation" } },
    "models": [ … ] }
  ```

  A mode with no entry falls back to the humanized ID (`standard_stems` →
  "Standard Stems"), so the table never has to be exhaustive. Adding a model in
  a new mode therefore needs no backend change; giving that mode a nicer label
  is a data edit.

### `models_dir` resolution

`Settings.models_dir` defaults to the repository's `models/` directory, resolved
from `config.py`'s own location (three parents up) so the server can be started
from any working directory. Installed non-editably, that path will not exist and
`STRATICATE_MODELS_DIR` must point at the catalog.

### Loading is fatal by design

`ModelCatalog.from_directory` is called in `create_app()` (not in the lifespan
block, which feature 013 owns), so an invalid catalog stops the process at
startup with a message naming the file and the problem. Serving a half-valid
catalog would silently strip separation choices from the UI.

### What feature 025 (model download manager) will add

The catalog already tolerates the manifest fields 025 needs (`artifact` with
`download_url` / `size_bytes` / `sha256`, and `licensing`): they are accepted
and dropped on load because they are not part of the API-facing `Model`. 025 is
expected to:

- add an installation-state concept (`available` / `downloading` / `installed`)
  and expose it on `Model` or a sibling resource, so the UI can distinguish a
  model that exists in the catalog from one whose weights are on disk;
- add install/update/remove endpoints and the download → SHA-256 verify →
  atomic-rename pipeline writing under `Settings.models_dir`;
- decide whether a mode's `quality_options` should hide tiers whose weights are
  not installed yet. Today every catalogued model is offered, which is correct
  while only built-in fake models exist.

### Known limitations

- `frontend/src/api/generated/api.d.ts` is not regenerated in this PR (frontend
  is out of scope, and regenerating would conflict with parallel frontend
  branches). The next frontend feature that touches the generated types must
  re-run `npm run generate:api` to pick up `quality_tier` / `QualityTier`.
- The catalog wrapper (`catalog_version`, `separation_modes`, `models`) is
  validated by a private pydantic model in `catalog.py` and documented here;
  `models/schemas/` still only carries the per-model manifest schema.
