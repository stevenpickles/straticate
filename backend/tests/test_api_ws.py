"""Tests for ``WS /api/v1/ws`` against a real application and job manager.

These exercise the endpoint end to end with Starlette's ``TestClient``. The
app (and therefore the job manager and event hub) runs on the test client's
portal loop, so every manager call is marshalled onto that loop with
``portal.call`` — matching the manager's single-loop concurrency contract.
Synchronization is by reading from the socket and by ``asyncio.Event`` gates;
never by sleeping.
"""

import asyncio
from collections.abc import Iterator
from functools import partial
from typing import Any, cast

import pytest
from anyio.from_thread import BlockingPortal
from fastapi import FastAPI
from starlette.testclient import TestClient, WebSocketTestSession

from straticate.jobs import EventHub, JobContext, JobExecutor, JobManager
from straticate.jobs.hub import CLOSE_GOING_AWAY
from straticate.main import create_app
from straticate.schemas.jobs import (
    Job,
    JobState,
    SeparationConfiguration,
    SeparationResult,
    SeparationResultMetrics,
    Stem,
)

WS_URL = "/api/v1/ws"


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A ``TestClient`` with the application lifespan running."""
    with TestClient(create_app()) as test_client:
        yield test_client


def portal_of(client: TestClient) -> BlockingPortal:
    portal = client.portal
    assert portal is not None
    return portal


def app_of(client: TestClient) -> FastAPI:
    return cast(FastAPI, client.app)


def manager_of(client: TestClient) -> JobManager:
    return cast(JobManager, app_of(client).state.job_manager)


def hub_of(client: TestClient) -> EventHub:
    return cast(EventHub, app_of(client).state.event_hub)


def make_configuration() -> SeparationConfiguration:
    return SeparationConfiguration(
        audio_id="01AUDIO0000000000000000000",
        mode_id="vocals",
        quality_id="high_quality",
        device_id=None,
    )


def make_result(job_id: str) -> SeparationResult:
    return SeparationResult(
        job_id=job_id,
        model_id="model-1",
        stems=[Stem(name="vocals", duration_seconds=60.0, sample_rate_hz=44100, channels=2)],
        metrics=SeparationResultMetrics(processing_seconds=7.5, realtime_factor=8.0),
    )


def scripted_executor(gate: asyncio.Event | None = None) -> JobExecutor:
    """An executor emitting stage + progress events, optionally gated."""

    async def executor(job: Job, context: JobContext) -> SeparationResult:
        context.set_stage(JobState.SEPARATING)
        if gate is not None:
            await gate.wait()
        context.report_progress(1.0, 4, 4, 60.0, 60.0)
        return make_result(job.id)

    return executor


def submit(client: TestClient, executor: JobExecutor) -> Job:
    """Submit a job on the application's event loop."""
    return portal_of(client).call(
        partial(manager_of(client).submit, make_configuration(), executor, model_id="model-1")
    )


def receive_event(session: WebSocketTestSession) -> dict[str, Any]:
    return cast(dict[str, Any], session.receive_json())


def receive_until(session: WebSocketTestSession, event_type: str) -> list[dict[str, Any]]:
    """Read events until (and including) the first one of ``event_type``."""
    events: list[dict[str, Any]] = []
    while True:
        event = receive_event(session)
        events.append(event)
        if event["type"] == event_type:
            return events


# -- happy path -------------------------------------------------------------


def test_client_receives_the_full_event_sequence_in_order(client: TestClient) -> None:
    with client.websocket_connect(WS_URL) as session:
        job = submit(client, scripted_executor())
        events = receive_until(session, "job_completed")

    assert [event["type"] for event in events] == [
        "job_created",
        "job_started",
        "job_stage_changed",
        "job_stage_changed",
        "job_progress",
        "job_completed",
    ]
    assert {cast(str, event["job_id"]) for event in events} == {job.id}

    created = cast(dict[str, Any], events[0]["job"])
    assert created["id"] == job.id
    assert created["state"] == "queued"
    assert events[1]["started_at"] is not None
    assert [cast(str, e["stage"]) for e in events[2:4]] == ["preparing", "separating"]

    progress = events[4]
    assert progress["type"] == "job_progress"
    assert progress["job_id"] == job.id
    assert progress["stage"] == "separating"
    assert progress["progress"] == 1.0
    assert progress["chunks_completed"] == 4
    assert progress["chunks_total"] == 4
    assert progress["audio_total_seconds"] == 60.0
    assert isinstance(progress["elapsed_seconds"], float)

    completed = events[5]
    assert completed["type"] == "job_completed"
    assert completed["job_id"] == job.id
    result = cast(dict[str, Any], completed["result"])
    assert result["job_id"] == job.id
    assert result["model_id"] == "model-1"
    assert result["metrics"] == {"processing_seconds": 7.5, "realtime_factor": 8.0}


