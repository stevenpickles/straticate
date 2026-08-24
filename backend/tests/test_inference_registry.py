"""Tests for the catalog model → ``Separator`` registry.

The point of the registry is that the *catalog* decides what a separator
claims: adding a fake model to ``models/catalog.json`` must need no code
change, and no built-in descriptor constant may be consulted on the resolution
path.

Since feature 026 the registry also owns *when* a separator is built. A real
backend reads hundreds of megabytes on a cache miss, so ``aget`` offloads the
build to a worker thread; the tests at the bottom of this file prove the event
loop stays responsive while that happens and that two racing callers build once.
"""

import asyncio
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from straticate.config import Settings
from straticate.errors import ApplicationError
from straticate.inference import (
    FAKE_ARCHITECTURE,
    ROFORMER_ARCHITECTURE,
    FakeSeparator,
    Separator,
    SeparatorRegistry,
    fake_separator_builder,
    separator_info_from_model,
)
from straticate.inference.registry import roformer_separator_builder
from straticate.inference.roformer import RoFormerSeparator
from straticate.models import ModelCatalog
from straticate.models.layout import weights_path
from straticate.schemas import Model
from tests.roformer_fixtures import tiny_catalog_block, write_tiny_weights


def make_catalog_model(model_id: str, **overrides: object) -> Model:
    """A minimal valid :class:`Model`, with ``overrides`` applied."""
    fields: dict[str, object] = {
        "id": model_id,
        "display_name": model_id,
        "architecture": FAKE_ARCHITECTURE,
        "version": "1.0",
        "separation_mode": "vocals",
        "stems": ["vocals", "instrumental"],
        "sample_rate": 44100,
        "capabilities": {"cpu": True},
    }
    fields.update(overrides)
    return Model.model_validate(fields)


@pytest.fixture
def real_models() -> list[Model]:
    """The repository's own catalog entries."""
    return ModelCatalog.from_directory(Settings().models_dir).list_models()


@pytest.fixture
def real_fake_models(real_models: list[Model]) -> list[Model]:
    """The repository's entries served by the fake engine.

    Since feature 026 the catalog also holds a real model, whose separator
    cannot be built without installed weights — so tests about the *fake* engine
    say so rather than iterating everything and hoping.
    """
    return [model for model in real_models if model.architecture == FAKE_ARCHITECTURE]


def test_fake_architecture_model_yields_a_fake_separator_mirroring_the_catalog(
    real_fake_models: list[Model],
) -> None:
    registry = SeparatorRegistry()
    for model in real_fake_models:
        separator = registry.get(model)
        assert isinstance(separator, FakeSeparator)
        info = separator.info
        assert info.model_id == model.id
        assert info.display_name == model.display_name
        assert info.architecture == model.architecture
        assert info.version == model.version
        assert info.separation_mode == model.separation_mode
        assert list(info.stems) == model.stems
        assert info.sample_rate == model.sample_rate


def test_a_catalog_only_model_needs_no_code_change() -> None:
    """A fake model that no built-in descriptor constant knows about still resolves."""
    model = make_catalog_model(
        "fake-six-stem-999",
        display_name="Fake Six Stems",
        version="3.2",
        separation_mode="six_stems",
        stems=["vocals", "drums", "bass", "guitar", "piano", "other"],
        sample_rate=48000,
    )
    separator = SeparatorRegistry().get(model)
    assert isinstance(separator, FakeSeparator)
    assert separator.info == separator_info_from_model(model)
    assert separator.info.stem_count == 6
    assert separator.info.sample_rate == 48000


def test_get_caches_one_instance_per_model(real_fake_models: list[Model]) -> None:
    registry = SeparatorRegistry()
    first, second = real_fake_models[0], real_fake_models[1]

    assert registry.get(first) is registry.get(first)
    assert registry.get(first) is not registry.get(second)


