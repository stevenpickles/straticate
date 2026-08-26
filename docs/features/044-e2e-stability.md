# [044] Playwright tier stability under load

Branch: `044-e2e-stability`
Status: PLANNED
Dependencies: 030

## Objective

Make the Playwright tier trustworthy when the machine is busy, so a failure
means a defect rather than a coincidence.

## Why this is numbered now

Three flakes were observed during the 033–042 waves, and the rule was that a
third earns a feature:

| Spec / test | Seen during | Outcome |
| --- | --- | --- |
| `separation.spec.ts` — "exports the stems as a real download" | 037 | passed in isolation and on re-run |
| `separation.spec.ts` — "offers a route back out of the inspect phase" | 040 | passed in isolation and on the immediate re-run |
| `separation.spec.ts` — a progress assertion | 042 | passed alone 7/7, whole tier re-ran clean 24/24 |

Two things stand out. **All three are in `separation.spec.ts`**, and **none has
ever failed in CI** — every occurrence was on a development machine running
several agents, a GPU workload and a 4½-minute backend suite concurrently.

So this is not "the tests are wrong". It is that the one spec which drives a
real job through real timing is the one that loses when the machine is
oversubscribed — and a tier nobody trusts is worse than no tier, because its
failures get waved through, which is exactly what a real regression would need
to slip past.

## Scope

- **Diagnose before changing anything.** Reproduce under deliberate load (run
  the backend suite, or a GPU job, alongside the tier) rather than guessing.
  Establish *which* waits fail: the fake separator's chunk pacing, a
  `waitForResponse`, an `expect.poll` bound, or Playwright's own action
  timeouts.
- **Do not reach for `waitForTimeout`.** Feature 030 established no fixed
  sleeps and verified it by grep; that discipline is why these tests are
  otherwise sound and must survive this feature.
- Consider, and justify: raising the *specific* timeouts that lose under load
  rather than a global bump; making the fake separator's pacing in the E2E
  fixture explicitly generous (it is a fixture, not a benchmark); asserting on
  the recorded WebSocket event sequence rather than on transient DOM state
  where the two are equivalent (030 already does this in places, and the
  places it does are not the ones that flake); and Playwright's own retry
  facilities, weighing that a retry can also hide a genuine intermittent bug.
- **Quantify the result.** Run the tier N times under load before and after —
  a claim of "more stable" without a count is not evidence.

## Out of scope

Changing application behaviour to suit a test. Adding retries as a substitute
for diagnosis. Broadening coverage — this feature makes the existing tier
trustworthy, it does not grow it.

## Acceptance criteria

- [ ] The cause is diagnosed and stated, not guessed at
- [ ] No fixed sleeps introduced; 030's grep still comes back clean
- [ ] The tier passes repeatedly under deliberate contention, with the number of
      runs reported
- [ ] Wall-clock cost in CI is unchanged or better
- [ ] If retries are used at all, the reasoning is recorded — including what a
      retry could hide
