# [042] Production build — the backend serves the frontend

Branch: `042-production-build`
Status: PR OPEN
Dependencies: 003, 024
PR: #53

## Objective

**One process.** `uvicorn straticate.main:app` (or `python -m straticate`)
serves the API *and* the built single-page app, so running Straticate is one
command and one URL instead of two servers a user has to understand.

## Why

Before this, the only way to use Straticate was to start uvicorn and the Vite
dev server by hand and open `:5173`. That is a development arrangement: the Vite
proxy was carrying production traffic, and a user had to know which port was
which. For a **local-first, desktop-shaped tool** (ARCHITECTURE.md §14) the
natural shape is one process on one port.

## Scope

- `backend/src/straticate/frontend.py` — new module: the SPA mount, the fallback
  route that refuses `/api/**`, and the page served when there is no bundle.
- `backend/src/straticate/config.py` — `Settings.frontend_dist_dir`, defaulting
  to the repository's `frontend/dist` resolved from the *module's* location.
- `backend/src/straticate/main.py` — `create_app()` mounts the frontend last;
  the lifespan logs which of the two modes the server started in.
- `README.md` — a "Run it" quick start (build once, one command, one URL) kept
  distinct from "Develop it".
- `DEVELOPMENT.md` — the production build/run path, alongside the unchanged
  development flow.
- `.github/workflows/ci.yml` — the `e2e` job builds the frontend and smoke-tests
  the real mount (see *CI*).

## Out of scope

Packaging — a pipx console entry point, an installer, a desktop bundle — which
is a larger question and was explicitly not chosen for v0.1.0. Any change to the
API, the WebSocket, or the frontend's own behaviour. Serving on anything other
than the loopback interface the settings already default to, TLS, and auth: this
is not a hardening or network-exposure feature. Version bumps and the CHANGELOG
are 043's.

## Expected modules/files

- `backend/src/straticate/frontend.py` (new)
- `backend/src/straticate/config.py` (one field)
- `backend/src/straticate/main.py` (wiring + startup log)
- `backend/tests/test_frontend_mount.py` (new)
- `README.md`, `DEVELOPMENT.md`, `.github/workflows/ci.yml`

## Acceptance criteria

- [x] `npm run build` then `python -m straticate` serves a working app on one
      port: upload, configure, separate, inspect and export all function —
      **verified by driving the built app in a real browser**, see *Verification*
- [x] Deep links and refreshes work, and `/api/v1/**` is never shadowed — an
      unknown API path is still the JSON error envelope, a bad method on a real
      route is still `405`, and the WebSocket is untouched
- [x] With no `frontend/dist`, the API starts and works normally and the root
      URL explains what to do
- [x] The bundle path is configurable (`STRATICATE_FRONTEND_DIST_DIR`) and
      working-directory independent
- [x] Development mode is unchanged; the Playwright tier still passes (24 tests)
- [x] Docs distinguish "run it" from "develop it"
- [x] All gates green; backend suite clean under `-W error` (**867 passed**)

## Required tests

`backend/tests/test_frontend_mount.py` (24 tests), every one of them against an
application built with an **explicit** `frontend_dist_dir` — pointing either at
a throwaway bundle in `tmp_path` or at a directory that does not exist. That is
not fastidiousness: the default is the repository's `frontend/dist`, which
exists for anyone who has run `npm run build` and does not on CI, so a test
relying on the default would assert opposite things depending on who ran it.

- the bundle is served: root, hashed asset, deep link, `HEAD`, and a traversal
  attempt that gets the app rather than the file outside it;
- the API is untouched: `/api/v1/health` works, an unknown API path is the
  `not_found` envelope under **every** method, `POST /api/v1/health` is still
  `405`, `/api` and `/api/v2/...` are reserved too, `/docs` and `/openapi.json`
  are not shadowed, the OpenAPI document gains no path, and the WebSocket still
  connects;
- no bundle: the API works, the root URL names `npm run build` and the setting,
  a deep link is still a `404`, and a directory with no `index.html` counts as
  no bundle;
- the path: absolute by default, unchanged by `chdir`, settable from the
  environment, and an application serving the bundle it was *given*.

