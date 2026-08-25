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

- `backend/src/straticate/frontend.py` — new module: the bundle server, the
  fallback installed as the router's `default`, and the page served when there
  is no bundle.
- `backend/src/straticate/config.py` — `Settings.frontend_dist_dir`, defaulting
  to the repository's `frontend/dist` resolved from the *module's* location.
- `backend/src/straticate/main.py` — `create_app()` installs the frontend
  fallback; the lifespan logs which of the two modes the server started in.
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
- [x] Deep links and refreshes work, and `/api/v1/**` is never shadowed **in any
      spelling** — an unknown API path is still the JSON error envelope, a bad
      method on a real route is still `405`, `redirect_slashes` still redirects,
      and the WebSocket is untouched
- [x] A genuine miss inside the bundle is a `404`, not the entry document
- [x] With no `frontend/dist`, the API starts and works normally and the root
      URL explains what to do
- [x] The bundle path is configurable (`STRATICATE_FRONTEND_DIST_DIR`) and
      working-directory independent
- [x] Development mode is unchanged; the Playwright tier still passes (24 tests)
- [x] Docs distinguish "run it" from "develop it"
- [x] All gates green; backend suite clean under `-W error` (**891 passed**)

## Required tests

`backend/tests/test_frontend_mount.py` (48 tests), every one of them against an
application built with an **explicit** `frontend_dist_dir` — pointing either at
a throwaway bundle in `tmp_path` or at a directory that does not exist. That is
not fastidiousness: the default is the repository's `frontend/dist`, which
exists for anyone who has run `npm run build` and does not on CI, so a test
relying on the default would assert opposite things depending on who ran it.

- the bundle is served: root, hashed asset, deep link, `HEAD`, and a traversal
  attempt that gets the app rather than the file outside it;
- a **miss** inside the bundle is not a deep link: a stale hashed chunk, a
  missing stylesheet, `favicon.ico` and a module `import()` (`Accept: */*`) all
  stay `404`, while a navigation to an extension-shaped path still gets the app;
- the API is untouched: `/api/v1/health` works, an unknown API path is the
  `not_found` envelope under **every** method and in **every spelling**
  (`//api/…`, `///api/…`, `/./api/…`, `/x/../api/…`, `//api`, driven straight
  at the ASGI scope because no HTTP client can express them),
  `POST /api/v1/health` is still `405`, `/api` and `/api/v2/...` are reserved,
  `/docs` and `/openapi.json` are not shadowed **and still redirect from their
  trailing-slash spellings**, the routing table gains no route at all, the
  OpenAPI document gains no path, and the WebSocket still connects;
- the entry document behaves like a file: `If-None-Match` gets `304`, and an
  `index.html` deleted under a running server is a `404` envelope, not a `500`;
- no bundle: the API works, the root URL names `npm run build` and the setting,
  `HEAD /` sends headers only, a deep link is still a `404`, a directory with no
  `index.html` counts as no bundle, and `POST /` is `404` in **both** modes;
- the path: absolute by default, unchanged by `chdir` (asserting the `chdir`
  actually happened), settable from the environment, and an application serving
  the bundle it was *given*.

## Notes / decisions

### The frontend is the router's `default`, not a route

This is the whole feature, and the failure mode is silent. A catch-all route (or
`StaticFiles(html=True)` mounted at `/`) matches `/api/v1/nope` as happily as
`/jobs/01J…`, so every unknown API path — and every client bug that produces one
— would come back `200 text/html` instead of the documented envelope. Nothing
looks broken until something tries to parse it.

Answering "is this the API?" *inside* the handler is not enough either, because
of how Starlette's router (and FastAPI's) dispatches:

```text
full match → partial match (405) → redirect_slashes → default
```

A **full match wins immediately, wherever it is in the table** — being last does
not protect anything. A catch-all route therefore breaks three things at once:
it answers `POST /api/v1/health` itself, so the `405` that route's partial match
exists to produce never happens; it answers `/api/v1/nope` with HTML; and by
matching every unrouted path it makes `redirect_slashes` **dead code**, so
`/docs/` quietly becomes the app instead of redirecting to `/docs`.

The first version of this feature was such a route, with the exclusions moved
into `matches()`. That fixed the first two but not the third, and it left the
property resting on a hand-maintained list of refusals. Installing the frontend
as the router's **`default`** is the same idea taken to its conclusion: the
default is what runs when everything else has declined, so the routing table is
**literally unchanged** — a test asserts `app.routes` is identical before and
after — and every ordering property follows from that rather than from a guard
remembering to say no. The fallback can only add answers for requests that would
otherwise have been `404`.

It still declines three kinds of request, handing them back to the router's own
`not_found` so they keep exactly the response they had: anything under `/api`,
any method other than `GET`/`HEAD` (which is what keeps `POST /` a `404` whether
or not anyone has run `npm run build`), and any non-HTTP scope.

`/api`, not `/api/v1`: a future `/api/v2` should be a routing decision, not a
silent change in which requests turn into HTML, and it is already the boundary
Vite's dev proxy uses, so development and production agree on where the API
ends. A test pins that `main.API_PREFIX` still lives under it.

### The API guard runs on the normalized path

