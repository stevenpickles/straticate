# [018] Compute device detection + devices API

Branch: `018-device-detection`
Status: PR OPEN
Dependencies: 005
PR: #…

## Objective

The backend now knows which compute devices the host offers and serves them as
*logical* devices through `GET /api/v1/system/devices`, so job configuration
(015), runtime telemetry (019), and real inference (026) can reference devices
by ID without any PyTorch object leaking into application-level APIs.

## Scope

- `backend/src/straticate/system/` — new package for host system capabilities.
  - `devices.py`:
    - `ComputeDeviceProbe` — the per-backend detection seam (`backend` +
      `detect() -> Sequence[ComputeDevice]`).
    - `TorchCudaProbe` — NVIDIA CUDA detection through an **optional** PyTorch
      import resolved at call time (see "Notes / decisions").
    - `cpu_device()` / `cpu_name()` / `total_system_memory_bytes()` — the
      always-present CPU fallback.
    - `DeviceDetector` — runs accelerator probes in priority order, always
      appends the CPU device, caches the result (`devices()`, `refresh()`),
      and offers `select_default_device()` and `get_device(device_id)`.
    - `get_device_detector` / `DeviceDetectorDep` — FastAPI dependency
      accessors reading `app.state.device_detector`.
- `backend/src/straticate/api/system.py` — `GET /system/devices` added to the
  existing system router (`/health` and `/version` untouched).
- `backend/src/straticate/main.py` — `create_app()` constructs the detector,
  warms it once, and stores it on `app.state.device_detector`. The lifespan
  block is deliberately untouched (feature 013 owns it).

## Out of scope

- Runtime telemetry sampling, VRAM usage, NVML utilization/temperature — 019.
- Device *selection* during job creation — 015.
- Real inference and the PyTorch dependency — 026.
- Model catalog (010), WebSocket hub/lifespan (013), FakeSeparator (014),
  frontend.

## Expected modules/files

- `backend/src/straticate/system/__init__.py`
- `backend/src/straticate/system/devices.py`
- `backend/src/straticate/api/system.py` (extended)
- `backend/src/straticate/main.py` (wiring)
- `backend/tests/test_devices.py`, `backend/tests/test_system.py` (extended)

## Acceptance criteria

- [x] `GET /api/v1/system/devices` returns `ComputeDevice[]` exactly as
      documented in `docs/contracts/rest-api.md`.
- [x] Detection priority is CUDA first, CPU last; the CPU device is always
      present, so the list is never empty.
- [x] `select_default_device()` returns the first CUDA device when one exists,
      otherwise the CPU device.
- [x] `get_device(device_id)` raises `ApplicationError("device_not_found",
      status_code=404)` for unknown IDs.
- [x] A probe that raises logs a warning and contributes no devices; detection
      and application startup still succeed.
- [x] No PyTorch (or `psutil`, or any other) dependency was added; the whole
      suite passes on a GPU-free machine with torch absent.
- [x] `backend` stays an open string set — `mps`, `directml`, … need no API
      change.

## Required tests

`backend/tests/test_devices.py`:

- module imports without pulling `torch` into `sys.modules`;
- `load_torch()` returns `None` when the import fails;
- CPU device always reported with a non-empty name, ID `cpu`, and a plausible
  `memory_total_bytes`;
- a fake torch module with two CUDA devices maps to the exact contract JSON,
  and CUDA sorts before CPU;
- an unavailable CUDA runtime yields no CUDA devices;
- a raising probe degrades to CPU only, logs exactly one warning, and does not
  suppress other healthy probes;
- detection is cached until `refresh()`;
- `select_default_device()` prefers CUDA, falls back to CPU;
- `get_device()` resolves known IDs and 404s on unknown ones.

`backend/tests/test_system.py`: `GET /api/v1/system/devices` over
`httpx.ASGITransport` — the real detector (CPU-only on CI) and a fake
CUDA probe injected into `app.state`.

## Notes / decisions

### PyTorch is an optional probe, not a dependency

ARCHITECTURE.md §14 requires normal CI to stay GPU-free and fast, and PyTorch
is deferred to feature 026. So CUDA detection sits behind a pluggable probe:
`TorchCudaProbe` resolves torch with `importlib.import_module("torch")` *inside*
`detect()` and consumes it through narrow structural protocols
(`TorchModuleLike`, `CudaNamespaceLike`, `CudaDevicePropertiesLike`). Nothing
imports torch at module scope, and the type checker never needs torch stubs.

With torch absent the probe returns no devices and the system reports CPU only
— a normal, first-class, tested path, not an error. NVML is likewise optional
and not implemented here (ARCHITECTURE.md §12: basic operation must never
require NVML).

### How feature 026 plugs in real torch

Add `torch` to `backend/pyproject.toml`. `load_torch()` then resolves the real
module and `TorchCudaProbe` starts reporting real CUDA devices. **No API
change** anywhere: same probe, same `DeviceDetector`, same endpoint. If 026
needs a different accelerator (MPS, DirectML), it adds another
`ComputeDeviceProbe` implementation to the detector's `probes` tuple — the
`backend` field is an open string set, never a closed enum.

### What feature 019 (runtime telemetry) should call

`straticate.system.devices` owns the **static** device facts. For each job:

1. Resolve the device — `DeviceDetector.get_device(job.configuration.device_id)`
   (or `select_default_device()` when the job pinned none), obtained via
   `get_device_detector` / `DeviceDetectorDep`.
2. Copy `id`, `name`, `backend`, `memory_total_bytes` straight into
   `GpuMetrics`; emit `gpu: null` when the resolved device's backend is `cpu`
   (`CPU_BACKEND`).
3. Sample the **dynamic** fields itself — `memory_allocated_bytes`,
   `memory_peak_bytes` (PyTorch memory APIs) and the optional
   `utilization` / `temperature_celsius` (NVML). Nothing in this feature polls
   hardware after startup.

### Other decisions

- Total system RAM is read through platform APIs (`os.sysconf` on POSIX,
  `GlobalMemoryStatusEx` via `ctypes` on Windows), resolved with `getattr` so
  the module type-checks on every platform. `psutil` was not worth a new
  dependency for one number. Any failure degrades to `0`, documented as
  "unknown" on the endpoint; it never raises.
- CPU name comes from `platform.processor()`, falling back to
  `platform.uname().machine`, then the literal `"CPU"` — descriptive only,
  never empty.
- Detection runs in `create_app()` rather than the lifespan: devices cannot
  change during a run, detection never raises, and feature 013 owns the
  lifespan block in parallel.