## Notes / decisions

### The fallback refuses to match; it does not decline afterwards

This is the whole feature, and the failure mode is silent. A catch-all route (or
`StaticFiles(html=True)` mounted at `/`) matches `/api/v1/nope` as happily as
`/jobs/01J…`, so every unknown API path — and every client bug that produces one
— would come back `200 text/html` instead of the documented envelope. Nothing
looks broken until something tries to parse it.

Answering "is this the API?" *inside* the handler is not enough either, because
of how Starlette's router dispatches:

```python
for route in self.routes:
    match, child_scope = route.matches(scope)
    if match == Match.FULL:
        ...handle and return
    elif match == Match.PARTIAL and partial is None:
        partial = route          # remembered, used only if nothing FULL-matches
```

A **full match wins immediately, wherever it is in the table** — being last does
not protect anything. So a fallback that accepted every method would answer
`POST /api/v1/health` itself, and the `405` that route's partial match exists to
produce would never happen. `SinglePageAppRoute.matches` therefore returns
`Match.NONE` for anything under `/api`, for any method other than `GET`/`HEAD`,
and for any non-HTTP scope. With those three refusals the routing table behaves
exactly as it did when nothing was mounted; the mount can only *add* answers for
paths that previously had none.

`/api`, not `/api/v1`: a future `/api/v2` should be a routing decision, not a
silent change in which requests turn into HTML, and it is already the boundary
Vite's dev proxy uses, so development and production agree on where the API
ends. A test pins that `main.API_PREFIX` still lives under it.

### No bundle is a documented state (feature 018's pattern)

`frontend/dist` does not exist until somebody runs `npm run build` — which is how
the backend suite runs, how the Playwright tier runs, and how every contributor
works before their first build. So the application starts and serves the entire
API regardless; only the root URL differs, answering with a small page naming
the directory it looked in, the command that fixes it, and the two things that
*do* work meanwhile (`/api/v1/health` and `/docs`).

**200, not 404 or 503.** Nothing failed and nothing is degraded: every
documented endpoint answers. What is missing is a build step the reader can run,
so the response is the instructions for running it — the same reasoning that
makes a host with no GPU a normal `200` from `/system/devices`. Only the root
URL is special-cased; a deep link with no bundle is still a `404`, because
pretending to be an app that does not exist would be the dishonest answer.

### The bundle is located once, at build time

`mount_frontend` looks for `index.html` when the application is built, not per
request. That keeps a filesystem `stat` off every request, and the answer cannot
change for a running server in any way that matters — a build finishing later
leaves the process serving what it found at startup. The cost is that building
the frontend while the server is running means restarting it, which DEVELOPMENT.md
says.

### Working-directory independence

