"""The shared FFmpeg runner: every invocation is bounded.

Nothing here waits for a real timeout — ``subprocess.run`` is stubbed, so the
expiry path is exercised in microseconds.
"""

import subprocess
from collections.abc import Sequence
from typing import Any, NoReturn

import pytest

from straticate.audio import ffmpeg as ffmpeg_module
from straticate.audio.ffmpeg import FFmpegTimeout, run_ffmpeg
from straticate.config import get_settings


@pytest.fixture(autouse=True)
def fresh_settings() -> Any:
    """The runner reads process-wide settings; keep the cache honest."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_the_settings_timeout_is_passed_to_every_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRATICATE_FFMPEG_TIMEOUT_SECONDS", "17.5")
    seen: dict[str, Any] = {}

    def fake_run(command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen["command"] = list(command)
        seen.update(kwargs)
        return subprocess.CompletedProcess(list(command), 0, b"out", b"")

    monkeypatch.setattr(ffmpeg_module.subprocess, "run", fake_run)

    result = run_ffmpeg(["ffprobe", "-version"])

    assert result.stdout == b"out"
    assert seen["timeout"] == 17.5
    assert seen["capture_output"] is True
    assert seen["check"] is False


def test_an_explicit_timeout_overrides_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(list(command), 0, b"", b"")

    monkeypatch.setattr(ffmpeg_module.subprocess, "run", fake_run)

    run_ffmpeg(["ffmpeg", "-version"], timeout_seconds=0.5)

    assert seen["timeout"] == 0.5


def test_expiry_raises_ffmpeg_timeout_naming_the_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    def wedged(command: Sequence[str], **kwargs: Any) -> NoReturn:
        raise subprocess.TimeoutExpired(list(command), kwargs["timeout"])

    monkeypatch.setattr(ffmpeg_module.subprocess, "run", wedged)

    with pytest.raises(FFmpegTimeout) as excinfo:
        run_ffmpeg(["ffprobe", "-i", "song.wav"], timeout_seconds=3)

    assert excinfo.value.tool == "ffprobe"
    assert excinfo.value.timeout_seconds == 3
    assert "3s" in str(excinfo.value)


def test_a_non_zero_exit_is_returned_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """Interpreting a failed encode belongs to the call site, not the runner."""

    def failing(command: Sequence[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(list(command), 1, b"", b"nope")

    monkeypatch.setattr(ffmpeg_module.subprocess, "run", failing)

    result = run_ffmpeg(["ffmpeg"], timeout_seconds=1)

    assert result.returncode == 1
    assert result.stderr == b"nope"


def test_a_timeout_is_not_an_audio_error() -> None:
    """The three call sites must be free to classify it themselves."""
    from straticate.audio import AudioProbeError
    from straticate.inference.pcm import AudioDecodeError

    timeout = FFmpegTimeout("ffmpeg", 1.0)
    assert not isinstance(timeout, AudioProbeError)
    assert not isinstance(timeout, AudioDecodeError)
