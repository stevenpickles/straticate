"""Tests for wide-stereo detection (feature 063).

Two tiers, and the split is the honest one.

**The synthesized proxy**, which is everything below the integration marker:
stereo pairs constructed at known correlations, so the arithmetic, the
threshold's boundary, the streaming shape and the documented edge cases are all
pinned in CI without a byte of music. What it deliberately cannot establish is
whether ``WIDE_STEREO_THRESHOLD`` fires on ordinary records — that needs ordinary
records, and they cannot live in this repository. See the protocol in
``docs/features/063-wide-stereo-detection.md``.

**The known track**, which is ``test_known_wide_track_reproduces_published_figure``:
feature 041's own 1968 mix, run through the shipped endpoint, reproducing the
**+0.229** 041 published. It is ``@pytest.mark.integration`` (deselected by
default, like the real-model tier) because the audio is not committed. If it ever
stops reproducing, the implementation is wrong — that is the whole point of
having one real number in a file of synthetic ones.
"""

import asyncio
import io
import math
import random
import statistics
import time
import wave
from array import array
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

from straticate.audio import analysis as analysis_module
from straticate.audio.analysis import (
    ANALYSIS_BLOCK_FRAMES,
    WIDE_STEREO_THRESHOLD,
    CorrelationSums,
    StereoAnalysisCache,
    analyse_stereo,
    analysis_from_sums,
)
from straticate.config import Settings
from straticate.main import create_app
from straticate.schemas import AudioMetadata, StereoAnalysis

AUDIO_URL = "/api/v1/audio"

SAMPLE_RATE = 44100

KNOWN_TRACK_NAME = "beatles-back-in-the-ussr.m4a"
"""Feature 041's track, kept at the repository root and gitignored.

``docs/features/063-wide-stereo-detection.md`` records what it measured; this
constant is only how the integration test finds it.
"""

KNOWN_TRACK_CORRELATION = 0.229
"""The full-band L/R correlation feature 041 published for that track."""


# --------------------------------------------------------------------------
# Fixtures and synthesis
# --------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings whose data_dir lives inside the test's tmp_path."""
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """Override the shared app fixture with tmp_path-backed settings."""
    return create_app(settings)


def wav_bytes(left: array[int], right: array[int] | None = None) -> bytes:
    """Encode one or two int16 planes as a 16-bit PCM WAV file in memory."""
    planes = [left] if right is None else [left, right]
    frames = min(len(plane) for plane in planes)
    interleaved: array[int] = array("h", bytes(2 * frames * len(planes)))
    for index, plane in enumerate(planes):
        interleaved[index :: len(planes)] = plane[:frames]
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(len(planes))
        writer.setsampwidth(2)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(interleaved.tobytes())
    return buffer.getvalue()


def correlated_pair(
    target: float, *, frames: int = 40000, seed: int = 20630
) -> tuple[array[int], array[int]]:
    """Two int16 planes whose Pearson correlation is close to ``target``.

    The construction is the one 041's handoff describes as the thing being
    measured: a **shared** component plus an **independent** one per channel,

    .. code-block:: text

        L = shared + k * n_left      R = shared + k * n_right

    with all three components drawn from the same distribution, which puts the
    population correlation at ``1 / (1 + k^2)``. Solving that for ``k`` gives the
    weight below. The *sample* correlation lands near, not on, the target — which
    is why every assertion about these signals is made against the value an
    independent reference (:func:`statistics.correlation`) computes for the very
    same samples, and only the closeness to ``target`` is a tolerance.

    Amplitudes are normalised so one standard deviation is 4 000 counts,
    eight of which still fit inside int16: full scale is never approached, so
    nothing is clipped and the correlation the samples carry is the one the
    construction asked for.
    """
    weight = math.sqrt((1.0 - target) / target)
    scale = 4000.0 / math.sqrt(1.0 + weight * weight)
    rng = random.Random(seed)
    left: array[int] = array("h")
    right: array[int] = array("h")
    for _ in range(frames):
        shared = rng.gauss(0.0, 1.0)
        left.append(_clamped(scale * (shared + weight * rng.gauss(0.0, 1.0))))
        right.append(_clamped(scale * (shared + weight * rng.gauss(0.0, 1.0))))
    return left, right


