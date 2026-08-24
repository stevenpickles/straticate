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

``timeout_seconds`` is a **required** argument, and this module reads no
settings of its own. Reaching for the process-global
:func:`~straticate.config.get_settings` here would quietly ignore the
``Settings`` an application was actually built with — and ``create_app(settings)``
is a documented path that every test fixture uses — so the bound travels the
same way every other setting does: from ``app.state.settings`` down through the
caller. Making the argument required is what stops a future call site from
silently falling back to a default nobody chose.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence

logger = logging.getLogger(__name__)

DEFAULT_FFMPEG_TIMEOUT_SECONDS = 600.0
"""Default bound for one FFmpeg/ffprobe invocation, in seconds.

The single definition of the default: :attr:`Settings.ffmpeg_timeout_seconds
<straticate.config.Settings.ffmpeg_timeout_seconds>` takes it from here, and so
does :class:`~straticate.inference.fake.FakeSeparator`, so there is no second
number to keep in step. Ten minutes is generous for a full-length track on a
slow disk and still finite.
"""


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
    command: Sequence[str], *, timeout_seconds: float
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
        timeout_seconds: The bound for this invocation. Required; it comes from
            the caller's ``Settings.ffmpeg_timeout_seconds``, never from a
            global read here.

    Returns:
        The completed process, whatever its return code — a non-zero exit is
        the caller's to interpret, not an exception here.

    Raises:
        FFmpegTimeout: The process did not finish in time. It is killed before
            this is raised (``subprocess.run`` does that on expiry), so no
            orphan survives the failure.
    """
    try:
        return subprocess.run(
            list(command), capture_output=True, check=False, timeout=timeout_seconds
        )
    except subprocess.TimeoutExpired as exc:
        tool = command[0] if command else "ffmpeg"
        logger.error("%s exceeded its %gs timeout and was killed", tool, timeout_seconds)
        raise FFmpegTimeout(tool, timeout_seconds) from exc


__all__ = ["DEFAULT_FFMPEG_TIMEOUT_SECONDS", "FFmpegTimeout", "run_ffmpeg"]
