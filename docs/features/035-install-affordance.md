# [035] First-run model install affordance

Branch: `035-install-affordance`
Status: PR OPEN
Dependencies: 025, 032
PR: #41

## Objective

A user with a fresh checkout can install the one model a default server offers
— from the UI, without reading the docs — and then separate. Until the weights
are there the app says so, names the download size, offers to install it, and
disables "Start separation" with a visible reason instead of pretending the
model is ready and answering `409 model_weights_missing`.

## The defect this pays off

Feature 032 stopped offering the development fixtures as quality tiers, which
was right: before it, a user who uploaded a file and pressed **Start
separation** on the defaults got comb-filtered fixture audio presented as a
separation. Its notes recorded the accepted cost:

> Clone-and-run no longer produces audio without a network fetch. […] the
> frontend has no install affordance because it never reads `installation`.

Feature 025 had already built and served everything needed to fix that —
`installation` on the model resource, `POST /models/{id}/install`,
`DELETE /models/{id}/weights` — and explicitly deferred the UI. This feature is
that UI, scoped to the configure step rather than to a model-management screen
(still unclaimed).

## Scope

- **`frontend/src/api/models.ts`** — `getModel`, `installModel`,
  `removeModelWeights` over the shared `get`/`post`/`del` helpers, typed with
  contract aliases; `ModelInstallState`, `ModelInstallation` and
  `ModelLicensing` added to `api/types.ts`.
- **`frontend/src/components/useModelInstallation.ts`** — the hook that watches
  one model: one read per selected tier, a 1 s poll while a download runs, the
  `install` action, and the pure helpers `needsInstall` / `startBlockedReason` /
  `installationOf`.
- **`frontend/src/components/ModelInstallPanel.tsx`** (+ `.css`) — what the
  configure step says about missing weights, and the button that fetches them.
- **`frontend/src/components/SeparationOptions.tsx`** (+ `.css`) — mounts the
  panel for the selected tier's model, disables "Start separation" with an
  `aria-describedby` reason, and renders a `model_weights_missing` answer to
  `POST /jobs` as the actionable state.
- **`frontend/e2e/install.spec.ts`** — the fresh-checkout case, with the model's
  states scripted so CI downloads nothing.
- Test fixtures for a downloadable model, a built-in model and the four
  installation states; `vi.mock('../api/models')` in the two suites that mount
  the configure step incidentally.

## Out of scope

- Anything under `backend/` and `frontend/src/api/generated/api.d.ts`. Feature
  025 had already generated every type this needed; no schema change was
  required or made.
- **A model-management UI** — browsing every model, updating, removing weights,
  labelling a development fixture as one. Still **unclaimed**, and now the third
  feature to say so (025, 032, 035). `removeModelWeights` is implemented in the
  client because it is one line of the same surface, but nothing calls it.
- **Rendering `licensing`.** `Model.licensing` is served (025) and is the
  natural home for the attribution surface **027** will need — a licence is
  exactly what a user should read *before* starting an 870 MiB download. Not
  rendered here; the alias exists in `api/types.ts` for whoever does.
- **Hiding uninstalled tiers.** Open since 010, deferred by 025, 026 and 032.
  **Still deferred, deliberately** — and this feature is the argument for
  keeping it that way: a tier you can see, price and install is strictly better
  than one that silently is not there. The decision still belongs with the
  model-management UI.
- `frontend/src/state/jobState.tsx`, `App.tsx`, `ws/JobEventBridge.tsx` and the
  reload spec (feature 033, in parallel).

## Expected modules/files

- `frontend/src/api/models.ts` · `models.test.ts` · `types.ts`
- `frontend/src/components/useModelInstallation.ts` · `.test.ts`
- `frontend/src/components/ModelInstallPanel.tsx` · `.test.tsx` · `.css`
- `frontend/src/components/SeparationOptions.tsx` · `.test.tsx` · `.css`
- `frontend/src/components/{AudioSummary,DropZone}.test.tsx` (mock the new
  module; the components are untouched)