def _clamped(value: float) -> int:
    """Round to the symmetric int16 range the rest of the application uses."""
    return max(-32767, min(32767, round(value)))


def exactly_half_correlated() -> tuple[array[int], array[int]]:
    """A pair whose Pearson correlation is **exactly** +0.5, by integer arithmetic.

    Eight frames of a symmetric two-level signal: both planes are zero-mean with
    equal energy, so ``r`` reduces to ``sum(L*R) / 8`` and the pattern below puts
    six of the eight products at ``+1`` and two at ``-1``. Nothing about it is
    approximate — it is what pins which side of :data:`WIDE_STEREO_THRESHOLD` a
    tie falls on.
    """
    amplitude = 10000
    pattern_left = [1, 1, 1, 1, -1, -1, -1, -1]
    pattern_right = [1, 1, 1, -1, -1, -1, 1, -1]
    left = array("h", [amplitude * value for value in pattern_left])
    right = array("h", [amplitude * value for value in pattern_right])
    return left, right


def sums_of(left: array[int], right: array[int], *, block_frames: int = 4096) -> CorrelationSums:
    """Accumulate two planes through the real block path."""
    sums = CorrelationSums()
    interleaved: array[int] = array("h", bytes(4 * len(left)))
    interleaved[0::2] = left
    interleaved[1::2] = right
    raw = interleaved.tobytes()
    step = block_frames * 4
    for offset in range(0, len(raw), step):
        sums.add(raw[offset : offset + step])
    return sums


async def upload(client: httpx2.AsyncClient, name: str, content: bytes) -> str:
    """POST ``content`` and return the registered audio ID."""
    response = await client.post(AUDIO_URL, files={"file": (name, content, "audio/wav")})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def analysis_of(client: httpx2.AsyncClient, audio_id: str) -> StereoAnalysis:
    """GET the analysis of ``audio_id``, asserting a 200."""
    response = await client.get(f"{AUDIO_URL}/{audio_id}/analysis")
    assert response.status_code == 200, response.text
    return StereoAnalysis.model_validate(response.json())


# --------------------------------------------------------------------------
# The arithmetic
# --------------------------------------------------------------------------


@pytest.mark.parametrize("target", [0.3, 0.45, 0.5, 0.55, 0.7, 0.9])
def test_synthesized_pairs_measure_their_constructed_correlation(target: float) -> None:
    left, right = correlated_pair(target)
    sums = sums_of(left, right)
    measured = sums.correlation
    assert measured is not None

    # Against an independent implementation, on the very same samples: this is
    # what says the five-sum form is Pearson's coefficient and not something
    # that merely trends with it.
    reference = statistics.correlation(list(left), list(right))
    assert measured == pytest.approx(reference, rel=1e-9, abs=1e-12)

    # And against the construction, which is a finite-sample question and so a
    # tolerance rather than an equality.
    assert measured == pytest.approx(target, abs=0.02)


@pytest.mark.parametrize("target", [0.3, 0.45, 0.5, 0.55, 0.7, 0.9])
def test_wide_stereo_follows_the_threshold(target: float) -> None:
    left, right = correlated_pair(target)
    analysis = analysis_from_sums(sums_of(left, right))
    assert analysis.l_r_correlation is not None
    assert analysis.wide_stereo is (analysis.l_r_correlation < WIDE_STEREO_THRESHOLD)


def test_threshold_boundary_is_not_wide() -> None:
    """Exactly at the threshold is *not* wide: the comparison is strictly below."""
    left, right = exactly_half_correlated()
    analysis = analysis_from_sums(sums_of(left, right))
    assert analysis.l_r_correlation == 0.5
    assert analysis.wide_stereo is False


def test_just_below_the_threshold_is_wide() -> None:
    """One frame short of the tie, and the same pair is called wide."""
    left, right = exactly_half_correlated()
    # Drop the last frame, whose product is -1: the remaining seven give a
    # correlation strictly below 0.5 while staying an exact integer ratio.
    sums = sums_of(left[:-1], right[:-1])
    analysis = analysis_from_sums(sums)
    assert analysis.l_r_correlation is not None
    assert analysis.l_r_correlation < WIDE_STEREO_THRESHOLD
    assert analysis.wide_stereo is True


def test_identical_channels_correlate_at_exactly_one() -> None:
    left, _ = correlated_pair(0.5)
    sums = sums_of(left, left)
    assert sums.correlation == 1.0