`Settings.frontend_dist_dir` resolves from `config.py`'s own location
(`parents[3] / "frontend" / "dist"`), exactly as `models_dir` does, and is
overridable with `STRATICATE_FRONTEND_DIST_DIR`. A relative `frontend/dist`
would have resolved against the process working directory, so the app would
appear when you started from `backend/` and vanish when you started from
anywhere else. Verified by running the server from `C:\` with the interpreter
inside the checkout's `.venv`: it served the same app.

### What the CORS allowlist is for now

**`Settings.cors_origins` (feature 029) is still load-bearing. Do not remove it,
and do not widen it.** What changed is only that it is no longer on the *normal*
path.

Served this way the app is **same-origin**: the page and the API share a scheme,
host and port, so the browser sends no `Origin` a preflight would care about, and
`CORSMiddleware` never gets consulted for the traffic a user generates. That is
also why feature 021's `Content-Range`/`Accept-Ranges` and feature 023's Web
Audio seeking have never needed the `expose_headers` list in practice.

The allowlist now covers exactly one thing: **a browser page on some other
origin talking to this backend directly.** Two of those are real today and both
run on every PR —

- the development flow (Vite on `:5173`, which the two default entries name);
- the Playwright tier (Vite on `:5123` against a backend on `:8123`) —

and both are *proxied*, so in the common case they too look same-origin to the
browser and the list is not consulted. It is consulted the moment a page fetches
`:8000` itself, which is what the stem player does when it is not behind the dev
proxy.

So the honest description is: **not dead configuration, but no longer the path
most requests take.** Deleting it breaks cross-origin development the first time
someone works without the proxy; widening it to `["*"]` is worse, because it
buys nothing for a same-origin app and hands every page on the internet the
ability to call a backend on the user's own machine.
(`allows_credentials()` already refuses to combine `"*"` with credentials, which
is the *second* line of defence, not a licence to reach for the first.)

Nothing here changes with the feature that eventually introduces auth: that
feature should re-read 029 rather than this note.

### CI: yes, and in the `e2e` job

`backend/tests/test_frontend_mount.py` covers the routing hermetically and in
milliseconds, which is where routing belongs. What it cannot see is the one
thing only a real Vite build decides: whether the asset URLs in the built
`index.html` are root-relative and therefore resolve against the mount. Set
`base` in `vite.config.ts` and every hermetic test stays green while the served
app loads nothing.

So CI builds the frontend once and asserts, against a running
`python -m straticate`, that the root URL and a deep link are the built app,
that the asset the built page asks for is served, and that an unknown
`/api/v1/**` path is still the JSON envelope.

It is in the **`e2e` job** because that job already installs both toolchains, so
the marginal cost is one `vite build` and one server start — about 15 s, on a
job that is not the pipeline's critical path — rather than the ~1 min a job of
its own would spend installing Python and Node to do the same thing. Adding Node
to the `backend` job would have slowed the critical path instead.

It is deliberately **not a Playwright spec**, which was the obvious alternative:
Playwright's `webServer` is global to the config, not per-project, so a
production spec would make every local `npm run e2e` build the frontend and start
a third server before running anything. That is exactly the "slow and brittle"
the brief warned about, for a check that needs no browser — the assertions are
about bytes on the wire, and `curl` makes them in seconds.

### Verification

Not reasoned about — run. `npm run build`, then `python -m straticate` on
`:8042` with a temporary data directory, then the app driven in Chrome:

- the root URL served the built app (header reporting `backend v0.1.0.dev0`,
  i.e. the same-origin `/api/v1/version` call succeeded);
- uploaded a 6 s stereo WAV through the file picker → the Configure phase showed
  the probed metadata;
- selected Standard Stems / Fast and started the job → it ran and completed, and
  the backend log shows `WebSocket /api/v1/ws [accepted]`, so progress arrived
  over the same-origin socket;
- Inspect listed four stems, and pressing Play ran the transport to `0:06 / 0:06`
  — all four stems fetched from `/api/v1/jobs/{id}/stems/{stem}` and decoded by
  Web Audio;
- a hard navigation to `/some/deep/link` returned the app, which restored the
  completed job;
- the export path was exercised over the same server rather than through the
  browser's save dialog: `GET /api/v1/jobs/{id}/export?export_format=wav_pcm24`
  returned `200 application/zip` with `Content-Disposition: attachment`, holding
  `vocals/drums/bass/other.wav` and `separation.json`. A ranged stem request
  returned `206` with `Content-Range`;
- no console errors.

Also verified live: `POST /api/v1/health` → `405` envelope, `GET /api/v1/nope`
and `POST /api/v1/nope` → `404` envelope, `/api` → `404` envelope, `/docs` and
`/openapi.json` unaffected; a server started from `C:\` served the same app; and
a server started with `STRATICATE_FRONTEND_DIST_DIR` pointing at a directory
that does not exist served the "frontend is not built" page at `/`, a `404`
envelope on a deep link, and a fully working API.

### Noticed, out of scope

- **Packaging remains the real "one command" story.** This feature removes the
  second *server*; it does not remove `uv sync`, `npm ci` and `npm run build`
  from a first-time setup. A console entry point plus a bundled `dist` would,
  and is the natural successor once v0.1.0 ships.
- **Cache headers for hashed assets.** `FileResponse` sends `ETag` and
  `Last-Modified` but no `Cache-Control`, so Vite's content-hashed assets are
  revalidated on every load instead of being cached immutably. Harmless on
  loopback; a one-line improvement for anyone who ever serves this over a
  network, which is itself a different feature.
