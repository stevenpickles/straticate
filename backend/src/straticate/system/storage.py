"""Free space on the filesystem that receives downloaded model weights.

Why the backend has to answer this
==================================

An install writes hundreds of megabytes to
``{models_dir}/weights/{model_id}/`` on the machine running Straticate
(:mod:`straticate.models.layout`). The browser has no view of that filesystem:
the one disk figure it can obtain — ``navigator.storage.estimate()`` — is the
quota of the *page's own origin* inside the *browser's* profile directory, a
different number about a different disk. Feature 037 said so honestly in the UI
rather than improvising an answer; this module is the fact that replaces the
admission.

The shape of the answer follows feature 018
===========================================

:mod:`straticate.system.devices` resolves platform facts through a narrow seam
and **degrades rather than raises**: a probe that fails logs a warning and
contributes nothing, and ``total_system_memory_bytes()`` answers ``0`` —
documented as "unknown" — instead of propagating an exception. The same
discipline applies here, because the same thing is true: a figure the host
cannot produce is a normal condition, not a server error, and a UI that can
say "I don't know" is more useful than one that shows a 500.

So :func:`storage_report` never raises. The platform primitive
(:func:`shutil.disk_usage`) is isolated behind :func:`read_disk_usage` so tests
can stub every failure mode — a missing directory, a permissions failure, an
unsupported platform — **without ever filling a real disk**.

Two deliberate differences from 018
-----------------------------------

- **Unknown is ``None``, not ``0``.** Zero bytes of installed RAM is
  impossible, so ``0`` is an unambiguous "unknown" for a device. Zero bytes
  free on a disk is entirely possible and is the *worst* case — the one this
  feature exists to warn about — so conflating it with "could not tell" would
  turn the most important reading into the most ambiguous one.
- **Nothing is cached.** Devices cannot change during a run, so 018 probes once
  at startup. Free space changes every second, so it is read per request; the
  call is a single ``statvfs``/``GetDiskFreeSpaceEx`` and costs less than the
  HTTP round trip carrying it.

Everything here is synchronous and blocking
-------------------------------------------

``stat`` and ``shutil.disk_usage`` are filesystem calls. On a healthy local
disk they are microseconds; on an unresponsive network mount — a case this
feature's own warn-rather-than-refuse reasoning explicitly anticipates — they
block for as long as the mount does. So :func:`storage_report` is a plain
synchronous function and the *route* offloads it with :func:`asyncio.to_thread`
(:func:`straticate.api.system.read_storage`), the discipline features 022 and
025 already follow for FFmpeg and for downloads. ``/system/devices`` sidesteps
the question by probing once at startup; this endpoint deliberately does not
cache, so it has to answer it properly.

Which directory is measured
---------------------------

``models_dir`` need not exist yet: on a fresh checkout nothing has been
installed and ``{models_dir}/weights`` is created by the first install. Both
platform primitives require an existing path, so this module measures the
**nearest existing ancestor** of ``models_dir`` (:func:`nearest_existing_dir`).
That is not an approximation — the missing directories will be created on that
same filesystem by the very install being priced, so it is the filesystem the
bytes actually land on. Only when no ancestor can be examined at all (or the
primitive fails) is the answer unknown.
"""

import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from straticate.schemas import StorageReport

logger = logging.getLogger(__name__)


class DiskUsageLike(Protocol):
    """The subset of :func:`shutil.disk_usage`'s result we consume."""

    @property
    def total(self) -> int: ...

    @property
    def free(self) -> int: ...


DiskUsageReader = Callable[[Path], DiskUsageLike]
"""Reads total/free bytes for the filesystem holding a path."""

UNKNOWN_STORAGE = StorageReport(free_bytes=None, total_bytes=None)
"""The documented "the host could not answer" report.

Both fields are ``null`` together: they come from one call, so either it
answered or it did not.
"""