def test_inverted_channels_correlate_at_exactly_minus_one() -> None:
    left, _ = correlated_pair(0.5)
    inverted = array("h", [-value for value in left])
    sums = sums_of(left, inverted)
    assert sums.correlation == -1.0
    analysis = analysis_from_sums(sums)
    assert analysis.wide_stereo is True


def test_blocked_accumulation_equals_whole_track_accumulation() -> None:
    """Blocking is a memory and latency device, never an approximation."""
    left, right = correlated_pair(0.42, frames=9001)
    whole = sums_of(left, right, block_frames=len(left))
    for block_frames in (1, 7, 1024, ANALYSIS_BLOCK_FRAMES):
        blocked = sums_of(left, right, block_frames=block_frames)
        assert blocked == whole
        assert blocked.correlation == whole.correlation


def test_partial_trailing_frame_is_ignored() -> None:
    """A truncated stream loses its incomplete frame, not its accumulated sums."""
    left, right = correlated_pair(0.6, frames=64)
    complete = sums_of(left, right)
    truncated = CorrelationSums()
    interleaved: array[int] = array("h", bytes(4 * len(left)))
    interleaved[0::2] = left
    interleaved[1::2] = right
    truncated.add(interleaved.tobytes() + b"\x01\x00")
    assert truncated == complete


def test_zero_variance_channel_has_no_correlation_and_is_called_wide() -> None:
    """A one-sided track is the extreme of the failure mode, not an absence of it."""
    left, _ = correlated_pair(0.5, frames=2048)
    silent = array("h", bytes(2 * len(left)))
    analysis = analysis_from_sums(sums_of(left, silent))
    assert analysis.l_r_correlation is None
    assert analysis.wide_stereo is True


def test_too_few_frames_has_no_correlation_and_is_not_wide() -> None:
    empty = analysis_from_sums(CorrelationSums())
    assert empty.l_r_correlation is None
    assert empty.wide_stereo is False


async def test_mono_source_is_answered_without_decoding(tmp_path: Path) -> None:
    """No image, no subprocess: a mono upload never reaches FFmpeg."""
    missing = tmp_path / "never-read.wav"
    metadata = AudioMetadata(
        duration_seconds=1.0,
        container="wav",
        codec="pcm_s16le",
        channels=1,
        sample_rate_hz=SAMPLE_RATE,
        bit_depth=16,
        bit_rate_bps=None,
    )
    analysis = await analyse_stereo(missing, metadata, timeout_seconds=5.0)
    assert analysis.l_r_correlation is None
    assert analysis.wide_stereo is False
    assert not missing.exists()


# --------------------------------------------------------------------------
# The endpoint
# --------------------------------------------------------------------------


async def test_endpoint_measures_a_wide_upload(client: httpx2.AsyncClient) -> None:
    left, right = correlated_pair(0.2, frames=8000)
    audio_id = await upload(client, "wide.wav", wav_bytes(left, right))
    analysis = await analysis_of(client, audio_id)
    assert analysis.l_r_correlation is not None
    assert analysis.l_r_correlation == pytest.approx(0.2, abs=0.05)
    assert analysis.wide_stereo is True


async def test_endpoint_measures_an_ordinary_upload(client: httpx2.AsyncClient) -> None:
    left, right = correlated_pair(0.85, frames=8000)
    audio_id = await upload(client, "ordinary.wav", wav_bytes(left, right))
    analysis = await analysis_of(client, audio_id)
    assert analysis.l_r_correlation is not None
    assert analysis.l_r_correlation == pytest.approx(0.85, abs=0.05)
    assert analysis.wide_stereo is False


async def test_endpoint_agrees_with_the_in_process_measurement(
    client: httpx2.AsyncClient,
) -> None:
    """What the wire says is what the streaming pass computed — to the last bit."""
    left, right = correlated_pair(0.31, frames=12000)
    audio_id = await upload(client, "track.wav", wav_bytes(left, right))
    served = await analysis_of(client, audio_id)
    assert served.l_r_correlation == sums_of(left, right).correlation


async def test_endpoint_answers_a_mono_upload(client: httpx2.AsyncClient) -> None:
    left, _ = correlated_pair(0.5, frames=4000)
    audio_id = await upload(client, "mono.wav", wav_bytes(left))
    analysis = await analysis_of(client, audio_id)
    assert analysis.l_r_correlation is None
    assert analysis.wide_stereo is False


