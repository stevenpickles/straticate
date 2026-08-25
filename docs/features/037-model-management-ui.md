# [037] Model management UI

Branch: `037-model-management-ui`
Status: PR OPEN
Dependencies: 025, 035
PR: #44

## Objective

A place in the product to see and manage models — every catalogued model with
its installation state, download size, hardware requirements and **licence** —
and to install, cancel and remove weights from the UI. Three features deferred
to it (025, 032, 035) and a fourth (027) is partly blocked on it.

## What was built

- **`frontend/src/licensing.ts`** — the rules for reading a `ModelLicensing`
  block honestly. Pure, tested, no React.
- **`frontend/src/components/ModelLicence.tsx`** — what those rules render:
  code licence, weights licence, commercial use, redistribution, attribution
  and the cautions each combination earns.
- **`frontend/src/components/ModelLibrary.tsx`** + **`ModelCard.tsx`** — the
  view: every catalogued model, its facts, its terms, and every action that
  can be taken on its weights.
- **`frontend/src/components/useModelCatalog.ts`** + `api/models.ts`'s new
  `listModels` — `GET /models`, read once.
- **`frontend/src/components/useModelInstallation.ts`** — feature 035's hook,
  **generalised** with `remove()` (which is also cancel), a shared per-model
  request guard, a poll that stops while a removal is in flight, and a
  regression fix for a guard that was never released.
- **`frontend/src/components/DiskCostNotice.tsx`** — what an install costs and
  what cannot be checked about it.
- **`frontend/src/components/InstallProgressBar.tsx`** + `installProgress.ts` —
  the download bar and its arithmetic, moved out of `ModelInstallPanel` so both
  places that show a download agree.
- **`frontend/src/components/SeparationOptions.tsx`** — every quality tier
  priced from the catalog, and the selected model's licence and attribution
  rendered where the model is chosen.
- **`Header.tsx`** / **`App.tsx`** — the way in and out of the library, and the
  focus that comes back with it.
- **`frontend/src/state/modelRevision.tsx`** — the one-shot signal that another
  view may have installed or removed something.
- **`frontend/e2e/models.spec.ts`**, with the shared model helpers lifted into
  `e2e/app.ts`.

## The backend audit: no new endpoint was needed

Checked before a line of UI was written, against
`backend/src/straticate/api/models.py` and `models/installer.py`:

| What the view needs | What already serves it |
| --- | --- |
| Enumerate every model | `GET /models` — the full `Model`, including `requirements`, `licensing` and the live `installation` block |
| One model's state and progress | `GET /models/{id}` |
| Install | `POST /models/{id}/install` (202, returns immediately) |
| Cancel a running install | `DELETE /models/{id}/weights` |
| Remove installed weights | `DELETE /models/{id}/weights` |
| Concurrent installs of *different* models | Supported: `ModelInstaller._running` is keyed by model ID, and `model_busy` is raised only for a second install of the **same** model |

Nothing was added to `backend/` and `frontend/src/api/generated/api.d.ts` is
untouched. **One genuine gap was found and is reported rather than
improvised:** there is no endpoint reporting free space beside `models_dir`.
See *Disk space* below — the UI states the gap instead of pretending.

## Licensing: the load-bearing part

`Model.licensing` had been served since feature 025 and rendered nowhere. It is
now rendered in two places: on every card in the library, and beside the
selected quality tier in the configure step. Five rules decide what it may say.

### 1. Code terms and weights terms are never folded together

They are separate rows, always. A model whose *code* is MIT may ship weights
under CC-BY-NC, or under a sentence on a model card that names no licence at
all — which is precisely why feature 027 is blocked. Collapsing them into one
"Licence: MIT" line would tell a user an 870 MB download is free to use on the
strength of a fact about the source code.

When both are declared and differ, a notice says so: *"The weights are licensed
separately from the code. The code licence says nothing about what you may do
with the download."*

When the weights licence is **absent**, the notice names the code licence
explicitly so the reader cannot borrow it: *"No weights licence is declared.
The code licence (MIT) does not cover the weights, so their terms are
unknown."*

That caution is about **a download whose terms nobody has stated**, so
`describeLicensing` is told whether the weights are downloaded at all
(`LicensingContext.weightsAreDownloaded`, defaulting to the risky reading). A
built-in separator fetches nothing from a third party and has no separate
weights to license: warning about its unstated weights terms would be inventing
a risk, and adding `"licensing": {"code_license": "MIT"}` to a development
fixture used to do exactly that. Everything else — a refusal, an informal
statement, a difference between the two licences — is as true of a built-in
model and is still raised.

