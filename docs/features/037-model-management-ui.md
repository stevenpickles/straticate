# [037] Model management UI

Branch: `037-model-management-ui`
Status: PLANNED
Dependencies: 025, 035

## Objective

A place in the product to see and manage models, rather than only the one
install affordance feature 035 put next to the selected quality tier.

## Why it is numbered now

**Three features have deferred to it** — 025, 032 and 035 — and a fourth (027)
is blocked partly because of it. That is the signal.

What is missing today, from 035's own report:

- no way to **remove** installed weights from the UI (`removeModelWeights` is
  written, tested and never called);
- no way to **cancel** an in-flight download (the REST route exists: `DELETE
  /models/{id}/weights` cancels a running install — feature 025 built it
  precisely so a trickling host could be escaped);
- **no disk-space check or warning** before an 870 MiB fetch;
- no "installs in flight" view;
- the panel describes only the **selected tier's** model. Correct for today's
  one-tier default server, wrong the moment feature 027 or 028 adds a tier.

## The licensing surface — the load-bearing part

`Model.licensing` has been served since feature 025 and is **rendered
nowhere**. `grep -rn "licensing" frontend/src` finds only the generated types
and the alias 035 left in `api/types.ts`.

That stopped being cosmetic:

- Feature 027 is blocked in part because UVR's MDX weights would require
  attribution the application has no place to give.
- The project owner has cleared **CC-BY-NC** weights for personal use, which
  makes feature 028 (Demucs) viable — and **CC-BY carries attribution as a
  binding condition, not a request**.
- A licence is exactly what a user should be able to read **before** committing
  to a multi-hundred-megabyte download, which is the one moment it can still
  change their decision. That was 025's stated reason for surfacing it on the
  resource in the first place.

So this feature owns rendering `licensing`: the licence of code and weights,
whether commercial use is permitted, and the required attribution string —
somewhere a user will actually see it.

## Scope sketch

- A models view: every catalogued model, its installation state, size, licence,
  and requirements (`recommended_vram_mb` / `minimum_vram_mb`, corrected by
  feature 036).
- Install, cancel and remove, driven by the existing REST surface — **no new
  backend endpoints should be needed**; confirm that before writing any.
- Attribution rendered wherever a model is chosen or used, not only in a
  settings corner.
- Decide, finally, the question open since feature 010 and deferred by 025, 026
  and 032: **should a mode hide quality tiers whose weights are not installed?**
  035 argued no — a tier you can see, price and install beats one that silently
  is not there — and this is the feature that can act on that argument by
  making the alternative visible instead.

## Out of scope

Backend changes (unless the audit above finds a genuine gap — then report it
rather than improvising an endpoint). A remote/browsable model catalog. Model
updates as distinct from remove-then-install.

## Acceptance criteria

- [ ] Every catalogued model is visible with state, size, requirements, licence
- [ ] Install, cancel and remove all work from the UI
- [ ] Attribution is rendered where a user will see it before choosing a model
- [ ] Disk space is checked, or the risk is stated, before a large download
- [ ] The hide-uninstalled-tiers question is answered and recorded
- [ ] No new backend endpoint was needed (or the gap is reported, not improvised)