def test_two_clients_both_receive_every_event(client: TestClient) -> None:
    with (
        client.websocket_connect(WS_URL) as first,
        client.websocket_connect(WS_URL) as second,
    ):
        assert hub_of(client).connection_count == 2
        submit(client, scripted_executor())
        first_events = receive_until(first, "job_completed")
        second_events = receive_until(second, "job_completed")

    assert first_events == second_events


def test_client_disconnecting_mid_job_breaks_neither_the_job_nor_the_peer(
    client: TestClient,
) -> None:
    gate = asyncio.Event()
    with client.websocket_connect(WS_URL) as survivor:
        with client.websocket_connect(WS_URL) as leaver:
            job = submit(client, scripted_executor(gate))
            # Both clients follow the job up to the `separating` stage change.
            for session in (leaver, survivor):
                stages = receive_until(session, "job_stage_changed")
                assert stages[-1]["stage"] == "preparing"
                assert receive_event(session)["stage"] == "separating"
        # `leaver` is gone; the gated job now finishes.
        portal_of(client).call(gate.set)
        remaining = receive_until(survivor, "job_completed")

    assert [event["type"] for event in remaining] == ["job_progress", "job_completed"]
    assert manager_of(client).get(job.id).state is JobState.COMPLETED


# -- inbound messages -------------------------------------------------------


def test_inbound_client_messages_are_ignored(client: TestClient) -> None:
    with client.websocket_connect(WS_URL) as session:
        session.send_text("hello")
        session.send_json({"type": "subscribe", "job_id": "01JOB"})
        session.send_bytes(b"\x00\x01\x02")

        submit(client, scripted_executor())
        events = receive_until(session, "job_completed")

    assert events[0]["type"] == "job_created"
    assert events[-1]["type"] == "job_completed"


# -- shutdown ---------------------------------------------------------------


def test_hub_shutdown_closes_the_connection(client: TestClient) -> None:
    with client.websocket_connect(WS_URL) as session:
        hub = hub_of(client)
        assert hub.connection_count == 1
        portal_of(client).call(hub.aclose)

        message = session.receive()
        assert message["type"] == "websocket.close"
        assert message["code"] == CLOSE_GOING_AWAY
    assert hub.connection_count == 0


def test_connecting_after_hub_shutdown_closes_cleanly(client: TestClient) -> None:
    """A closed hub is "going away", not an unhandled error on the socket.

    The application's catch-all HTTP exception handler cannot answer a
    WebSocket scope, so the endpoint must handle the closed hub itself.
    """
    hub = hub_of(client)
    portal_of(client).call(hub.aclose)

    with client.websocket_connect(WS_URL) as session:
        message = session.receive()
        assert message["type"] == "websocket.close"
        assert message["code"] == CLOSE_GOING_AWAY
    assert hub.connection_count == 0


def test_application_shutdown_leaves_no_registered_clients() -> None:
    app = create_app()
    with TestClient(app) as test_client:
        hub = cast(EventHub, app.state.event_hub)
        with test_client.websocket_connect(WS_URL):
            assert hub.connection_count == 1
    assert hub.is_closed
    assert hub.connection_count == 0


def test_each_lifespan_cycle_gets_a_fresh_hub_wired_to_its_manager() -> None:
    app = create_app()
    hubs: list[EventHub] = []
    for _ in range(2):
        with TestClient(app) as test_client:
            hub = cast(EventHub, app.state.event_hub)
            hubs.append(hub)
            with test_client.websocket_connect(WS_URL) as session:
                submit(test_client, scripted_executor())
                events = receive_until(session, "job_completed")
            assert events[0]["type"] == "job_created"
    first, second = hubs
    assert first is not second
    assert first.is_closed and second.is_closed