- `frontend/src/test/fixtures.ts`
- `frontend/e2e/install.spec.ts`
- `docs/features/035-install-affordance.md` · `ROADMAP.md`

## Acceptance criteria

- [x] With the model uninstalled, the configure step says so, names the size
      (`formatFileSize`, so `870 MB` reads the way every other size in the app
      does) and offers an Install action.
- [x] "Start separation" is disabled until the weights are installed, with a
      reason that is both visible and announced (`aria-describedby`).
- [x] Installing shows real progress from `installation.progress` /
      `downloaded_bytes` on a `progressbar` with proper aria values, and settles
      on `installed` or `failed`.
- [x] A failed install shows the backend's own message and offers a retry.
- [x] Polling stops on every terminal state, on a failed read, while the tab is
      hidden, and on unmount — asserted with `vi.getTimerCount()`.
- [x] A model with `requires_download: false` shows none of the affordance.
- [x] `POST /jobs` answering `model_weights_missing` re-reads the model and
      renders the install, not a raw error — and the download that follows shows
      its progress, because the live state outranks the refusal.
- [x] The E2E tier covers it without downloading anything, with no fixed sleeps.
- [x] `format:check` · `lint` · `typecheck` · `test` · `build` green; the other
      E2E specs unaffected (15 passed).

## Required tests

- `api/models.test.ts` — request URL, method and parsed `installation` for each
  of the three calls; live progress; a failed install's `error`; a model that
  needs no download; a percent-encoded model ID; typed `ApiError`s for
  `model_not_found`, `model_busy` and `model_not_downloadable`.