`//api/v1/nope` is what a client emits when it builds `f"{base}/api/v1/jobs"`
with a `base` that ends in a slash. Browsers do not collapse it, no route
matches it, and a guard that compares the *raw* path against `/api` therefore
sees a non-API path and hands it to the frontend — an API call answered
`200 text/html`, which is precisely the failure this design exists to prevent,
reintroduced by a spelling. The first version of this feature had exactly that
hole; it was found in review, and the regression tests now cover `//api/…`,
`///api/…`, `/./api/…`, `/x/../api/…` and `//api`.

Two details worth keeping:

- `posixpath.normpath` alone is not enough. POSIX gives a path beginning with
  **exactly two** slashes implementation-defined meaning, so `normpath("//api")`
  is `"//api"`. Repeated slashes are collapsed first, then normalized.
- No HTTP client can express these spellings — httpx resolves `/./x` and
  `/a/../x` while building the URL and reads a leading `//` as an authority — so
  the tests drive the ASGI scope directly, which is what a socket-level client
  actually sends. (`curl --path-as-is` is the equivalent at the CI end.)

Resolving `..` errs deliberately toward the API: `/x/../api/v1/nope` reserves
rather than serves. Nothing is resolved against the filesystem here; StaticFiles
does its own, stricter lookup and refuses to leave the bundle directory.

### A miss inside the bundle is not a deep link

`/jobs/01J…` is a client-side route and must return `index.html`.
`/assets/index-OLD-HASH.js` is a file that is genuinely not there and must
return `404`. Answering the second with the entry document is how a tab left
open across a rebuild fails: it lazily `import()`s a chunk whose hash has
changed, and the browser reports *"expected a JavaScript module script but the
server responded with a MIME type of text/html"* — an error naming neither the
missing chunk nor the stale tab. The same applies to any `fetch()` of a bundle
file.

`is_navigation()` draws the line on two signals, either sufficient: the client
asked for HTML (`Accept: text/html…`, which every navigation sends and no module
`import()` does), or the last path segment carries no file extension (which
covers `curl` and anything else sending `Accept: */*` for a page). A navigation
to an extension-shaped path like `/reports/2026.05` is rescued by the first; a
module import of a missing chunk is caught by the second.

### The entry document is served as a file, not rebuilt per request

The fallback returns `StaticFiles.get_response("index.html", scope)` rather than
a freshly constructed `FileResponse`. Two things follow. Conditional requests
work — a browser holding the document sends `If-None-Match` and gets `304`,
instead of the whole document plus an `ETag` it can never spend. And deleting
`index.html` from a running server (reachable, because the directory is
inspected once at startup) answers the documented `404` envelope rather than a
`FileNotFoundError` and a `500`.

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
that the asset the built page asks for is served **as JavaScript**, that a chunk
which is not there is a `404`, that an unknown `/api/v1/**` path is still the
JSON envelope **in both the canonical and the double-slash spelling**
(`curl --path-as-is`), and that `/docs/` still redirects.

The content-type assertion is not decoration, and its absence was a real hole in
the first version of this step: while the fallback answered every miss with
`index.html`, `curl -sf "$base$asset"` returned `200` **whether or not the file
existed**, so the step proved only that the URL was root-relative. A rollup
change emitting an entry name absent from disk would have left it green — which
is the exact class of bug the step was added to catch. Checking the media type,
and separately that a known-missing chunk is a `404`, is what makes the claim
true.

It is in the **`e2e` job** because that job already installs both toolchains, so
the marginal cost is one `vite build` and one server start — **measured at 5 s**
on its first run (4 s of build, 1 s to start and check), on a job that is not
the pipeline's critical path — rather than the ~1 min a job of its own would
spend installing Python and Node to do the same thing. Adding Node to the
`backend` job would have slowed the critical path instead.

Measured on that run: `backend` 2 min 59 s, `e2e` 1 min 50 s (of which these two
steps are 5 s), `frontend` 52 s. The pipeline's wall clock is unchanged.

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

After the review fixes, the same server was driven again end to end — upload,
configure, separate (telemetry and progress over the WebSocket), inspect,
playback, deep-link refresh, no console errors — and each fix was checked
against it directly: `curl --path-as-is //api/v1/nope`, `///api/v1/nope` and
`//api` → `404 application/json`; `/assets/no-such-chunk.js` → `404` envelope
while the real hashed asset stays `200 text/javascript`; `/docs/` and
`/openapi.json/` → `307` to the real route; `POST /` → `404`; a deep link with
`If-None-Match` → `304` with an empty body. `//jobs/abc` still returns the app,
so normalizing the guard did not make the frontend pickier.

### Review findings, and what they changed

Code review of the first version found seven issues; all are fixed here and
each has a regression test that fails against that version. Two were behavioural
(`//api/v1/nope` returned `200 text/html`; every `StaticFiles` miss became
`index.html`), and one of the "low" findings mattered more than its label — the
CI step added to catch the asset-URL class of bug **could not catch it**,
because that second behavioural bug made its `curl -sf` succeed for a file that
did not exist. The routing-order reasoning itself was verified correct and is
unchanged in substance; moving from a route to the router's `default` is that
same reasoning applied to the two places a route could not reach
(`redirect_slashes`, and `POST /` differing between the two modes).

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
