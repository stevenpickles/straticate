"""The application runs without PyTorch (feature 034).

Feature 018 designed torch as an *optional runtime probe*: its absence is "a
normal, first-class, tested path, not an error". Feature 026 made it a hard
dependency of real *inference*, which is correct — and accidentally made it a
hard dependency of *importing the application*, because
``inference/registry.py`` imported the RoFormer builder at module scope. This
module pins the property that puts back: with torch absent the app imports,
``create_app()`` succeeds, ``/health``, ``/models`` and ``/separation-modes``
serve, a fake-separator job runs end to end, and only a job for a torch-backed
model is refused — with the ``separator_unavailable`` (501) envelope the
registry has always produced for a model this build cannot run.

**How torch's absence is simulated.** The same way 018 does it: nothing is
uninstalled. :func:`torch_absent` evicts ``torch`` (and the two Straticate
modules that import it) from ``sys.modules``, clears the values the lazy
re-exports have memoised, and installs a ``sys.meta_path`` finder that refuses
to import ``torch`` — then puts every one of those back on the way out. Inside
that window the interpreter behaves exactly as one with no torch installed, and
outside it the rest of the suite is untouched. The complementary check, that
``straticate.main`` does not import torch *at all*, is made in a subprocess,
because inside this session other test modules have legitimately imported it.
"""

import asyncio
import importlib
import json
import shutil
import subprocess
import sys
from collections.abc import Generator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import httpx2
import pytest
from fastapi import FastAPI

from straticate.config import Settings
from straticate.errors import ApplicationError
from straticate.inference import (
    DEMUCS_ARCHITECTURE,
    ROFORMER_ARCHITECTURE,
    SeparatorRegistry,
    default_separator_builders,
)
from straticate.jobs import JobEvent, JobManager
from straticate.main import create_app
from straticate.models import CATALOG_FILENAME, ModelCatalog
from straticate.models.layout import weights_path
from straticate.schemas import AudioFile, AudioMetadata, Model
from straticate.schemas.events import JobCancelledEvent, JobCompletedEvent, JobFailedEvent
from straticate.system import DeviceDetector
from tests.audio_fixtures import write_tone_wav
from tests.conftest import fake_quality_id
from tests.roformer_fixtures import TINY_SAMPLE_RATE, tiny_catalog_block, write_tiny_weights

API = "/api/v1"
WAIT_TIMEOUT = 30.0

TORCH_BACKED_MODEL_ID = "tiny-vocals-001"

LAZY_EXPORTS: dict[str, tuple[str, ...]] = {
    "straticate.inference.roformer": (
        "DEFAULT_CHUNK_SAMPLES",
        "DEFAULT_NUM_OVERLAP",
        "NvmlProbe",
        "RoFormerParameters",
        "RoFormerSeparator",
    ),
    "straticate.inference.demucs": (
        "DEFAULT_OVERLAP",
        "DEFAULT_TRANSITION_POWER",
        "DemucsParameters",
        "DemucsSeparator",
        "NvmlProbe",
        "load_checkpoint_package",
    ),
    "straticate.inference": (
        "RoFormerParameters",
        "RoFormerSeparator",
        "DemucsParameters",
        "DemucsSeparator",
    ),
}
"""What each package resolves lazily, and therefore memoises in its globals.

Spelled out rather than read from the packages' private ``_LAZY_*`` sets, so a
name added there without being considered here shows up as a *test* that stops
proving anything — which
:func:`test_the_lazy_re_exports_all_fail_without_torch` then catches, because it
walks these lists and requires every one of them to raise.
"""

BACKEND_MODULE = "straticate.inference.roformer.separator"
"""The RoFormer implementation module — one of the two that import torch."""

DEMUCS_BACKEND_MODULE = "straticate.inference.demucs.separator"
"""The Hybrid Transformer Demucs implementation module (feature 028)."""

BACKEND_MODULES = (BACKEND_MODULE, DEMUCS_BACKEND_MODULE)
"""Every Straticate module that imports torch at module scope."""

