# [032] Keep development models out of the user-facing catalog

Branch: `032-hide-development-models`
Status: PR OPEN
Dependencies: 010, 026
PR: #36

## Objective

Development fixtures — the fake separator's catalog entries — no longer appear
anywhere in the user-facing API. A fresh checkout with default settings cannot
see, select, install or run one, while CI, the backend suite and the Playwright
end-to-end tier turn them back on with a single environment variable and get
exactly the behaviour that existed before this feature.

## The defect

Against `dev` at 4ebb659:

```text
mode='vocals' stems=['vocals', 'instrumental']
    tier='balanced'      model='fake-vocals-001'
    tier='high_quality'  model='vocals-hq-001'
mode='standard_stems' stems=['vocals', 'drums', 'bass', 'other']
    tier='balanced'      model='fake-standard-001'
```

`fake-vocals-001` is feature 014's development fixture: a feed-forward comb
filter that ARCHITECTURE.md §8 describes as "not separation and never pretends
to be". Feature 026 added a real model to the same separation mode, and the
fixture — untiered, therefore `balanced` — sorted *first*. Feature 011's UI
preselects a mode's first quality option, so a user who uploaded a file and
pressed **Start separation** without changing anything got comb filtering:
plausible-sounding, audibly wrong, labelled as an ordinary quality tier, with
nothing anywhere saying it was a fixture. `standard_stems` was entirely fake and
was offered as a real separation mode.

Nobody owned this. 010 established tiers, 014 added the fixtures, 026 added the
first real model; each was individually correct.

## Scope

- `models/schemas/model-manifest.schema.json` — new optional `development_only`
  boolean (default `false`).
- `models/catalog.json` — the two fake entries declare it.
- `backend/src/straticate/schemas/models.py` — `Model.development_only`
  (contract change; `api.d.ts` regenerated).
- `backend/src/straticate/config.py` — `Settings.include_development_models`
  (`STRATICATE_INCLUDE_DEVELOPMENT_MODELS`), default `False`.
- `backend/src/straticate/models/catalog.py` — the filter, applied once at
  construction; `ModelCatalog(..., include_development=…)`, `from_directory`,
  `from_file`.
- `backend/src/straticate/main.py` — `create_app` passes the setting through.
- `backend/tests/conftest.py` — session-wide fixture flipping the setting on.
- `ARCHITECTURE.md` §8/§9, `docs/contracts/rest-api.md`.

## Decisions

### The marker is a manifest field, not an architecture name

`architecture` is an open set that application code outside `inference/` must
not branch on (AGENTS.md principle 1, ARCHITECTURE.md §1). `development_only` is
an explicit, self-describing fact the catalog author states. A boolean rather
than an enum: there is no third state in sight, and defaulting to `false` means
a normal manifest entry needs no new field at all.

### The filter runs once, where the catalog is loaded

Not per route. `ModelCatalog` is the object every consumer already shares — the
model routes, the installer, and `POST /jobs`'s mode/quality resolution — so
filtering there makes "a user cannot select or run a fixture" a property of the
data rather than a rule four call sites must remember. Nothing outside
`config.py` and `catalog.py` learned a new concept.

The catalog is still **validated as written**, hidden entries included: whether
a fixture is served must never decide whether a malformed catalog is detected,
or CI (fixtures on) and a user's machine (fixtures off) would disagree about
which files load, and a fixture that broke its mode's stem agreement would sail
through every user's startup and fail only in CI.

### Both `/models` and `/separation-modes`

`/models` is arguably an inventory while `/separation-modes` is explicitly what
the frontend renders from — but Straticate is local-first, unauthenticated and
has one audience. An inventory that lists a comb filter `/separation-modes`
refuses to offer is a contradiction a client has to reconcile, and a fixture a
client can see is one it can offer to install. One predicate, one catalog, no
surface that disagrees with another.

### `GET /models/{hidden_id}` → `404 model_not_found`

No new error code. On a server that hides fixtures, the ID names nothing the
catalog contains, which is exactly what `model_not_found` means; the entry is
absent, not forbidden. A distinct "hidden" code would be a second condition
every client must handle and would advertise an entry the server chose not to
serve. Install and remove answer the same way, for the same reason.

