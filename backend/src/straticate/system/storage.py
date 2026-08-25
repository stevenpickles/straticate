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

    ``Path.is_dir`` answers ``False`` rather than raising for a path it cannot
    stat, so an unreadable ancestor is simply skipped.
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

    **Never raises.** Any failure — a path with no examinable ancestor, a
    permissions error, an unsupported platform — is logged once at warning
    level and degrades to the documented unknown report, exactly as feature
    018's probes degrade to "no devices". Negative or nonsensical readings are
    clamped to ``0``, which the contract distinguishes from unknown.
    """
    reader = read_usage or read_disk_usage
    target = nearest_existing_dir(models_dir)
    if target is None:
        logger.warning(
            "No existing directory at or above %s; free disk space is unknown.", models_dir
        )
        return UNKNOWN_STORAGE
    try:
        usage = reader(target)
        # Inside the ``try`` with the call itself: a reading this code cannot
        # make sense of is the same kind of event as a call that failed, and
        # "never raises" has to cover both.
        return StorageReport(
            free_bytes=max(int(usage.free), 0),
            total_bytes=max(int(usage.total), 0),
        )
    except Exception:
        logger.warning(
            "Could not read free disk space for %s; reporting unknown.", target, exc_info=True
        )
        return UNKNOWN_STORAGE