async def test_unknown_audio_id_is_404(client: httpx2.AsyncClient) -> None:
    response = await client.get(f"{AUDIO_URL}/01NOPE/analysis")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "audio_not_found"


async def test_missing_file_on_disk_is_404(client: httpx2.AsyncClient, settings: Settings) -> None:
    left, right = correlated_pair(0.5, frames=2000)
    audio_id = await upload(client, "gone.wav", wav_bytes(left, right))
    (settings.data_dir / "audio" / audio_id / "original.wav").unlink()

    response = await client.get(f"{AUDIO_URL}/{audio_id}/analysis")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "audio_not_found"


async def test_undecodable_bytes_are_reported_as_such(
    client: httpx2.AsyncClient, settings: Settings
) -> None:
    """ffprobe accepted these bytes at upload; FFmpeg cannot decode them now."""
    left, right = correlated_pair(0.5, frames=2000)
    audio_id = await upload(client, "broken.wav", wav_bytes(left, right))
    (settings.data_dir / "audio" / audio_id / "original.wav").write_bytes(b"not audio at all")

    response = await client.get(f"{AUDIO_URL}/{audio_id}/analysis")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "audio_not_decodable"


async def test_a_slow_decode_times_out_as_its_own_error(
    client: httpx2.AsyncClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass that runs out of time is not a file that cannot be decoded.

    The bound is shortened *after* the upload, so ffprobe still had its normal
    budget: the only thing under test is what the analysis pass does when its own
    clock runs out.
    """
    left, right = correlated_pair(0.5, frames=8000)
    audio_id = await upload(client, "slow.wav", wav_bytes(left, right))
    settings: Settings = app.state.settings
    app.state.settings = settings.model_copy(update={"ffmpeg_timeout_seconds": 0.05})

    def wedged(
        decoder: object, sums: CorrelationSums, block_frames: int
    ) -> bool:  # pragma: no cover - the loop abandons it
        time.sleep(1.0)
        return True

    monkeypatch.setattr(analysis_module, "_pump", wedged)

    response = await client.get(f"{AUDIO_URL}/{audio_id}/analysis")
    assert response.status_code == 504
    body = response.json()
    assert body["error"]["code"] == "audio_analysis_timed_out"
    assert body["error"]["detail"]["timeout_seconds"] == pytest.approx(0.05)


# --------------------------------------------------------------------------
# Caching
# --------------------------------------------------------------------------


async def test_concurrent_first_requests_share_one_computation(
    client: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Single flight: two GETs racing on a cold cache decode the file once."""
    left, right = correlated_pair(0.4, frames=6000)
    audio_id = await upload(client, "race.wav", wav_bytes(left, right))

    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()
    real = analysis_module.analyse_stereo

    async def counting(
        path: Path, metadata: AudioMetadata, *, timeout_seconds: float
    ) -> StereoAnalysis:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return await real(path, metadata, timeout_seconds=timeout_seconds)

    monkeypatch.setattr("straticate.api.audio.analyse_stereo", counting)

    first = asyncio.create_task(client.get(f"{AUDIO_URL}/{audio_id}/analysis"))
    # Deterministic, not a race on scheduling: the second request is only sent
    # once the first has demonstrably entered the loader and is parked there.
    await asyncio.wait_for(started.wait(), 5.0)
    second = asyncio.create_task(client.get(f"{AUDIO_URL}/{audio_id}/analysis"))
    await asyncio.sleep(0)
    release.set()
    responses = await asyncio.gather(first, second)

    assert calls == 1
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()


async def test_a_second_request_is_served_from_the_cache(
    client: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    left, right = correlated_pair(0.4, frames=6000)
    audio_id = await upload(client, "cached.wav", wav_bytes(left, right))
    first = await analysis_of(client, audio_id)

    async def forbidden(
        path: Path, metadata: AudioMetadata, *, timeout_seconds: float
    ) -> StereoAnalysis:
        raise AssertionError("a cached analysis must not be recomputed")

    monkeypatch.setattr("straticate.api.audio.analyse_stereo", forbidden)
    assert await analysis_of(client, audio_id) == first


async def test_deleting_the_upload_drops_its_cached_analysis(
    client: httpx2.AsyncClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    left, right = correlated_pair(0.4, frames=4000)
    audio_id = await upload(client, "doomed.wav", wav_bytes(left, right))
    await analysis_of(client, audio_id)

    cache: StereoAnalysisCache = app.state.stereo_analysis_cache
    discarded: list[str] = []
    real_discard = cache.discard

    def recording(target: str) -> None:
        discarded.append(target)
        real_discard(target)

    monkeypatch.setattr(cache, "discard", recording)

    assert (await client.delete(f"{AUDIO_URL}/{audio_id}")).status_code == 204
    assert discarded == [audio_id]
    assert (await client.get(f"{AUDIO_URL}/{audio_id}/analysis")).status_code == 404


async def test_discarding_forgets_a_cached_measurement() -> None:
    """The cache holds one computation per ID until it is discarded, then none."""
    cache = StereoAnalysisCache()
    calls = 0

    async def load() -> StereoAnalysis:
        nonlocal calls
        calls += 1
        return StereoAnalysis(l_r_correlation=0.9, wide_stereo=False)

    assert (await cache.get("01ABC", load)).wide_stereo is False
    await cache.get("01ABC", load)
    assert calls == 1

    cache.discard("01ABC")
    await cache.get("01ABC", load)
    assert calls == 2


async def test_a_failed_computation_is_not_cached() -> None:
    """A failure must not become the permanent answer for an audio ID."""
    cache = StereoAnalysisCache()
    attempts = 0

    async def flaky() -> StereoAnalysis:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("boom")
        return StereoAnalysis(l_r_correlation=0.9, wide_stereo=False)

    with pytest.raises(RuntimeError):
        await cache.get("01ABC", flaky)
    assert (await cache.get("01ABC", flaky)).l_r_correlation == 0.9
    assert attempts == 2


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


def test_openapi_document_carries_the_route_and_the_schema() -> None:
    from straticate.scripts.export_openapi import build_openapi_document

    document = build_openapi_document()
    path = document["paths"]["/api/v1/audio/{audio_id}/analysis"]["get"]
    ref = path["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert ref == "#/components/schemas/StereoAnalysis"

    schema = document["components"]["schemas"]["StereoAnalysis"]
    assert set(schema["properties"]) == {"l_r_correlation", "wide_stereo"}
    assert set(schema["required"]) == {"l_r_correlation", "wide_stereo"}


# --------------------------------------------------------------------------
# The known track (integration tier)
# --------------------------------------------------------------------------


def _known_track() -> Path | None:
    """Feature 041's track, if this checkout has it.

    Looked for at the repository root — where it is gitignored — and at
    ``STRATICATE_WIDE_STEREO_FIXTURE`` for a checkout that keeps it elsewhere
    (a git worktree, for instance, whose root is not the one holding the file).
    """
    import os

    override = os.environ.get("STRATICATE_WIDE_STEREO_FIXTURE")
    if override:
        candidate = Path(override)
        return candidate if candidate.is_file() else None
    candidate = Path(__file__).resolve().parents[2] / KNOWN_TRACK_NAME
    return candidate if candidate.is_file() else None


@pytest.fixture
def known_track() -> Iterator[Path]:
    path = _known_track()
    if path is None:
        pytest.skip(f"{KNOWN_TRACK_NAME} is not in this checkout (it is never committed)")
    yield path


@pytest.mark.integration
async def test_known_wide_track_reproduces_published_figure(
    client: httpx2.AsyncClient, known_track: Path
) -> None:
    """Feature 041's +0.229, through the shipped endpoint, on the shipped path.

    Everything else in this file is synthetic. This is the one assertion that
    says the implementation measures *the thing 041 measured*, and it is the
    stop condition the feature brief named: if this stops reproducing, the
    implementation is wrong and nothing built on it may be trusted.
    """
    response = await client.post(
        AUDIO_URL,
        files={"file": (known_track.name, known_track.read_bytes(), "audio/mp4")},
    )
    assert response.status_code == 201, response.text
    audio_id = str(response.json()["id"])

    analysis = await analysis_of(client, audio_id)
    assert analysis.l_r_correlation is not None
    assert round(analysis.l_r_correlation, 3) == KNOWN_TRACK_CORRELATION
    assert analysis.wide_stereo is True