def read_disk_usage(path: Path) -> DiskUsageLike:
    """Read total/free bytes for the filesystem holding ``path``.

    A one-line wrapper on purpose: it is the seam every failure mode is
    injected through in the tests (a missing directory, a permissions failure,
    an unsupported platform), so none of them needs a real full disk.

    Raises:
        OSError: The platform could not answer for this path.
    """
    return shutil.disk_usage(path)


def nearest_existing_dir(path: Path) -> Path | None:
    """The deepest existing directory at or above ``path``, or ``None``.

    ``models_dir`` may not exist before the first install, and both platform
    primitives need a path that does. Its nearest existing ancestor sits on the
    filesystem the missing directories will be created on, which is the
    filesystem an install writes to — so this substitution reports on the right
    disk rather than approximating one.

    **This can raise, and callers must guard it.** ``Path.is_dir`` swallows only
    the errors :mod:`pathlib` calls "does not exist" — ``ENOENT``, ``ENOTDIR``,
    ``EBADF``, ``ELOOP`` and three Windows equivalents — and re-raises anything
    else. ``EACCES`` is **not** on that list, so an ancestor the process may not
    stat propagates a :class:`PermissionError` from here. :func:`storage_report`
    is the guarded entry point that turns it into the documented unknown; an
    earlier version of this docstring claimed the opposite and put the walk
    outside that guard, which is exactly how a permissions failure became a
    ``500`` instead of the ``200`` the contract promises.

    Raises:
        OSError: An ancestor could not be examined (e.g. ``PermissionError``).
    """
    for candidate in (path, *path.parents):
        if candidate.is_dir():
            return candidate
    return None


def storage_report(models_dir: Path, read_usage: DiskUsageReader | None = None) -> StorageReport:
    """Report free/total bytes for the filesystem holding ``models_dir``.

    Args:
        models_dir: :attr:`straticate.config.Settings.models_dir` — the
            directory an install writes weights beneath. Need not exist.
        read_usage: The platform primitive. Defaults to
            :func:`read_disk_usage`; tests inject failures through it.

    Returns:
        The figures, or :data:`UNKNOWN_STORAGE` when the host cannot produce them.

    **Never raises.** Any failure — a path with no examinable ancestor, an
    ancestor that cannot be stat'ed, a permissions error, an unsupported
    platform — is logged once at warning level and degrades to the documented
    unknown report, exactly as feature 018's probes degrade to "no devices".
    Negative or nonsensical readings are clamped to ``0``, which the contract
    distinguishes from unknown.

    **The whole read is inside the guard, the directory walk included.**
    :func:`nearest_existing_dir` is not exception-free — ``Path.is_dir`` lets
    ``EACCES`` through — so leaving the walk outside meant a ``models_dir``
    whose ancestor the process cannot read answered ``500`` rather than the
    contract's ``200`` with ``null`` figures. Interpreting the reading is
    inside it too: a reading this code cannot make sense of is the same kind
    of event as a call that failed, and "never raises" has to cover both.

    **Blocking.** ``stat`` and ``shutil.disk_usage`` are filesystem calls, and
    on an unresponsive network mount they can hang for as long as the mount
    does. Callers on the event loop must offload this to a worker thread; the
    route does (see :func:`straticate.api.system.read_storage`).
    """
    reader = read_usage or read_disk_usage
    target: Path | None = None
    try:
        target = nearest_existing_dir(models_dir)
        if target is None:
            logger.warning(
                "No existing directory at or above %s; free disk space is unknown.", models_dir
            )
            return UNKNOWN_STORAGE
        usage = reader(target)
        return StorageReport(
            free_bytes=max(int(usage.free), 0),
            total_bytes=max(int(usage.total), 0),
        )
    except Exception:
        logger.warning(
            "Could not read free disk space for %s; reporting unknown.",
            target or models_dir,
            exc_info=True,
        )
        return UNKNOWN_STORAGE
