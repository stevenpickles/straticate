"""Restart harness: what a *second* process finds in a data directory.

Feature 057 made job records durable, and the only honest way to test that is
to stop asking one application about itself. The harness runs an application
over a temporary ``data_dir``, drives it to whatever state the test is about,
lets its lifespan exit, drops it — and then builds a **new** application over
the same directory and asserts through that one's client. Nothing is carried
across in memory: the second application shares only the bytes on disk.

Use it like this::

    first = build_app(tmp_path)             # tests.test_api_jobs.build_app
    async with running_app(first) as client:
        ...                                 # drive it, event-driven
    del first                               # nothing survives but the files

    async with running_app(build_app(tmp_path)) as client:
        assert (await client.get(f"/api/v1/jobs/{job_id}")).status_code == 200

Three rules this harness exists to keep, for the features that will reuse it
(deletion, pruning, disk usage — 058-060):

- **The second application must be built from scratch**, not merely re-entered.
  Re-entering one app object's lifespan twice exercises the *per-lifespan*
  wiring (a fresh manager, hub and sampler each cycle, which ``main.lifespan``
  documents) but keeps every object ``create_app`` built; only a second
  ``build_app`` proves that what came back came off the disk.
- **The first application's lifespan must have exited** before the second one
  starts. Terminal records are persisted as they happen rather than at
  shutdown, so this is not about flushing — it is about never having two live
  job managers over one data directory.
- **No sleeps.** Drive the first application with the same event-driven waits
  the rest of the suite uses (``EventRecorder.wait_for_terminal``); the restart
  itself is synchronous once the lifespan has exited.

:func:`write_job_record` and :func:`read_job_record` are the crash simulator:
they put a record on disk that no process ever wrote in the ordinary way (a
job left ``separating`` by a kill -9) and read back what the next boot made of
it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx2
from fastapi import FastAPI

from straticate.jobs.layout import job_record_path


@asynccontextmanager
async def running_app(app: FastAPI) -> AsyncGenerator[httpx2.AsyncClient]:
    """Run ``app``'s lifespan on this test's event loop and yield a client.

    The lifespan is what creates and starts the job manager (and, since feature
    057, what loads the durable records), so a restart test must enter it —
    the plain ``client`` fixture does not, and would see no manager at all.
    """
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def write_job_record(data_dir: Path, record: dict[str, Any]) -> Path:
    """Write ``record`` as ``{data_dir}/jobs/{record["id"]}/job.json``.

    The crash simulator: a record written straight to disk, in whatever state
    the test names, without a job manager ever having produced it. Returns the
    path so a test can corrupt or inspect it afterwards.
    """
    path = job_record_path(data_dir, cast(str, record["id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def read_job_record(data_dir: Path, job_id: str) -> dict[str, Any]:
    """Read the job record on disk — what the *next* process would load."""
    path = job_record_path(data_dir, job_id)
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