def test_an_unregistered_architecture_is_a_501(real_fake_models: list[Model]) -> None:
    model = make_catalog_model("demucs-hq-001", architecture="demucs")
    registry = SeparatorRegistry()

    with pytest.raises(ApplicationError) as excinfo:
        registry.get(model)

    error = excinfo.value
    assert error.code == "separator_unavailable"
    assert error.status_code == 501
    assert "demucs-hq-001" in error.message
    assert "demucs" in error.message
    assert error.detail == {
        "model_id": "demucs-hq-001",
        "architecture": "demucs",
    }
    # An architecture that does exist is unaffected.
    assert registry.get(real_fake_models[0]) is not None


def test_a_custom_builder_map_is_honoured() -> None:
    built: list[Model] = []

    def build(model: Model) -> Separator:
        built.append(model)
        return FakeSeparator(separator_info_from_model(model), chunk_delay_seconds=0.0)

    registry = SeparatorRegistry({"custom_net": build})
    assert registry.architectures == frozenset({"custom_net"})

    model = make_catalog_model("custom-001", architecture="custom_net")
    separator = registry.get(model)
    assert isinstance(separator, FakeSeparator)
    assert built == [model]

    with pytest.raises(ApplicationError, match="separator implementation"):
        registry.get(make_catalog_model("fake-001"))


def test_register_adds_an_architecture_after_construction() -> None:
    registry = SeparatorRegistry({})
    model = make_catalog_model("late-001", architecture="late_net")

    with pytest.raises(ApplicationError):
        registry.get(model)

    registry.register("late_net", fake_separator_builder(chunk_delay_seconds=0.0))
    assert isinstance(registry.get(model), FakeSeparator)


def test_the_default_registry_covers_the_architectures_this_build_implements() -> None:
    assert SeparatorRegistry().architectures == frozenset(
        {FAKE_ARCHITECTURE, ROFORMER_ARCHITECTURE}
    )


def test_the_fake_builder_passes_its_tuning_through() -> None:
    """Tests inject a zero-delay builder; production keeps the visible defaults."""
    builder = fake_separator_builder(
        chunk_seconds=1.0,
        chunk_delay_seconds=0.0,
        model_load_seconds=0.0,
        device=None,
    )
    separator = builder(make_catalog_model("fake-001"))
    assert isinstance(separator, FakeSeparator)
    assert separator.runtime_stats() is None


# --------------------------------------------------------------------------
# The RoFormer builder: a catalog entry is all it takes
# --------------------------------------------------------------------------


def test_the_roformer_builder_configures_itself_from_the_catalog(tmp_path: Path) -> None:
    """Adding a Mel-Band RoFormer model must be a data edit, not a code change."""
    models_dir = tmp_path / "models"
    write_tiny_weights(weights_path(models_dir, "tiny-vocals-001"))
    block = tiny_catalog_block()

    builder = roformer_separator_builder(
        models_dir=models_dir,
        inference_parameters=lambda model_id: block if model_id == "tiny-vocals-001" else None,
        ffmpeg_timeout_seconds=11.0,
    )
    model = make_catalog_model(
        "tiny-vocals-001",
        architecture=ROFORMER_ARCHITECTURE,
        sample_rate=block["model"]["sample_rate"],
    )

    separator = builder(model)
    assert isinstance(separator, RoFormerSeparator)
    assert separator.info == separator_info_from_model(model)
    assert separator.parameters.chunk_samples == block["inference"]["chunk_size"]
    assert separator.ffmpeg_timeout_seconds == 11.0


def test_the_roformer_builder_reports_missing_weights_as_a_409(tmp_path: Path) -> None:
    builder = roformer_separator_builder(
        models_dir=tmp_path / "models", inference_parameters=lambda _: tiny_catalog_block()
    )
    model = make_catalog_model("tiny-vocals-001", architecture=ROFORMER_ARCHITECTURE)

    with pytest.raises(ApplicationError) as excinfo:
        builder(model)

    assert excinfo.value.code == "model_weights_missing"
    assert excinfo.value.status_code == 409


