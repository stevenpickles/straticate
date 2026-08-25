# [040] Free-disk-space endpoint for installs

Branch: `040-free-disk-space-endpoint`
Status: PLANNED
Dependencies: 025, 037

## Objective

Let the UI say "870 MB needed, 2.1 GB free" before someone starts a download
that cannot finish.

## Why the frontend cannot do this itself

Feature 037 investigated and reported it rather than improvising an endpoint.
The weights are written by the **backend**, to `Settings.models_dir`, on the
machine running Straticate. `navigator.storage.estimate()` describes the
*page origin's quota inside the browser profile* — a different number about a
different disk, and useless here.

So 037 states the limitation honestly in the UI wherever an install is offered:
that the download will be written to the machine running Straticate, and that
Straticate cannot check that machine's free space from the browser. That is the
correct interim behaviour, and this feature is what replaces it with a fact.

## Scope

- A small system endpoint (e.g. `GET /system/storage`) reporting free and total
  bytes for the filesystem holding `Settings.models_dir`. Note the existing
  `GET /system/devices` precedent in `api/system.py`, and feature 018's pattern
  of degrading gracefully rather than raising when a platform cannot answer
  (`total_system_memory_bytes` returns `0` and documents it as unknown).
- Frontend: use it in the install affordance (035) and the model library (037)
  so the disk cost is a comparison rather than a bare number. Keep 037's honest
  wording as the fallback for when the figure is unavailable.
- Decide whether the installer should **refuse** an install it can prove will
  not fit, or only warn. Refusing is defensible; a false refusal on a wrong
  reading is not. Whichever you choose, say why.

## Out of scope

Quotas, cleanup, or retention policies for existing artifacts — export
artifacts and job outputs accumulate unboundedly and that is separately
unclaimed (021, 022 and 025 all record it). Reporting free space anywhere other
than where an install is offered.

## Acceptance criteria

- [ ] The endpoint reports free/total for the filesystem holding `models_dir`,
      and degrades to a documented "unknown" rather than raising on a platform
      that cannot answer
- [ ] The install affordance and the model library show the comparison, and
      fall back to 037's honest wording when the figure is unavailable
- [ ] The refuse-or-warn decision is made and justified
- [ ] Contract documented; `api.d.ts` regenerated; all gates green