### 2. Silence is rendered as silence

The contract says a `null` permission means "not declared", never "not
permitted" — and, just as importantly, never "permitted". So `commercial_use_
permitted` and `redistribution_permitted` render as **Permitted** / **Not
permitted** / **Not stated**, three states, never two. `PermissionState` in
`licensing.ts` exists so that no branch anywhere can accidentally treat `null`
as `false` or as `true`.

### 3. An informally stated licence is rendered in full and flagged

**How restrictive or informally-stated weights terms are rendered** — the
question the assignment asked to be reported on:

`licenceTerm()` classifies a declared licence as `named` only if it is a single
whitespace-free SPDX-shaped token of at most 48 characters. **SPDX identifiers
are whitespace-free by construction**, so anything containing a space is prose.
Prose is rendered **verbatim** — the whole sentence, not a truncation — with
the aside *"(stated in words, not as a named licence)"* beside it and a notice
saying *"The weights terms are stated in words rather than as a named licence.
Read them in full before installing."*

The rule is deliberately blunt, and deliberately biased. `"MIT License"` is
classified as informal, which is technically over-cautious; the cost is one
sentence a user reads unnecessarily. The opposite error — letting a paragraph
of conditions be presented as though it were a recognisable identifier — is the
one this feature exists to prevent. A heuristic that tried to be cleverer
("`Research use only` is three words, so it must be a name") would make exactly
that mistake on exactly the strings that matter.

A **restrictive** declaration is never softened: `commercial_use_permitted:
false` renders as **Not permitted** in danger styling *and* earns its own
notice (*"Commercial use of these weights is not permitted. They are cleared
for personal use only."*), and the same for redistribution. The card's badge
reads **Restricted use**.

### 4. Nothing is ever described as permissive

The badge has four values: **Restricted use**, **Terms not stated**, **Read the
terms**, **Terms declared**. The best case says only that the terms were
declared. This code does not read licence texts, so it must not label one free,
open or permissive — and a test asserts that those words never appear.

A stated refusal outranks silence for the badge, because "you may not" is a
fact to obey while "not stated" is a caution to investigate. Neither caution is
lost either way: the badge picks one word, the notice list keeps both.

### 5. "No attribution required" is a claim, and needs a licence to make it

`attribution: null` means "none required" *only* when a weights licence was
actually declared. With the weights terms unstated, the attribution row reads
**Not stated** — a model whose terms nobody has published may well require a
credit nobody has written down. (`attributionFallback()`.)

### Where attribution is shown

Both in the library **and beside the tier that is being chosen**, in the
configure step. A credit visible only in a settings corner is not a credit
given, and the project owner's CC-BY clearance makes attribution a binding
condition rather than a courtesy. The compact placement drops **nothing** —
same permissions, same notices, same attribution — because that placement is
the one that comes *before* the download.

The one place it is still not shown is the running job's telemetry panel
(feature 020), which renders a `ModelInfo` event payload carrying no licensing.
Noted under *Known limitations*.

## Cancel and remove: one request, two intents

`DELETE /models/{id}/weights` cancels a running install **and** deletes
installed weights. Feature 025 built it that way deliberately and wrote down
why: the outcome of cancelling is exactly "this model has no weights", and with
the network bound being per-operation rather than a total budget, it is the
only escape from a transfer that will not finish. That is a good API and a
terrible button, so the card never shows one ambiguous control:

| Model state | Control | What it says | Confirmation |
| --- | --- | --- | --- |
| `downloading` | **Cancel download** | "Cancelling stops the transfer and deletes the partly downloaded file. Nothing is kept, so installing again starts from the beginning." | **None** |
| `installed` | **Remove weights** | "Delete the 870 MB of weights for X? Separations using this model will not run until it is installed again, and installing again downloads the whole artifact." | **Required** |

The asymmetry is the point. Cancelling is one click because requiring a dialog
to escape a stuck download is hostile — the user is trying to *stop* something,
and 025 made this route the only way out. Removing asks first because it throws
away a download the user waited for, and names the size that would have to come
back over the network.

`Cancel download` and `Remove weights` are never on screen at the same time,
and neither is `Install` while a download is running (a second `POST /install`
could only earn `model_busy` for a transfer that is going perfectly well).

**A confirmation belongs to the installation it was asked about.** Both
branches that render the confirm group are gated on `installed`, so a flag left
standing once the model leaves that state is invisible rather than gone — and
comes back the instant it is installed again. The reachable sequence: open the
confirmation, tab away, let the weights be removed from somewhere else, come
back (the visibility re-read lands `available`, the group disappears), install
again — and the download settling would put "Delete the 870 MB of weights?" on
screen, unprompted, on the heels of a successful install. The card therefore
drops the confirmation whenever the model leaves `installed`, adjusted during
render rather than in an effect so there is no frame in which the stale
question is shown.

## Disk space

**Straticate cannot check it, and says so.** The notice beside every install
reads: *"870 MB will be written to the machine running Straticate. Straticate
cannot check that machine's free space from the browser, so make sure there is
room before installing."*

This is not laziness. The weights are written by the **backend**, on whatever
machine that is; the browser has no view of that filesystem. The one disk
figure a browser can obtain — `navigator.storage.estimate()` — describes the
quota of the *page's own origin* inside the *browser's* profile directory. It
is a different number about a different disk, and rendering it here would be
worse than silence, because it would look like an answer.

The honest fix is a backend endpoint reporting free space beside `models_dir`.
That is a backend change and out of scope for this feature, so it is
**reported, not improvised** (see *Reported out of scope*).

`LARGE_DOWNLOAD_BYTES` is 100 MB: above it the notice is styled as a warning
rather than a footnote, so a future catalog entry of a few megabytes is not
dressed up as a commitment. An artifact whose size the catalog does not publish
counts as large, because unknown is at least as risky as big.

## The hide-uninstalled-tiers question, answered

Raised by **010**, deferred by **025**, **026**, **032**, argued against by
**035**. Decided here.

> **No. A separation mode must never hide a quality tier because its weights
> are not installed.**

Five reasons, in the order they decided it:

1. **Hiding makes the product silently differ from machine to machine.** The
   catalog is a property of the build; what is on disk is a property of one
   installation. Deriving the offered tiers from the second means two users of
   the same version see different applications, and neither is told why.
2. **On a default server it would empty the screen.** Since 032, a fresh
   checkout offers exactly one mode with exactly one real tier, backed by an
   870 MB download. Hiding it yields a configure step with nothing in it, no
   explanation, and no way to discover that a model exists at all.
3. **The tier is the only place the price can be shown.** Download size,
   hardware requirements and licence are all decision inputs, and the moment
   before an install is the only one at which they can still change the
   decision — 025's stated reason for putting `licensing` on the resource.
   Hiding the tier removes the one affordance that could act on them.
4. **The failure hiding was meant to prevent is already prevented.** Feature
   035 made an uninstalled tier safe: `startBlockedReason` disables "Start
   separation" with a visible, announced sentence until the server has said the
   weights are there. A visible uninstalled tier can no longer produce the
   `model_weights_missing` surprise the question was originally about.
5. **This feature makes the alternative real.** "Show it" is only the better
   answer if the user can then act, and now they can: the library lists every
   model with its state, size, requirements and terms, and installs it.

**Implemented, not merely recorded.** Every tier is now priced *at the point of
choice*, from `GET /models` rather than from the selected tier's own read:

- `Needs a 870 MB download` · `Installed` · `Downloading its weights…` ·
  `Its last install failed`, and nothing at all for a model that needs no
  download.

The phrase sits outside the radio's `<label>` and is referenced with
`aria-describedby`, so the tier's own name stays its accessible name and the
cost is announced as a description. This also closes 035's own limitation —
"the panel describes only the *selected* tier's model. A catalog with several
uninstalled tiers would make a user select each in turn to see its size" —
which becomes real the moment 027 or 028 lands a second tier.

The annotation is an **enrichment, never a gate**: a failed catalog read leaves
the tiers exactly as feature 011 rendered them, with no annotation and nothing
blocked by its absence.

## Reusing 035's hook rather than forking it

The assignment's rule was: reuse, or generalise — do not copy.

**Reused, and generalised where the list needed more.** The library renders one
`<ModelCard>` per model and each card calls `useModelInstallation(model.id)`
for its own model. That is not a coincidence of shape: the hook was already
keyed by model ID and already held no assumption that the ID came from a
selected quality tier, so "a component per model" makes every rule it enforces
— the sequence-numbered responses, the per-model request guards, the 1 s poll
and each of its five exit conditions — apply per row for free. There is exactly
one implementation of the install machinery in the app.

Three additions, all of them shared:

- **`remove()`**, with the same shape as `install()`. `removeModelWeights` had
  been written and tested by 035 and never called; it is called now.
- **A shared guard.** `install()` and `remove()` check *both* in-flight refs,
  because an install and a removal would race over the same file on the server.
- **The poll stops while a removal is in flight.** Without it, a read the
  server issued before the cancel had unwound would land a `downloading` record
  a moment after the user pressed the button that stopped it, flicking the bar
  back on for an interval.

And one **regression fix** to 035's code, verified to fail against the pre-fix
version: a request guard is now released when its request settles, whatever is
selected by then. The `then`/`catch` handlers apply a *record*, so they are
rightly gated on the model still being the selected one — but a guard is not a
record. Leaving it set meant that an install which settled while the user was
looking at another tier left the first tier's button disabled and `aria-busy`
for ever, which is the mirror image of the hang `installingFor` was keyed by
model to prevent.

### Why the catalog is read separately, and never polled

`useModelCatalog` reads `GET /models` **once**. It is the inventory half of
model management and `useModelInstallation` is the live-state half — a split
025 designed for when it confined the mutable part of `Model` to the nested
`installation` object:

- a `Model` is a projection of its manifest and does not change while the
  process runs, so it is read once and kept;
- `installation` belongs to whoever is watching one particular model.

Re-reading the whole collection on a timer would duplicate the per-model watch,
cost a request a second whether or not anything was downloading, and put a
second implementation of "is it there yet" beside the one 035 got right. The
`installation` blocks the collection read *does* carry are still used: they are
what price an unselected quality tier, and what a library row shows for the one
round trip before its own first read answers, so a card is never blank and
never flashes.

### …and how it is kept in step without becoming one

Reading once is only honest if what is derived from that read is corrected when
the world moves. Three things do that, and none of them is a timer.

**The live record outranks the catalog's copy of the same model.** The
configure step already holds a polled record for the selected tier, so that is
what prices it. Without this the radio and the panel directly below it
contradicted each other the moment an install finished — "Needs a 870 MB
download" above "Model weights installed" — and, because that sentence is the
radio's `aria-describedby` target, a screen reader announced the tier as
needing a download that had just completed.

**A freshly-read record is folded back into the catalog** (`applyModel`).
Preferring the live record keeps the *selected* tier honest; folding keeps it
honest once the user selects a different one and it is no longer live. In the
library, each card hands its reads up the same way (`onModelRead`), which is
what stops the `role="status"` summary reporting "0 installed" above a card
saying "Installed — 870 MB on disk". It costs **no request**: the answer that
installed or removed the weights is the answer being written down, so there is
no window in which the two disagree. Only a change of `installation.state`
replaces an entry, so a running download's byte counts — which nothing derived
from the catalog reads — cannot re-render every consumer once a second.

**Closing the library bumps a revision** (`state/modelRevision.tsx`), and the
configure step re-reads once when it changes. This is the price of keeping the
workflow *hidden* rather than unmounted: it does not re-read on the way back
the way a remounted view would, so a model installed from a library card would
otherwise leave the configure step pricing a tier that is already on disk —
with "Start separation" disabled for weights that are there. The revision is a
**known event**: it fires when a user leaves a screen on which they could have
changed something, and never on its own.

## Where the library lives

Beside the workflow, not inside it. Managing model weights is not a step of
separating a file, so it is not a sixth phase; it is a view the header toggles.

Opening it **hides the workspace rather than unmounting it**
(`.app-workflow[hidden]`). That is load-bearing: unmounting would tear down the
stem player's Web Audio graph, lose a running job's rendered progress, and drop
the upload behind them. Hidden, the whole workflow is exactly where the user
left it when they come back, and `[hidden]` takes it out of the accessibility
tree meanwhile, so nothing behind the library is reachable by keyboard or
screen reader.

Whether the library is open is `useState` in `App`, deliberately **not** part of
`AppStateProvider`: it is not a phase, it is not persisted across a reload
(feature 033), and nothing in the workflow may branch on it.

Closing it from inside — the library's own "Back to workflow" button —
**returns focus to the header's models button** before the library unmounts. A
keyboard user who pressed it has conceptually gone back to the control they
came in through; unmounting a focused button drops focus on `<body>`, and their
next Tab restarts at the top of the document.

## Also closed here

- **Development fixtures are labelled.** A server running with
  `STRATICATE_INCLUDE_DEVELOPMENT_MODELS=1` sees its fixtures in the library,
  each carrying *"Development fixture — this entry exists to exercise the
  application and does not perform real separation. A default server does not
  offer it at all."* Feature 032 named this screen as where that belonged.
- **An "installs in flight" view.** Every downloading model shows its own bar
  in the library, which is what 035 asked for. It is still per-view, not
  global — see *Known limitations*.

## Acceptance criteria

- [x] Every catalogued model is listed with state, size, requirements and
      licensing.
- [x] Install, cancel and remove all work from the UI, and the cancel/remove
      duality is legible rather than a single ambiguous button.
- [x] Licensing — including restrictive and informally-stated weights terms —
      is visible before installing, and attribution is visible where a model is
      chosen or used.
- [x] Disk cost is stated before a large download, and its unknowability is
      stated plainly rather than omitted.
- [x] The hide-uninstalled-tiers question is answered (**no**), implemented
      (every tier priced at the point of choice) and justified.
- [x] 035's install hook is reused and generalised, not duplicated.
- [x] No new backend endpoint was needed; the one genuine gap (free disk space)
      is reported rather than improvised.
- [x] E2E covers listing, licence visibility, a scripted install, a cancel and
      a remove, downloading nothing, with no fixed sleeps.
- [x] `format:check` · `lint` · `typecheck` · `test` · `build` green; the whole
      Playwright suite green (22 passed, of which 4 are new).

## Tests

**Unit / component — 743 passed (36 files), up from 623 (29 files), and the
whole suite is now free of `act(...)` warnings (38 → 0).**

- `licensing.test.ts` — named vs. informal classification (including
  `"MIT License"` deliberately landing on informal, and a very long unspaced
  string); `null`/`undefined`/blank as explicitly unstated; `null` permissions
  never becoming a decision; an MIT code licence never standing in for silent
  weights; separately-licensed weights flagged; both refusals reported; a
  stated refusal outranking silence for the badge without losing either notice;
  "none required" only claimable when a weights licence exists.
- `ModelLicence.test.tsx` — code and weights as separate rows; the attribution
  verbatim; the word "permissive" (and "free to use", "open source") never
  appearing; the informal statement rendered in full and flagged; refusals in
  danger styling; unstated permissions legible rather than blank; a built-in
  model with no artifact saying it has no separate weights licence instead of
  warning about terms that do not exist; the compact variant dropping nothing.
- `ModelCard.test.tsx` — the facts, requirements and terms of one model;
  `requirements` absent said rather than omitted; a fixture labelled; a
  built-in model offering no buttons at all; the disk-cost notice; install
  POSTed once with the backend's own progress; a failed install's own message
  with a retry; a refused install with the control that clears it; **Cancel
  download** offered only while downloading and taking one click; **Remove
  weights** offered only when installed, asking first, not requesting anything
  until confirmed, and declinable; a refused removal surfaced without losing
  the model.
- `ModelLibrary.test.tsx` — order preserved from the catalog; the counts (and
  "1 model", not "1 models"); every model's terms visible without opening
  anything; the catalog read **once**, not once per card; a failed read with a
  retry; an empty catalog said plainly; closing back to the workflow.
- `useModelCatalog.test.ts` — one read on mount; **no timer at all**; a failed
  read carrying the backend's code and message; a retry that stops claiming the
  read failed while it is in flight; the catalog kept on screen across a
  refresh; nothing applied after unmount.
- `useModelInstallation.test.ts` — seven new cases beside 035's: the DELETE and
  the model it returns; the same request cancelling a download and stopping the
  poll; a poll not flicking the bar back on mid-cancel; one DELETE for a double
  click; an install refused while a removal is in flight; a refused removal;
  and the guard-release regression (**verified to fail against the pre-fix
  code**).
- `SeparationOptions.test.tsx` — every tier priced, not only the selected one;
  a tier with missing weights **shown** rather than hidden; downloading and
  failed tiers named; a failed catalog read leaving the tiers unannotated and
  unblocked; a tier needing no download annotated with nothing; the selected
  model's terms and attribution rendered, before the install, and following the
  selection when it changes.
Regression tests from code review, **each verified to fail against the code as
reviewed**:

- `SeparationOptions.test.tsx` — the radio no longer contradicting the panel
  the moment an install finishes; a tier staying honest after the user selects
  a different one (the fold); and a bumped revision re-reading **once**, with a
  re-render that does not change it re-reading nothing.
- `ModelLibrary.test.tsx` — the summary counting what is installed *now* rather
  than what was installed when the view opened, up and down again, **without a
  second collection read**.
- `ModelCard.test.tsx` — the tab-away / removed-elsewhere / reinstall sequence,
  proving the delete confirmation does not resurrect itself, and that an
  ordinary re-read while the model stays installed does not close a question
  the user is still answering.
- `licensing.test.ts` / `ModelLicence.test.tsx` — a built-in model that
  declares a `code_license` no longer warned about weights it never downloads,
  while the same terms on a downloadable model warn exactly as before.
- `App.test.tsx` — focus returning to the header's models button when the
  library closes itself.

- `DiskCostNotice.test.tsx`, `installProgress.test.ts`, `models.test.ts`
  (`listModels`), `format.test.ts` (`formatMemorySize`), `Header.test.tsx` (the
  toggle, its `aria-expanded`, and `aria-controls` pointing at nothing rather
  than at an absent element), `App.test.tsx` (the library opening beside the
  workflow, the workspace leaving the accessibility tree but **staying
  mounted**, and coming back).

**E2E — 22 passed (4 new), nothing downloaded, no fixed sleeps.**
`e2e/models.spec.ts`: the listing asserted against the **real** `GET /models`
(ids, licensing regions and attribution strings all read from the response);
scripted restrictive and silent-weights records asserted while the model is
still uninstalled; install → cancel → install → confirm → remove with every
`/api/v1/models/...` request intercepted and the request counts asserted; and
the library proven not to disturb the workflow it sits beside.

## Reported out of scope

Per the assignment, found and **not** acted on:

1. **No endpoint reports free disk space beside `models_dir`.** This is the one
   genuine gap the audit found. A `GET /system/storage` (bytes free on the
   filesystem holding `Settings.models_dir`, alongside 018's device report)
   would let the UI say "870 MB needed, 2.1 GB free" instead of "Straticate
   cannot check". It is a backend change with a schema addition, so it wants
   its own numbered feature.
2. **Attribution is not rendered on the running job's telemetry panel.**
   `ModelInfo` — the `runtime_metrics` payload — carries display name,
   architecture, version, mode and stem count, but no `licensing`. Rendering a
   credit there would mean either a contract change or a model fetch from a
   component feature 020 owns. Both are out of scope here.
3. **`installation.error` on a `failed` model is in-memory only** (025's own
   note): a restart reports `available` again, so the library's "Install
   failed" state does not survive one. Unchanged, and correct as documented.

## Known limitations

- **No global "installs in flight" indicator.** A download shows its progress
  on its card in the library and in the configure step's panel when its tier is
  selected; closing the library stops the watching. The transfer belongs to the
  backend and survives, and the state is re-read on return — but there is
  nothing in the header saying "a download is running" while the user is
  elsewhere.
- **The catalog's *enumeration* is read once per opening.** Installation state
  is kept in step without re-reading it (each card folds its own reads back in,
  and closing the library re-reads the configure step), but a model that
  appears in or disappears from the catalog while the app is running — which
  needs a server restart, since the catalog is loaded once at startup — is not
  noticed until the view is reopened.
- **N+1 requests on opening the library**: one `GET /models` plus one
  `GET /models/{id}` per card. Deliberate (see *Why the catalog is read
  separately*), and cheap for a local application with a handful of models —
  but it would want revisiting for a catalog of dozens.
- **The model revision is bumped on closing the library, not on every change.**
  Two browser tabs still drift from each other until one of them re-reads;
  nothing here is a substitute for the live event stream feature 025
  deliberately did not add (`model_install_progress`), and the case for one is
  no stronger now than it was then.
- **No `update`, no resumable downloads, no re-verification of installed
  weights.** All three are 025's documented limitations and none of them is a
  UI problem; remove-then-install is the whole update story.
- **The licence heuristic is deliberately blunt.** `"MIT License"` is treated
  as prose. A user reads one extra sentence; nothing is ever under-stated.
- **`separation_mode` is shown as its raw ID** on a card (`vocals`), not as the
  display name — display names live on `SeparationMode`, and fetching
  `/separation-modes` as well to humanise one label was not worth a second
  request in an inventory view.
