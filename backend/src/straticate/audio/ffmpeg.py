"""The one way this application runs FFmpeg or ffprobe.

FFmpeg is the single compatibility layer for decode, probe and encode
(ARCHITECTURE.md §5), and every one of those calls is a blocking
:func:`subprocess.run` dispatched onto asyncio's **default**
``ThreadPoolExecutor`` — the same, finite pool for uploads, separations and
exports alike. An unbounded subprocess is therefore not a slow request: it is a
thread held forever, and enough of them starve audio probing and separation as
well as the export that started them. Export makes that reachable on purpose,
being the first endpoint where a client can start subprocesses on demand.

So there is exactly one runner, and it always passes a timeout:
:func:`run_ffmpeg`. Expiry raises :class:`FFmpegTimeout`, which each call site
maps onto **its own** documented error code — a timeout on an upload is not the
same event as a timeout on an export, and neither of them means "not
decodable".
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence

from straticate.config import get_settings

logger = logging.getLogger(__name__)


class FFmpegTimeout(Exception):
    """An FFmpeg or ffprobe invocation exceeded its bounded run time.

    Deliberately not a subclass of any module's "could not decode" error: a
    tool that ran out of time has told us nothing about the media. Call sites
    translate it into a code of their own rather than folding it into an
    existing one.

    Attributes:
        tool: The executable that was run (``"ffmpeg"`` / ``"ffprobe"``).
        timeout_seconds: The bound that expired.
    """

    def __init__(self, tool: str, timeout_seconds: float) -> None:
        super().__init__(f"{tool} did not finish within {timeout_seconds:g}s")
        self.tool = tool
        self.timeout_seconds = timeout_seconds


def run_ffmpeg(
    command: Sequence[str], *, timeout_seconds: float | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Run an FFmpeg-family command to completion, bounded by a timeout.

    Blocking: callers run this in a worker thread (``asyncio.to_thread``), as
    every existing call site already does.

    Output is captured as **bytes** rather than decoded text: FFmpeg's stderr
    is not guaranteed to be valid UTF-8 in any locale, and one call site
    (:func:`straticate.inference.pcm._decode_sync`) needs raw PCM on stdout
    anyway. Callers that want text decode with an explicit error policy.

    Args:
        command: The full argument vector, ``command[0]`` being the tool.
        timeout_seconds: Override the bound; defaults to
            ``Settings.ffmpeg_timeout_seconds``.

    Returns:
        The completed process, whatever its return code — a non-zero exit is
        the caller's to interpret, not an exception here.

    Raises:
        FFmpegTimeout: The process did not finish in time. It is killed before
            this is raised (``subprocess.run`` does that on expiry), so no
            orphan survives the failure.
    """
    limit = get_settings().ffmpeg_timeout_seconds if timeout_seconds is None else timeout_seconds
    try:
        return subprocess.run(list(command), capture_output=True, check=False, timeout=limit)
    except subprocess.TimeoutExpired as exc:
        tool = command[0] if command else "ffmpeg"
        logger.error("%s exceeded its %gs timeout and was killed", tool, limit)
        raise FFmpegTimeout(tool, limit) from exc


__all__ = ["FFmpegTimeout", "run_ffmpeg"]
