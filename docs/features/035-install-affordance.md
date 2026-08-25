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
      renders the install, not a raw error.
- [x] The E2E tier covers it without downloading anything, with no fixed sleeps.
- [x] `format:check` · `lint` · `typecheck` · `test` · `build` green; the other
      E2E specs unaffected (15 passed).

## Required tests

- `api/models.test.ts` — request URL, method and parsed `installation` for each
  of the three calls; live progress; a failed install's `error`; a model that
  needs no download; a percent-encoded model ID; typed `ApiError`s for
  `model_not_found`, `model_busy` and `model_not_downloadable`.
- `components/useModelInstallation.test.ts` — the pure helpers (including "an
  unread model blocks nothing"); one read per selection and none at all with no
  tier selected; a changed selection never showing the previous model; the poll
  running once per interval and stopping on `installed`, on `failed`, on a read
  failure, while hidden (and re-reading immediately on return) and on unmount
  with no timer left; one POST for a double click; a refused install surfaced
  without losing the model.
- `components/ModelInstallPanel.test.tsx` — silence for a built-in model and
  before the first read; the size and Install button; progress and aria values
  (determinate and indeterminate); the installed confirmation; a backend failure
  message with a retry; a refused install; an unreadable model offering only a
  re-read; and the `model_weights_missing` message rendered as an invitation.
- `components/SeparationOptions.test.tsx` — the size named and Start disabled
  with a reason; install → real progress with Start still disabled and the mode
  radios still usable; Start enabled once installed; nothing rendered for a
  model needing no download; a `model_weights_missing` create rendered as the
  panel (exactly one message, and the model re-read); a refused install retried.
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
  rescheduled. The panel offers a retry instead of hammering a backend that is
  not answering. A transient blip therefore costs one click — the download
  itself is unaffected, since it belongs to the backend.

### The hook's state carries the model ID

`useModelInstallation` stores `{modelId, model, error, installing}` together, so
"you selected a different tier" is a *derivation* ("this record is not about the
model you are asking about, so you are loading") rather than an effect that
resets state. That matters beyond tidiness: `react-hooks`'s
`set-state-in-effect` rule rejects the reset-effect shape outright, and the
derived form cannot render model A's size beside model B's name even for one
frame.

### What blocks Start, and what deliberately does not

`startBlockedReason(model)` is a pure function of the model record, with a
distinct sentence for `available`, `downloading` and `failed`. Two non-reasons:

- **A model that could not be read.** The user is not blocked by a fact the
  *client* failed to fetch. Start stays enabled and `POST /jobs` is the
  authority; the panel says the check failed and offers to re-run it.
- **A `model_weights_missing` answer that is still standing.** The 409 triggers
  a re-read, and it is the *re-read* that blocks Start. Gating on the stale
  error as well would strand a user whose weights turned out to be present after
  all, with a disabled button and nothing able to clear it.

### The 409 is rendered as an invitation, not an error

Weights can vanish between the check and the job — that is exactly the case
`docs/contracts/rest-api.md` documents. When `POST /jobs` answers
`model_weights_missing`, the message is rendered *inside* the install panel with
the Install button beside it, and the generic create-error alert is suppressed
for that one code, so a user sees one actionable message rather than a red
sentence and a separate panel disagreeing with it.

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
bringing it back and taking it away. Following 030's discipline, every wait is a
condition — the DOM reaching a state the script put it in — and nothing sleeps.

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
  user a click on "Try again". The alternative — retrying forever — hammers a
  backend that may be gone, and the download itself is unaffected either way.
- **Progress is per-model, not per-app.** Navigating away from the configure
  step (starting another separation, say) stops the watching; the download keeps
  running on the backend and the state is re-read on return. There is no global
  "installs in flight" indicator, which is again the model-management UI's job.
- **The panel describes only the *selected* tier's model.** A catalog with
  several uninstalled tiers would make a user select each in turn to see its
  size. Fine for the one-tier default server; another argument for the
  management screen once 027 and 028 land.