- `components/useModelInstallation.test.ts` — the pure helpers (including "an
  unread model blocks, a tier-less selection does not"); one read per selection
  and none at all with no tier selected; a changed selection never showing the
  previous model; the poll running once per interval and stopping on
  `installed`, on `failed`, on a read failure, while hidden (and re-reading
  immediately on return) and on unmount with no timer left; one POST for a
  double click; a refused install surfaced without losing the model. Plus the
  review regressions, each verified to fail against the pre-fix code: a retry
  resuming the poll; a read already in flight not un-starting the install it
  raced; a hung install for one tier not swallowing another tier's click; and
  the refusal hint being recorded, kept while a read agrees with it, and dropped
  the moment one reports `downloading` or `installed`.
- `components/ModelInstallPanel.test.tsx` — silence for a built-in model and
  before the first read; the size and Install button; progress and aria values
  (determinate and indeterminate); the installed confirmation; a backend failure
  message with a retry; a refused install; an unreadable model offering only a
  re-read; the `model_weights_missing` message rendered as a *priced*
  invitation; the download shown rather than the refusal that preceded it, with
  no second install beside it; and a retry offered when a read fails
  mid-download.
- `components/SeparationOptions.test.tsx` — the size named and Start disabled
  with a reason; install → real progress with Start still disabled and the mode
  radios still usable; Start enabled once installed; nothing rendered for a
  model needing no download; a `model_weights_missing` create rendered as the
  panel (exactly one message, and the model re-read); a refused install retried;
  Start refused while the state is still unknown; and the whole
  refused-then-installed flow, through progress to a re-enabled Start.
- `e2e/install.spec.ts` — see below.

## Notes / decisions

### Progress is polled, and the interval is 1 s

Feature 025 put install progress on the model resource rather than on the event
hub and wrote down why: an install is rare, user-initiated and coarse-grained;
REST is already the source of truth for reconnect and refresh
(ARCHITECTURE.md §11); the state field is needed on a plain `GET` anyway; and an
event would have been a shared-contract change with no consumer. That reasoning
was read before this was built and it still holds — so this feature adds **no**
event and consumes exactly the field 025 serves.

AGENTS.md principle 3 ("no polling loops") is a rule about **job** progress:
chunk-grained real work, ~4 Hz, with an event stream that already exists, where
a timer would be a lie. Re-reading one resource while its own download runs is
not that.

**1 s.** The artifact is 870 MiB — a minute or more on a fast connection, much
longer on a slow one — so 1 Hz draws a smooth bar and reports the outcome within
a second of it happening. Faster buys nothing a human can perceive while
multiplying requests against a server that is, on the same event loop, writing
the download to disk (025 keeps the per-chunk write there deliberately). Slower
would make the settle to `installed` feel unresponsive, since that transition is
what the Start button waits on. 100 ms would be abusive; 5 s would be rude.

### Every way the loop stops

- **Terminal state.** The next poll is scheduled only while
  `installation.state === 'downloading'`.
- **Unmount / a different tier.** The timer lives in an effect whose cleanup
  clears it; a test asserts `vi.getTimerCount() === 0` after unmounting
  mid-download.
- **A hidden tab.** `visibilitychange` stops the scheduling and a return
  re-reads *immediately*, so the first thing a returning user sees is current
  rather than up to a second stale.
- **A failed read.** The poll effect re-runs on each successful read (every one
  lands a fresh record); a read that fails changes nothing, so nothing is
  rescheduled, rather than hammering a backend that is not answering. That is
  only defensible if the user can restart it, so **the panel offers "Try again"
  wherever a request failed — including mid-download**, beside the frozen bar.
  It did not at first review: the retry was gated on having no record at all, so
  a blip mid-download left a stuck bar, an error line, and no control but a tier
  switch or a page reload. A transient blip now really does cost one click; the
  download itself is unaffected, since it belongs to the backend.

### The hook's state carries the model ID

`useModelInstallation` stores `{modelId, model, error, installingFor,
weightsMissing}` together, so "you selected a different tier" is a *derivation*
("this record is not about the model you are asking about, so you are loading")
rather than an effect that resets state. That matters beyond tidiness:
`react-hooks`'s `set-state-in-effect` rule rejects the reset-effect shape
outright, and the derived form cannot render model A's size beside model B's
name even for one frame.

`installingFor` names a *model* rather than being a boolean, and is the one
field a changed selection carries through. A boolean is cleared when the
selection changes but the ref guarding the POST is not, so an install request
that never settles would silently swallow every later click — including one for
a different tier, whose button rendered enabled because the boolean had been
reset. Keyed by ID, the guard and what the button shows cannot disagree.

### Every response is stamped, and only the newest is applied

Requests take a sequence number when they *start*; a response is applied only if
nothing newer has been. Without it, a read already in flight when Install is
clicked lands **after** the install's own answer and overwrites `downloading`
with the `available` the server described a round trip earlier — the poll never
starts, no progress is ever shown, and Start stays disabled long after the
weights have arrived. Cancelling on unmount does not cover it: that read belongs
to the same model and the same refresh, and nothing about it changed except what
happened while it was in the air. The window is real and one click wide, because
`noteWeightsMissing` starts exactly such a read and then renders the button.

### What blocks Start: not knowing is not ready

`startBlockedReason` takes the whole handle, not just the record, and answers
`null` only when the server has actually said the weights are there (or that the
model needs none). Every other state blocks, with its own sentence: still
reading, could not be read, `available`, `downloading`, `failed`.

The alternative — treating an unread model as startable, on the grounds that the
client's ignorance is not the user's problem — leaves a window one round trip
wide on entering the configure step and after **every** mode switch, in which a
click produces exactly the `model_weights_missing` refusal this feature exists
to prevent. "Unknown" is not "ready". The rule is only honest because every
blocking state has a control on screen: an install, a retry, or a wait that ends
by itself in one round trip.

A `model_weights_missing` answer is deliberately *not* a separate input to this
question. The refusal triggers a re-read, and it is the re-read that blocks
Start; gating on the stale error as well would strand a user whose weights
turned out to be present after all, with a disabled button and nothing able to
clear it.

### The 409 is a hint that a read is stale — it never outranks a live state

Weights can vanish between the check and the job — that is exactly the case
`docs/contracts/rest-api.md` documents. When `POST /jobs` answers
`model_weights_missing`, the message is rendered *inside* the install panel with
the Install button beside it, and the generic create-error alert is suppressed
for that one code, so a user sees one actionable message rather than a red
sentence and a separate panel disagreeing with it.

**What the refusal is not, is a state of its own.** The first implementation
derived it from `create.status === 'error'`, which persists until the *next*
Start attempt — which the same code had just disabled — and let it force the
panel's `downloading`, `installed` and `failed` branches all false. The result
was the worst possible outcome on the exact flow this feature was built for:
Start, refused, Install, and then **870 MB with no progress bar, no byte counts
and no completion**, behind a stale alert, with a live "Install model" button
beside a running download whose second `POST /install` could only earn
`model_busy`.

So the hint lives in the hook, beside the record it is about, and **clears
itself the moment a read (or the install's own `202`) reports `downloading` or
`installed`** — the server having spoken more recently than the job refusal did.
The panel enforces the same precedence directly: a download in flight, or a
recorded failure, is the live state and suppresses the hint; only a record
claiming `installed` — the stale one the refusal is *about* — loses to it. And
"Install model" is never offered beside a running download, whatever a hint
says.

### Nothing here can leak a download URL

025 keeps the URL and the pinned digest off the wire entirely, because a
presigned URL's query string *is* the credential. This feature renders
`installation.error.message` verbatim and invents no other text about the
transfer, so there is nothing to leak; a panel test asserts the rendered region
contains no `http`.

### The E2E tier scripts the states rather than downloading 870 MiB

The Playwright backend runs with `STRATICATE_INCLUDE_DEVELOPMENT_MODELS=1` —
load-bearing for every other spec, since the fake separator is what makes the
tier need no GPU and no weights. So `install.spec.ts` reconstructs the
fresh-checkout view in the browser instead:

1. `GET /separation-modes` is filtered to what a default server would serve,
   with every tier backed by a `development_only` model removed and any mode
   thereby emptied dropped. The rule is **derived from `GET /models`**, not
   hardcoded: it is 032's own predicate applied to the same data, so a catalog
   change cannot make the spec assert something the app never sees.
2. `GET /models/{id}` and `POST /models/{id}/install` are scripted through
   `available → downloading (0.25) → downloading (0.6) → installed`. The real
   install route is never reached; the spec asserts exactly one install request
   and that at least three reads followed it, which is what proves the bar was
   driven by polling rather than by the POST's single answer.

A second test uses the **real** backend to show a tier backed by a built-in
separator rendering none of the affordance, and switching between the two tiers
bringing it back and taking it away. A third drives the flow the review found
broken, end to end: a record claiming `installed`, a job refused with
`model_weights_missing` (scripted, because on a machine that *does* have these
weights the real backend would happily start an 870 MiB separation), the install
that refusal invites, and the progress that has to appear once it starts.
Following 030's discipline, every wait is a condition — the DOM reaching a state
the script put it in — and nothing sleeps.

## Known limitations

- **No way to remove weights from the UI.** `removeModelWeights` exists in the
  client and is untested against the UI because nothing calls it; removing is a
  model-management action, not a configure-step one.
- **No disk-space warning before an 870 MiB download**, and no cancel button for
  one in flight. `DELETE /models/{id}/weights` cancels a running install (025),
  so the affordance is one call away — but "cancel" and "remove" being the same
  request is a model-management question, and a user can still escape by
  restarting the backend.
- **A failed read stops the poll**, so a network blip mid-download costs the
  user a click on "Try again" (which the panel offers beside the frozen bar).
  The alternative — retrying forever — hammers a backend that may be gone, and
  the download itself is unaffected either way.
- **Progress is per-model, not per-app.** Navigating away from the configure
  step (starting another separation, say) stops the watching; the download keeps
  running on the backend and the state is re-read on return. There is no global
  "installs in flight" indicator, which is again the model-management UI's job.
- **The panel describes only the *selected* tier's model.** A catalog with
  several uninstalled tiers would make a user select each in turn to see its
  size. Fine for the one-tier default server; another argument for the
  management screen once 027 and 028 land.