EVICTED_PREFIXES = ("torch", *BACKEND_MODULES)
"""Modules that must be re-imported (and so re-blocked) inside the window.

``torch`` itself, and the Straticate modules that import it at module scope.
Each backend's ``vendor/`` package is reached only through its implementation
module, and each ``architecture.py`` is deliberately *not* here: those hold the
architecture names, import nothing, and are what let the registry key its
builder map without torch.
"""


# --------------------------------------------------------------------------
# Simulating an absent module
# --------------------------------------------------------------------------


class MissingModuleFinder:
    """A ``sys.meta_path`` finder that refuses one package and its submodules.

    Raising from ``find_spec`` rather than returning ``None`` is deliberate: a
    later finder must not get the chance to succeed, and the exception the
    caller sees is the ``ModuleNotFoundError`` a genuinely uninstalled package
    produces, message and ``name`` included.
    """

    def __init__(self, blocked: str) -> None:
        self.blocked = blocked

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if fullname == self.blocked or fullname.startswith(f"{self.blocked}."):
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


@contextmanager
def unimportable(blocked: str, evicted_prefixes: tuple[str, ...]) -> Generator[None]:
    """Make ``blocked`` unimportable, and force ``evicted_prefixes`` to re-import.

    Nothing is uninstalled and nothing is left changed: the evicted modules,
    the meta-path finder and the memoised lazy exports are all restored on the
    way out, so a test that needs the real module *before* entering the window
    (to build a synthetic checkpoint, say) and again after it is unaffected.
    """
    evicted = {
        name: module
        for name, module in sys.modules.items()
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in evicted_prefixes)
    }
    memoised: dict[tuple[str, str], object] = {}
    for module_name, names in LAZY_EXPORTS.items():
        module = sys.modules[module_name]
        for name in names:
            if name in vars(module):
                memoised[(module_name, name)] = vars(module).pop(name)

    finder = MissingModuleFinder(blocked)
    for name in evicted:
        del sys.modules[name]
    sys.meta_path.insert(0, finder)
    importlib.invalidate_caches()
    try:
        yield
    finally:
        sys.meta_path.remove(finder)
        for name in list(sys.modules):
            if any(name == prefix or name.startswith(f"{prefix}.") for prefix in evicted_prefixes):
                del sys.modules[name]
        sys.modules.update(evicted)
        for (module_name, name), value in memoised.items():
            setattr(sys.modules[module_name], name, value)
        importlib.invalidate_caches()


def torch_absent() -> AbstractContextManager[None]:
    """PyTorch is not installed at all — the ordinary deployment without the extra."""
    return unimportable("torch", EVICTED_PREFIXES)


def backend_import_broken() -> AbstractContextManager[None]:
    """PyTorch **is** installed; the backend module fails to import anyway.

    The other deployment fault: an incompatible ``einops`` or
    ``rotary-embedding-torch`` inside the vendored architecture, a corrupted
    wheel, a rename in ``separator.py``. Only the implementation module is
    blocked here — ``torch`` itself stays importable, which is exactly what
    :func:`straticate.inference.registry._torch_is_installed` looks for.
    """
    return unimportable(BACKEND_MODULE, (BACKEND_MODULE,))


def test_the_simulation_really_hides_torch() -> None:
    """The window is only worth anything if ``import torch`` fails inside it."""
    assert importlib.import_module("torch") is not None

    with torch_absent(), pytest.raises(ModuleNotFoundError):
        importlib.import_module("torch")

    # And the interpreter is exactly as it was.
    assert importlib.import_module("torch") is not None


def test_the_lazy_re_exports_all_fail_without_torch() -> None:
    """Every torch-backed re-export must genuinely re-import, not serve a cache."""
    for module_name, names in LAZY_EXPORTS.items():
        for name in names:
            assert getattr(importlib.import_module(module_name), name) is not None

    with torch_absent():
        for module_name, names in LAZY_EXPORTS.items():
            module = importlib.import_module(module_name)
            for name in names:
                with pytest.raises(ImportError):
                    getattr(module, name)


