# [042] Production build — the backend serves the frontend

Branch: `042-production-build`
Status: PLANNED
Dependencies: 003, 024

## Objective

**One process.** `uvicorn straticate.main:app` serves the API *and* the built
single-page app, so running Straticate is one command and one URL instead of two
servers a user has to understand.

## Why

Today the only way to use Straticate is to start uvicorn and the Vite dev server
by hand and open `:5173`. That is a development arrangement: the Vite proxy is
carrying production traffic, and a user has to know which port is which. For a
**local-first, desktop-shaped tool** (ARCHITECTURE.md §14) the natural shape is
one process on one port.

It also settles a question that has come up twice. The CORS allowlist
(`Settings.cors_origins`, feature 029) and its `expose_headers` exist because a
browser page might talk to `:8000` from a different origin. Served this way the
app is **same-origin**, so that path stops being the normal case — which is also
why feature 021's `Content-Range` / `Accept-Ranges` headers and feature 023's
Web Audio seeking have never needed it in practice.

## Scope

- Mount the built SPA (`frontend/dist`) from the FastAPI app: static assets, and
  an **SPA fallback** so a deep link or a refresh returns `index.html` rather
  than a 404 — while `/api/v1/**` and the WebSocket keep their exact current
  behaviour and are never shadowed by the fallback. Get the ordering right and
  test it: a request for `/api/v1/nope` must still be the JSON `404` envelope,
  not `index.html`.
- **Degrade honestly when the bundle is absent.** A source checkout that has not
  run `npm run build` has no `frontend/dist`. The API must still start and work
  (that is how the backend suite and the E2E tier run), and the root URL should
  say what to do rather than 404 or crash. Follow feature 018's pattern: a
  missing capability is a documented state, not an error.
- Make the bundle location a `Settings` field rather than a hardcoded relative
  path, so the server works from any working directory. `config.py` already
  solves this for `models_dir` — reuse the reasoning.
- **Development mode is unchanged.** Vite on `:5173` proxying `/api` to `:8000`
  stays exactly as documented; this adds a production path, it does not replace
  the development one.
- `DEVELOPMENT.md` and `README.md`: how to build and run for real use, kept
  distinct from the development instructions. The README quick start should
  become the one-process version.
- Consider whether CI should build the frontend and assert the mount works. If
  that costs a minute on every PR, say so and decide deliberately.

## Out of scope

Packaging — a pipx console entry point, an installer, a desktop bundle — which
is a larger question and was explicitly not chosen for v0.1.0. Any change to the
API, the WebSocket, or the frontend's own behaviour. Serving on anything other
than the loopback interface the settings already default to; this is not a
hardening or network-exposure feature.

## Acceptance criteria

- [ ] `npm run build` then `uvicorn straticate.main:app` serves a working app on
      one port: upload, configure, separate, inspect and export all function
- [ ] Deep links and refreshes work, and `/api/v1/**` is never shadowed — an
      unknown API path is still the JSON error envelope
- [ ] With no `frontend/dist`, the API starts and works normally and the root
      URL explains what to do
- [ ] The bundle path is configurable and working-directory independent
- [ ] Development mode is unchanged
- [ ] Docs distinguish "run it" from "develop it"
- [ ] All gates green; existing e2e specs unaffected

## Required tests

The mount and the fallback, both with and without a bundle present (build a
throwaway `index.html` in a temp directory rather than depending on a real
build); that `/api/v1/**` and the WebSocket are unaffected; and
working-directory independence.