### A job naming a hidden fixture → the existing 404s

`POST /jobs` selects a model by `mode_id` + `quality_id`, never by model ID, so
"requesting a hidden model" is exactly "requesting a tier that is not offered".
The answer is `quality_option_not_found` (404), or `separation_mode_not_found`
(404) when the whole mode was fixture-backed. Both already documented, both
true. Because the catalog is filtered, resolution cannot reach a hidden model at
all, so nothing downstream (registry, executor) needs a second check.

### `standard_stems` disappears

Its only model is a fixture and there is no real four-stem model until feature
028. A mode is derived exactly when at least one visible model serves it, so the
mode is simply not there — never served with an empty `quality_options` list,
which is a choice the frontend would render and nobody could act on. No fake
four-stem model was added to compensate.

### Both fixtures now declare an explicit `quality_tier` — read this before 028

`quality_tier` is optional and defaults to `balanced`. Both fixtures were
untiered, so both silently claimed `balanced` — **the tier a real model gets by
saying nothing**. Catalog validation deliberately reads every declared entry,
fixtures included (see above), so a hidden entry can still block a visible one:

> Feature 028 adds a real four-stem model to `standard_stems` without an
> explicit `quality_tier` → it collides with `fake-standard-001` on `balanced`
> → `ModelCatalog` raises at startup **for every user**, including the ones who
> never see fixtures and cannot see the entry that blocked them.

`fake-standard-001` therefore now declares `quality_tier: "fast"`, vacating the
default. `fake-vocals-001` declares `quality_tier: "balanced"` — explicitly,
because in `vocals` it is the only tier left: `high_quality` is `vocals-hq-001`
(026) and `fast` is what 027's MDX model will claim, so moving the fixture to
either would *guarantee* the collision rather than avoid it.

**Consequences for whoever specs 027 and 028:**

- **028** (four stems): `standard_stems` has `fast` taken by the fixture.
  Declare `balanced` or `high_quality`, or omit the field (which means
  `balanced`) — all three are free.
- **027** (fast vocals): `vocals` has no free tier. `fake-vocals-001` must be
  retiered or dropped from the catalog in that same PR. The failure is loud, at
  load, and names both model IDs — but it will happen.

Making the claims explicit does not remove the collision (nothing can, while
validation reads the whole file — and it must, or CI and a user's machine would
disagree about which catalogs load). It makes every fixture's claim visible in
the file, beside the entry a contributor is adding. A test pins that every
`development_only` entry declares its tier, and another pins that a hidden entry
colliding with a visible one still fails loudly.

### `development_only` is on `Model`, not on `QualityOption`

`GET /separation-modes` is therefore byte-identical to its pre-032 response when
the opt-in is on, so the frontend needs no change (feature 030 owns it right
now). A server that deliberately opted in can still label what it is showing,
from `GET /models`. Labelling a fixture *tier* in the UI belongs with a
model-management UI, which is unclaimed.

## Turning fixtures back on

```text
STRATICATE_INCLUDE_DEVELOPMENT_MODELS=1
```

`Settings.include_development_models`, default `False`. **Feature 030 needs this
in the environment of the backend process its Playwright tier launches**, since
that tier drives the real UI against the fake separator. CI jobs that exercise
the application without downloading real weights need it too. With it set, every
surface behaves exactly as it did before this feature.

The backend suite sets it once, for the whole session, in
`backend/tests/conftest.py` (`DEVELOPMENT_MODELS_ENV`) — as an environment
variable rather than a `create_app` argument, because the suite builds
applications along a dozen paths and every one of them reads `Settings`, which
reads the environment. Tests that assert the *default* behaviour opt out
explicitly with `Settings(include_development_models=False)`.

## Out of scope

- A model-management or attribution UI — **unclaimed**, and now slightly more
  wanted: it is where "this tier is a development fixture" would be labelled,
  and where "hide tiers whose weights are not installed" (open since 010) would
  be answered.
- Adding, removing or re-licensing any real model (027 is blocked on licensing,
  028 is not started).
- Any change to what the fake separator *does*. It is unchanged; it simply stops
  being offered.