def test_an_unknown_export_is_still_an_attribute_error() -> None:
    """The lazy layer must not turn a typo into an ``ImportError``."""
    for module_name in LAZY_EXPORTS:
        with pytest.raises(AttributeError):
            getattr(importlib.import_module(module_name), "NoSuchName")  # noqa: B009


# --------------------------------------------------------------------------
# The lazy layer must stay invisible to the type checker
# --------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[1]
"""``backend/`` — pyright is run from here so it finds ``[tool.pyright]``."""

PYRIGHT_PROBE = """from straticate.inference import SeparatorInfo
from straticate.inference import SeparaterInfo
from straticate.inference.roformer import RoFormerSeparator
from straticate.inference.roformer import RoFormerSeperator
from straticate.inference.demucs import DemucsSeparator
from straticate.inference.demucs import DemuxSeparator

_ = (
    SeparatorInfo,
    SeparaterInfo,
    RoFormerSeparator,
    RoFormerSeperator,
    DemucsSeparator,
    DemuxSeparator,
)
"""
"""One real export and one misspelling of it, per lazy package.

A module-level ``__getattr__`` that pyright can see makes *every* attribute of
its package resolve, so all six of these type-check and the misspellings ship.
"""

REAL_NAMES = ("SeparatorInfo", "RoFormerSeparator", "DemucsSeparator")
TYPO_NAMES = ("SeparaterInfo", "RoFormerSeperator", "DemuxSeparator")