def test_a_registry_built_without_a_models_directory_says_so(tmp_path: Path) -> None:
    """A wiring mistake is reported, never guessed around."""
    registry = SeparatorRegistry()  # no models_dir, no parameter source
    model = make_catalog_model("tiny-vocals-001", architecture=ROFORMER_ARCHITECTURE)

    with pytest.raises(ApplicationError) as excinfo:
        registry.get(model)

    assert excinfo.value.code == "separator_unavailable"
    assert excinfo.value.status_code == 501
    assert "models directory" in excinfo.value.message
    assert not list(tmp_path.iterdir())


# --------------------------------------------------------------------------
# Building must not block the event loop (feature 029's deferred finding)
# --------------------------------------------------------------------------


class GatedBuilder:
    """A builder that parks inside a worker thread until it is released."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.builds: list[str] = []
        self.threads: set[int] = set()

    def __call__(self, model: Model) -> Separator:
        self.builds.append(model.id)
        self.threads.add(threading.get_ident())
        self.entered.set()
        self.release.wait(timeout=30)
        return FakeSeparator(separator_info_from_model(model), chunk_delay_seconds=0.0)


async def test_aget_builds_off_the_event_loop() -> None:
    """The loop keeps running while a separator loads — proved, not asserted.

    A slow builder is parked in its worker thread; a task scheduled on the loop
    goes on ticking meanwhile. Against the pre-026 code (``registry.get`` called
    directly from ``create_job``) the ticker could not run at all, because the
    build held the only thread the loop had.
    """
    builder = GatedBuilder()
    registry = SeparatorRegistry({"gated": builder})
    model = make_catalog_model("gated-001", architecture="gated")

    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while not builder.release.is_set():
            ticks += 1
            await asyncio.sleep(0)

    ticker = asyncio.create_task(tick())
    build = asyncio.create_task(registry.aget(model))

    await asyncio.to_thread(builder.entered.wait, 30)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert ticks > 0, "the event loop was blocked while the separator was built"

    builder.release.set()
    await ticker
    separator = await build

    assert isinstance(separator, FakeSeparator)
    assert threading.get_ident() not in builder.threads


async def test_aget_builds_once_for_racing_callers() -> None:
    """Two submissions for one model share the instance rather than loading twice."""
    builder = GatedBuilder()
    builder.release.set()
    registry = SeparatorRegistry({"gated": builder})
    model = make_catalog_model("gated-001", architecture="gated")

    first, second = await asyncio.gather(registry.aget(model), registry.aget(model))

    assert first is second
    assert builder.builds == ["gated-001"]


async def test_aget_returns_a_cached_separator_without_suspending() -> None:
    """A cache hit is the common case and must cost nothing at all."""
    registry = SeparatorRegistry()
    model = make_catalog_model("fake-001")
    first = registry.get(model)

    coroutine = registry.aget(model)
    try:
        coroutine.send(None)
    except StopIteration as done:
        assert done.value is first
    else:  # pragma: no cover - would mean the fast path suspended
        coroutine.close()
        pytest.fail("aget suspended on a cache hit")


async def test_aget_surfaces_a_builder_failure_and_does_not_cache_it() -> None:
    attempts = 0

    def build(model: Model) -> Separator:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ApplicationError("model_weights_missing", "no weights", status_code=409)
        return FakeSeparator(separator_info_from_model(model), chunk_delay_seconds=0.0)

    registry = SeparatorRegistry({"flaky": build})
    model = make_catalog_model("flaky-001", architecture="flaky")

    with pytest.raises(ApplicationError) as excinfo:
        await registry.aget(model)
    assert excinfo.value.code == "model_weights_missing"

    # Installing the weights and retrying works: nothing negative was cached.
    assert isinstance(await registry.aget(model), FakeSeparator)
    assert attempts == 2


def test_a_registry_outlives_the_event_loop_that_first_used_it() -> None:
    """A registry is long-lived; an ``asyncio.Lock`` would not be.

    ``asyncio.Lock`` binds to whichever loop first *contends* for it, and it
    stays in the registry afterwards. A build that fails leaves the lock behind
    with nothing cached, so the next contended attempt — from a synchronous
    ``TestClient`` block after an async client on the same app, from a second
    ``asyncio.run``, or from the next test's function-scoped loop — met
    ``RuntimeError: ... is bound to a different event loop`` instead of
    building. The build already runs in a worker thread, so a thread lock is
    what actually guards it, and nothing is ever held across an ``await``.

    Two racing callers per loop are what make this reproducible: an uncontended
    ``asyncio.Lock`` acquire returns on a fast path that never looks at the loop
    at all, which is exactly why the bug is the kind that survives review.
    """
    attempts = 0
    fail_until = 2
    at_the_gate = threading.Event()
    proceed = threading.Event()

    def build(model: Model) -> Separator:
        nonlocal attempts
        attempts += 1
        at_the_gate.set()
        proceed.wait(timeout=30)
        if attempts <= fail_until:
            raise ApplicationError("model_weights_missing", "no weights", status_code=409)
        return FakeSeparator(separator_info_from_model(model), chunk_delay_seconds=0.0)

    registry = SeparatorRegistry({"flaky": build})
    model = make_catalog_model("flaky-001", architecture="flaky")

    async def two_racing_callers() -> Sequence[Separator | BaseException]:
        at_the_gate.clear()
        proceed.clear()
        first = asyncio.create_task(registry.aget(model))
        # The second caller only contends if the first is already building.
        await asyncio.to_thread(at_the_gate.wait, 30)
        second = asyncio.create_task(registry.aget(model))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        proceed.set()
        return await asyncio.gather(first, second, return_exceptions=True)

    # Loop one: both callers contend, both fail, nothing is cached — and a
    # per-model asyncio.Lock would now be bound to this loop, which is gone.
    outcomes = asyncio.run(two_racing_callers())
    assert [type(outcome) for outcome in outcomes] == [ApplicationError, ApplicationError]
    assert attempts == 2

    # Loop two: an entirely separate event loop. Nothing from the first may
    # leak into it.
    fail_until = 0
    outcomes = asyncio.run(two_racing_callers())
    assert all(isinstance(outcome, FakeSeparator) for outcome in outcomes), outcomes
    assert outcomes[0] is outcomes[1], "racing callers must share one instance"

    # And a third loop still gets the cached instance.
    assert asyncio.run(registry.aget(model)) is outcomes[0]


def test_the_lock_for_a_model_is_created_once_and_reused() -> None:
    """A miss must not construct and discard a fresh lock every time."""
    registry = SeparatorRegistry({})
    model = make_catalog_model("late-001", architecture="late_net")

    # Three misses (no builder registered), then a successful build.
    for _ in range(3):
        with pytest.raises(ApplicationError):
            asyncio.run(registry.aget(model))
    registry.register("late_net", fake_separator_builder(chunk_delay_seconds=0.0))
    assert isinstance(asyncio.run(registry.aget(model)), FakeSeparator)


def test_get_and_aget_share_one_cache_and_one_lock() -> None:
    """Both accessors are the same build-once path, reached differently."""
    builds: list[str] = []

    def build(model: Model) -> Separator:
        builds.append(model.id)
        return FakeSeparator(separator_info_from_model(model), chunk_delay_seconds=0.0)

    registry = SeparatorRegistry({"counted": build})
    model = make_catalog_model("counted-001", architecture="counted")

    synchronous = registry.get(model)
    assert asyncio.run(registry.aget(model)) is synchronous
    assert registry.get(model) is synchronous
    assert builds == ["counted-001"]