- `frontend/**`, `.github/workflows/ci.yml`, `DEVELOPMENT.md` — feature 030 owns
  those. Only the generated `frontend/src/api/generated/api.d.ts` is touched, as
  the contract change requires.

## Expected modules/files

- `models/schemas/model-manifest.schema.json` · `models/catalog.json`
- `backend/src/straticate/config.py` · `models/catalog.py` · `main.py` ·
  `schemas/models.py` · `api/models.py` (docs) · `jobs/resolution.py` (docs)
- `backend/tests/conftest.py` · `test_model_catalog.py` · `test_models_api.py` ·
  `test_api_jobs.py` · `test_api_export.py` · `test_api_results.py` ·
  `test_inference_registry.py`
- `DEVELOPMENT.md` (the opt-in, beside the local run command)
- `frontend/src/api/generated/api.d.ts` (regenerated)
- `ARCHITECTURE.md` · `docs/contracts/rest-api.md` · `ROADMAP.md`

## Acceptance criteria

- [x] With default settings, `GET /separation-modes` offers no development
      fixture, and no mode is served with an empty quality-option list.
- [x] With the opt-in enabled, the set of models, modes and quality options
      served is identical to today's (`Model` gains one additive field).
- [x] No application code outside `inference/` branches on an architecture name.
- [x] A user with default settings and a fresh checkout cannot select or run a
      development fixture through the API.
- [x] Every pre-existing test passes, with the setting flipped in a fixture
      rather than the tests' intent rewritten.
- [x] `api.d.ts` regenerated; `rest-api.md` updated.
- [x] Backend gates green (`ruff format --check`, `ruff check`, `pyright`,
      `pytest`, and the suite clean under `-W error`); frontend gates green.

## Required tests

- `test_model_catalog.py`: the two shipped fixtures are marked in
  `models/catalog.json`, and *every* fake-architecture entry is marked, so a
  future fixture cannot be added unmarked; the repository catalog offers a user
  no fixture and loses `standard_stems`; no mode is ever served with an empty
  option list, in either state; the opt-in reproduces the pre-032 catalog; a
  hidden model is not a catalog key; hiding a fixture frees the tier it
  occupied; a mode with every model hidden is not derived; an all-fixture
  catalog loads and serves nothing; a hidden entry is still validated (stems,
  duplicate tier, duplicate ID); an unmarked entry defaults to visible; a hidden
  *untiered* entry blocks a visible untiered one and the error names both; every
  shipped fixture declares an explicit `quality_tier`.
- `test_models_api.py`: `/models`, `/models/{id}`, install, remove and
  `/separation-modes` in **both** settings states, including the 404 envelope
  for a hidden model and the unchanged `QualityOption` shape.
  Plus: a catalog whose every entry is a fixture starts and serves empty
  `/models` and `/separation-modes` lists — an empty *result* is not a startup
  failure, only an invalid catalog is.
- `test_api_jobs.py`: `POST /jobs` naming a hidden fixture's tier → 404
  `quality_option_not_found`; naming a fixture-only mode → 404
  `separation_mode_not_found`; the same request accepted with the opt-in on.

## Notes / decisions

**Clone-and-run no longer produces audio without a network fetch.** The only
visible model on a default server is `vocals-hq-001`, whose weights are a 913 MB
download; until they are installed via
`POST /api/v1/models/vocals-hq-001/install`, pressing **Start separation**
fails with `model_weights_missing` (409), and the frontend has no install
affordance because it never reads `installation`. This is a **conscious trade,
accepted at review**: an honest failure is better than silently serving
comb-filtered fixture audio as a real separation, which is what happened before
this feature. A follow-up feature is being allocated for the install affordance;
it belongs with the unclaimed model-management UI.

Contributors are not affected: `STRATICATE_INCLUDE_DEVELOPMENT_MODELS=1`
restores the fake separator and the full loop with no weights, no CUDA and no
network. That is now documented next to the local run command in
DEVELOPMENT.md.

**`GET /models` may briefly look empty of anything installable.** With fixtures
hidden, `list_models()` on a checkout whose weights are absent returns exactly
one model in state `available`. That is correct; it is also the strongest
argument yet for feature 027 (a fast tier) and 028 (four stems).