def pyright_executable() -> str | None:
    """Locate pyright: on ``PATH`` under ``uv run``, else beside the interpreter."""
    found = shutil.which("pyright")
    if found is not None:
        return found
    for candidate in (
        Path(sys.executable).parent / "pyright",
        Path(sys.executable).parent / "pyright.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return None


def test_the_lazy_layer_is_invisible_to_the_type_checker(tmp_path: Path) -> None:
    """The ``if not TYPE_CHECKING`` guard, pinned by running pyright.

    This is the one property no runtime assertion can reach: guarded and
    unguarded behave identically at run time, and differ only in what the type
    checker sees. Without the guard pyright resolves **any** attribute of
    ``straticate.inference`` and ``straticate.inference.roformer`` through
    ``__getattr__``'s return type, so a typo — or an export someone deletes —
    stops being an error in the two packages the whole application imports
    from. Measured on this checkout: 0 errors without the guard, 4 with it.

    Deleting the guard makes this test fail, which is the entire point of it.
    """
    pyright = pyright_executable()
    if pyright is None:  # pragma: no cover - pyright is a declared dev dependency
        pytest.skip("pyright is not installed")

    probe = tmp_path / "lazy_export_probe.py"
    probe.write_text(PYRIGHT_PROBE, encoding="utf-8")

    completed = subprocess.run(
        [pyright, "--outputjson", str(probe)],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    report = cast(dict[str, Any], json.loads(completed.stdout))
    unresolved = [
        cast(str, diagnostic["message"])
        for diagnostic in cast(list[dict[str, Any]], report["generalDiagnostics"])
        if diagnostic.get("rule") == "reportAttributeAccessIssue"
    ]

    for typo in TYPO_NAMES:
        assert any(typo in message for message in unresolved), (
            f"pyright accepted {typo!r}: the module-level __getattr__ is visible to the "
            f"type checker, so every attribute of these packages now resolves. "
            f"Restore the `if not TYPE_CHECKING:` guard. Diagnostics: {unresolved}"
        )
    for real in REAL_NAMES:
        assert not any(real in message for message in unresolved), (
            f"pyright rejected the genuine export {real!r}: {unresolved}"
        )


# --------------------------------------------------------------------------
# The import graph itself
# --------------------------------------------------------------------------


def run_python(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_importing_the_application_does_not_import_torch() -> None:
    """The property, stated in a fresh interpreter.

    This is the regression feature 030 reported: ``straticate.main`` →
    ``inference/registry.py`` → ``inference/roformer/`` → ``torch``, at import.
    A subprocess is the only honest place to assert it, because torch *is*
    installed for this job and other test modules have already imported it.
    """
    result = run_python(
        "import sys; import straticate.main as main; "
        "assert main.create_app() is not None; "
        "sys.exit(1 if 'torch' in sys.modules else 0)"
    )
    assert result.returncode == 0, result.stderr or "straticate.main imported torch"


def test_the_roformer_package_names_its_architecture_without_torch() -> None:
    """The registry keys its builder map by this name; getting it must be free."""
    result = run_python(
        "import sys; from straticate.inference.roformer import ROFORMER_ARCHITECTURE; "
        "assert ROFORMER_ARCHITECTURE == 'mel_band_roformer'; "
        "sys.exit(1 if 'torch' in sys.modules else 0)"
    )
    assert result.returncode == 0, result.stderr or "the roformer package imported torch"


def test_the_demucs_package_names_its_architecture_without_torch() -> None:
    """Same property for feature 028's backend, and for the same reason."""
    result = run_python(
        "import sys; from straticate.inference.demucs import DEMUCS_ARCHITECTURE; "
        "assert DEMUCS_ARCHITECTURE == 'htdemucs'; "
        "sys.exit(1 if 'torch' in sys.modules else 0)"
    )
    assert result.returncode == 0, result.stderr or "the demucs package imported torch"


def test_building_the_default_registry_imports_no_backend() -> None:
    """Registering a builder must not resolve it (that is what made torch mandatory)."""
    result = run_python(
        "import sys; from straticate.inference import SeparatorRegistry; "
        "registry = SeparatorRegistry(); "
        "assert registry.architectures == frozenset({'fake', 'mel_band_roformer', 'htdemucs'}); "
        "sys.exit(1 if 'torch' in sys.modules else 0)"
    )
    assert result.returncode == 0, result.stderr or "building the registry imported torch"


# --------------------------------------------------------------------------
# The registry, with the backend unavailable
# --------------------------------------------------------------------------


def catalog_model(model_id: str, **overrides: object) -> Model:
    fields: dict[str, object] = {
        "id": model_id,
        "display_name": model_id,
        "architecture": ROFORMER_ARCHITECTURE,
        "version": "1.0",
        "separation_mode": "vocals",
        "stems": ["vocals", "instrumental"],
        "sample_rate": TINY_SAMPLE_RATE,
        "capabilities": {"cpu": True},
    }
    fields.update(overrides)
    return Model.model_validate(fields)


def wired_registry(models_dir: Path) -> SeparatorRegistry:
    """A registry wired exactly as :func:`straticate.main.create_app` wires one."""
    return SeparatorRegistry(
        default_separator_builders(
            models_dir=models_dir,
            inference_parameters=lambda _: tiny_catalog_block(),
        )
    )


def test_a_torch_backed_model_is_the_existing_501(tmp_path: Path) -> None:
    """Same code, same status, same ``detail`` as an unimplemented architecture.

    Feature 034 deliberately added no new error code: from the client's side
    "this build has no implementation for that model" is one fact, however the
    server arrived at it.
    """
    models_dir = tmp_path / "models"
    write_tiny_weights(weights_path(models_dir, TORCH_BACKED_MODEL_ID))
    registry = wired_registry(models_dir)
    model = catalog_model(TORCH_BACKED_MODEL_ID)

    with torch_absent(), pytest.raises(ApplicationError) as excinfo:
        registry.get(model)

    error = excinfo.value
    assert error.code == "separator_unavailable"
    assert error.status_code == 501
    assert error.message == (
        f"No separator implementation is available for model {TORCH_BACKED_MODEL_ID!r} "
        f"(architecture {ROFORMER_ARCHITECTURE!r})."
    )
    assert error.detail == {
        "model_id": TORCH_BACKED_MODEL_ID,
        "architecture": ROFORMER_ARCHITECTURE,
    }


def test_the_501_is_indistinguishable_from_an_unimplemented_architecture(tmp_path: Path) -> None:
    """Pinned as an equality, so the two can never drift apart unnoticed."""
    registry = wired_registry(tmp_path / "models")
    absent_backend = catalog_model("a-001")
    no_such_architecture = catalog_model("a-001", architecture="demucs")

    with torch_absent(), pytest.raises(ApplicationError) as missing_torch:
        registry.get(absent_backend)
    with pytest.raises(ApplicationError) as unimplemented:
        SeparatorRegistry({}).get(no_such_architecture)

    assert missing_torch.value.code == unimplemented.value.code
    assert missing_torch.value.status_code == unimplemented.value.status_code
    assert set(missing_torch.value.detail) == set(unimplemented.value.detail)
    assert missing_torch.value.message.replace(ROFORMER_ARCHITECTURE, "demucs") == (
        unimplemented.value.message
    )


def test_the_missing_package_is_named_in_the_log_not_in_the_envelope(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An operator needs the diagnosis; the browser needs only the refusal."""
    registry = wired_registry(tmp_path / "models")
    model = catalog_model(TORCH_BACKED_MODEL_ID)

    with (
        caplog.at_level("WARNING", logger="straticate.inference.registry"),
        torch_absent(),
        pytest.raises(ApplicationError) as excinfo,
    ):
        registry.get(model)

    assert "torch" not in excinfo.value.message
    assert "torch" not in json.dumps(excinfo.value.detail)
    assert any(
        "torch" in record.getMessage() and TORCH_BACKED_MODEL_ID in record.getMessage()
        for record in caplog.records
    ), caplog.text


def test_a_broken_installation_is_not_reported_as_a_missing_one(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The second deployment fault, told apart from the first — in the log.

    An operator whose ``--extra torch`` install is present but broken (an
    incompatible ``einops``, a corrupted wheel, a rename in ``separator.py``)
    must not be advised to run the command they have already run. The envelope
    is deliberately the same 501 either way.
    """
    registry = wired_registry(tmp_path / "models")
    model = catalog_model(TORCH_BACKED_MODEL_ID)

    with (
        caplog.at_level("WARNING", logger="straticate.inference.registry"),
        backend_import_broken(),
        pytest.raises(ApplicationError) as excinfo,
    ):
        registry.get(model)

    assert excinfo.value.code == "separator_unavailable"
    assert excinfo.value.status_code == 501

    logged = caplog.text
    assert "uv sync --extra torch" not in logged, logged
    assert "installed but" in logged and "failed to import" in logged, logged
    assert TORCH_BACKED_MODEL_ID in logged


def test_a_missing_installation_is_the_one_that_gets_the_install_command(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The inverse of the test above: the advice appears exactly when it helps."""
    registry = wired_registry(tmp_path / "models")
    model = catalog_model(TORCH_BACKED_MODEL_ID)

    with (
        caplog.at_level("WARNING", logger="straticate.inference.registry"),
        torch_absent(),
        pytest.raises(ApplicationError),
    ):
        registry.get(model)

    logged = caplog.text
    assert "uv sync --extra torch" in logged, logged
    assert "installed but" not in logged, logged


def test_the_failure_is_not_cached_so_installing_torch_fixes_it(tmp_path: Path) -> None:
    """The inverse: with torch present the very same registry builds the separator."""
    models_dir = tmp_path / "models"
    write_tiny_weights(weights_path(models_dir, TORCH_BACKED_MODEL_ID))
    registry = wired_registry(models_dir)
    model = catalog_model(TORCH_BACKED_MODEL_ID)

    with torch_absent(), pytest.raises(ApplicationError):
        registry.get(model)

    separator = registry.get(model)
    assert type(separator).__name__ == "RoFormerSeparator"
    assert separator.info.model_id == TORCH_BACKED_MODEL_ID
    assert registry.get(model) is separator


def test_the_fake_engine_is_untouched_by_the_missing_backend() -> None:
    """A registry that cannot build one architecture still builds the others."""
    registry = SeparatorRegistry()
    model = catalog_model("fake-001", architecture="fake", sample_rate=44100)

    with torch_absent():
        separator = registry.get(model)

    assert type(separator).__name__ == "FakeSeparator"


# --------------------------------------------------------------------------
# The application, with the backend unavailable
# --------------------------------------------------------------------------


class TerminalWatcher:
    """Records job events and lets a test await a job's terminal one."""

    def __init__(self) -> None:
        self.events: list[JobEvent] = []
        self._changed = asyncio.Event()

    def __call__(self, event: JobEvent) -> None:
        self.events.append(event)
        self._changed.set()

    async def terminal(self, job_id: str) -> JobEvent:
        index = 0
        while True:
            while index < len(self.events):
                event = self.events[index]
                index += 1
                if event.job_id == job_id and isinstance(
                    event, JobCompletedEvent | JobCancelledEvent | JobFailedEvent
                ):
                    return event
            self._changed.clear()
            await asyncio.wait_for(self._changed.wait(), timeout=WAIT_TIMEOUT)


def build_app(data_dir: Path, *, models_dir: Path | None = None) -> FastAPI:
    """A real application, isolated to ``data_dir``, with deterministic devices.

    The separator registry is the one :func:`~straticate.main.create_app` built
    — the *default* builders, RoFormer included — because the point of these
    tests is that that registry is constructible and usable without torch.
    """
    settings = (
        Settings(data_dir=data_dir)
        if models_dir is None
        else Settings(data_dir=data_dir, models_dir=models_dir)
    )
    app = create_app(settings)
    app.state.device_detector = DeviceDetector(probes=[])  # CPU only, deterministic
    return app


def register_audio(app: FastAPI, *, seconds: float = 0.4) -> str:
    """Write a real tone WAV into the app's audio store and register it."""
    store = app.state.audio_store
    audio_id = cast(str, store.new_id())
    path = cast(Path, store.prepare_original_path(audio_id, "song.wav"))
    write_tone_wav(path, seconds=seconds)
    store.register(
        AudioFile(
            id=audio_id,
            filename="song.wav",
            size_bytes=path.stat().st_size,
            uploaded_at=datetime.now(UTC),
            metadata=AudioMetadata(
                duration_seconds=seconds,
                container="wav",
                codec="pcm_s16le",
                channels=2,
                sample_rate_hz=44100,
                bit_depth=16,
                bit_rate_bps=1411000,
            ),
        )
    )
    return audio_id


def write_catalog(models: list[dict[str, Any]], models_dir: Path) -> Path:
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / CATALOG_FILENAME).write_text(
        json.dumps({"catalog_version": 1, "models": models}), encoding="utf-8"
    )
    return models_dir


def torch_backed_entry() -> dict[str, Any]:
    """A catalog entry for the real architecture, tuned tiny for tests."""
    return {
        "schema_version": 1,
        "id": TORCH_BACKED_MODEL_ID,
        "display_name": "Tiny Vocals",
        "architecture": ROFORMER_ARCHITECTURE,
        "version": "test",
        "separation_mode": "vocals",
        "stems": ["vocals", "instrumental"],
        "sample_rate": TINY_SAMPLE_RATE,
        "capabilities": {"cuda": True, "cpu": True},
        "default_inference_parameters": tiny_catalog_block(),
    }


def test_create_app_succeeds_without_torch(tmp_path: Path) -> None:
    with torch_absent():
        app = build_app(tmp_path)

    assert isinstance(app.state.separator_registry, SeparatorRegistry)
    assert app.state.separator_registry.architectures == frozenset(
        {"fake", ROFORMER_ARCHITECTURE, DEMUCS_ARCHITECTURE}
    )
    assert isinstance(app.state.model_catalog, ModelCatalog)


async def test_the_read_only_surfaces_serve_without_torch(tmp_path: Path) -> None:
    """``/health``, ``/models`` and ``/separation-modes`` — the whole read surface."""
    with torch_absent():
        app = build_app(tmp_path)
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
                health = await client.get(f"{API}/health")
                models = await client.get(f"{API}/models")
                modes = await client.get(f"{API}/separation-modes")

    assert health.status_code == 200, health.text
    assert health.json()["status"] == "ok"

    assert models.status_code == 200, models.text
    listed = cast(list[dict[str, Any]], models.json())
    # The catalogued torch-backed model is still *offered*: whether this build
    # can run it is answered by ``POST /jobs``, not by hiding it.
    assert {entry["architecture"] for entry in listed} >= {"fake", ROFORMER_ARCHITECTURE}

    assert modes.status_code == 200, modes.text
    assert cast(list[dict[str, Any]], modes.json())


async def test_a_fake_separator_job_runs_end_to_end_without_torch(tmp_path: Path) -> None:
    """The whole M1 workflow, on a build with no PyTorch at all."""
    with torch_absent():
        app = build_app(tmp_path)
        audio_id = register_audio(app)
        async with app.router.lifespan_context(app):
            watcher = TerminalWatcher()
            manager = cast(JobManager, app.state.job_manager)
            manager.add_listener(watcher)
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
                created = await client.post(
                    f"{API}/jobs",
                    json={
                        "audio_id": audio_id,
                        "mode_id": "vocals",
                        "quality_id": fake_quality_id("vocals"),
                    },
                )
                assert created.status_code == 201, created.text
                job_id = cast(str, created.json()["id"])

                terminal = await watcher.terminal(job_id)
                assert isinstance(terminal, JobCompletedEvent), terminal

                final = await client.get(f"{API}/jobs/{job_id}")
                result = await client.get(f"{API}/jobs/{job_id}/result")
                vocals = await client.get(f"{API}/jobs/{job_id}/stems/vocals")
            manager.remove_listener(watcher)

    assert final.status_code == 200, final.text
    body = cast(dict[str, Any], final.json())
    assert body["state"] == "completed"
    assert body["progress"] == 1.0

    assert result.status_code == 200, result.text
    stems = cast(list[dict[str, Any]], result.json()["stems"])
    assert [stem["name"] for stem in stems] == ["vocals", "instrumental"]
    for stem in stems:
        assert stem["duration_seconds"] > 0

    # Real playable bytes, not merely a record that a job finished.
    assert vocals.status_code == 200, vocals.text
    assert vocals.content[:4] == b"RIFF"


async def post_torch_backed_job(app: FastAPI, audio_id: str) -> httpx2.Response:
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                f"{API}/jobs",
                json={
                    "audio_id": audio_id,
                    "mode_id": "vocals",
                    "quality_id": "balanced",
                },
            )


async def test_a_job_for_a_torch_backed_model_is_a_501_envelope(tmp_path: Path) -> None:
    """The documented envelope, over HTTP, with the weights genuinely installed.

    Installing the synthetic checkpoint first is what makes this a statement
    about *torch* and not about the weights: the very same request succeeds in
    :func:`test_the_same_job_succeeds_with_torch_installed`.
    """
    models_dir = write_catalog([torch_backed_entry()], tmp_path / "models")
    write_tiny_weights(weights_path(models_dir, TORCH_BACKED_MODEL_ID))

    with torch_absent():
        app = build_app(tmp_path / "data", models_dir=models_dir)
        audio_id = register_audio(app)
        response = await post_torch_backed_job(app, audio_id)

    assert response.status_code == 501, response.text
    body = cast(dict[str, Any], response.json())
    assert set(body) == {"error"}
    error = cast(dict[str, Any], body["error"])
    assert set(error) == {"code", "message", "detail"}
    assert error["code"] == "separator_unavailable"
    assert error["detail"] == {
        "model_id": TORCH_BACKED_MODEL_ID,
        "architecture": ROFORMER_ARCHITECTURE,
    }


async def test_the_same_job_succeeds_with_torch_installed(tmp_path: Path) -> None:
    """The inverse. With torch present nothing about this request changed."""
    models_dir = write_catalog([torch_backed_entry()], tmp_path / "models")
    write_tiny_weights(weights_path(models_dir, TORCH_BACKED_MODEL_ID))

    app = build_app(tmp_path / "data", models_dir=models_dir)
    audio_id = register_audio(app)
    response = await post_torch_backed_job(app, audio_id)

    assert response.status_code == 201, response.text
    assert cast(dict[str, Any], response.json())["model_id"] == TORCH_BACKED_MODEL_ID
