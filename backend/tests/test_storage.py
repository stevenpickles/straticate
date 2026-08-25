"""Tests for free-disk-space reporting.

**No test here fills a disk.** The platform primitive is a seam
(:func:`straticate.system.storage.read_disk_usage`), so every degradation —
a models directory that does not exist, a permissions failure, a platform with
no answer at all — is a stub that raises, and the happy path is a stub that
returns known numbers. The two tests that touch the real ``shutil.disk_usage``
assert only that it answers *something* plausible for a directory that exists,
which is true on every machine this suite runs on.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from straticate.schemas import StorageReport
from straticate.system import (
    UNKNOWN_STORAGE,
    DiskUsageLike,
    DiskUsageReader,
    nearest_existing_dir,
    read_disk_usage,
    storage_report,
)

_LOGGER = "straticate.system.storage"


@dataclass(frozen=True)
class FakeUsage:
    """What :func:`shutil.disk_usage` returns, as far as this module cares."""

    total: int
    free: int


@dataclass
class RecordingReader:
    """A reader that reports fixed figures and remembers what it was asked about."""

    total: int
    free: int
    seen: list[Path] = field(default_factory=list[Path])

    def __call__(self, path: Path) -> DiskUsageLike:
        self.seen.append(path)
        return FakeUsage(total=self.total, free=self.free)


@dataclass(frozen=True)
class FailingReader:
    """A reader that fails the way a real platform fails."""

    failure: BaseException

    def __call__(self, path: Path) -> DiskUsageLike:
        raise self.failure


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_reports_free_and_total_for_the_models_directory(tmp_path: Path) -> None:
    reader = RecordingReader(total=512_000_000_000, free=42_000_000_000)

    report = storage_report(tmp_path, reader)

    assert report == StorageReport(free_bytes=42_000_000_000, total_bytes=512_000_000_000)
    assert reader.seen == [tmp_path]


def test_a_full_disk_is_zero_free_and_not_unknown(tmp_path: Path) -> None:
    """``0`` free is a fact — the most important one — never "we could not tell"."""
    report = storage_report(tmp_path, RecordingReader(total=512_000_000_000, free=0))

    assert report.free_bytes == 0
    assert report.total_bytes == 512_000_000_000
    assert report != UNKNOWN_STORAGE


def test_nonsensical_readings_are_clamped_rather_than_rejected(tmp_path: Path) -> None:
    report = storage_report(tmp_path, RecordingReader(total=-1, free=-1))

    assert report == StorageReport(free_bytes=0, total_bytes=0)


def test_the_real_primitive_answers_for_a_directory_that_exists(tmp_path: Path) -> None:
    """One test against the platform itself, with no disk filled to get it."""
    report = storage_report(tmp_path)

    assert report.free_bytes is not None
    assert report.total_bytes is not None
    assert report.total_bytes > 0
    assert 0 <= report.free_bytes <= report.total_bytes


def test_read_disk_usage_exposes_the_platform_primitive(tmp_path: Path) -> None:
    reading = read_disk_usage(tmp_path)

    assert reading.total > 0
    assert reading.free >= 0


# --------------------------------------------------------------------------
# A models directory that does not exist yet
# --------------------------------------------------------------------------


def test_a_missing_models_directory_reports_on_its_nearest_existing_parent(
    tmp_path: Path,
) -> None:
    """Nothing is installed yet, so ``models_dir`` may not exist.

    Its nearest existing ancestor is on the filesystem the install will create
    those directories on, so the figures describe the right disk — and the
    answer is a real one rather than a shrug.
    """
    missing = tmp_path / "models" / "weights" / "vocals-hq-001"
    reader = RecordingReader(total=1_000, free=500)

    report = storage_report(missing, reader)

    assert report == StorageReport(free_bytes=500, total_bytes=1_000)
    assert reader.seen == [tmp_path]


def test_nearest_existing_dir_walks_up_to_the_first_real_directory(tmp_path: Path) -> None:
    assert nearest_existing_dir(tmp_path) == tmp_path
    assert nearest_existing_dir(tmp_path / "a" / "b" / "c") == tmp_path


def test_a_file_in_the_path_is_not_treated_as_a_directory(tmp_path: Path) -> None:
    """``{file}/weights`` cannot be created, so the file itself is not the answer."""
    blocker = tmp_path / "models"
    blocker.write_bytes(b"not a directory")

    assert nearest_existing_dir(blocker / "weights") == tmp_path


def test_an_ancestor_that_cannot_be_stated_degrades_to_unknown(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A models directory whose ancestor the process may not read.

    ``Path.is_dir`` swallows only the errnos :mod:`pathlib` treats as "does not
    exist" — ``ENOENT``, ``ENOTDIR``, ``EBADF``, ``ELOOP`` — and ``EACCES`` is
    not among them, so the walk itself raises. It must still be a report of
    ``null`` figures: the endpoint promises ``200`` for a permissions failure,
    and a review found the walk sitting *outside* the guard, where this
    produced a ``500``.
    """
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    def refuse(self: Path) -> bool:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "is_dir", refuse)

    report = storage_report(tmp_path / "models")

    assert report == UNKNOWN_STORAGE
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_a_path_with_no_examinable_ancestor_is_unknown(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing anywhere above the models directory can be stat'ed."""
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    def never_a_directory(self: Path) -> bool:
        return False

    monkeypatch.setattr(Path, "is_dir", never_a_directory)

    report = storage_report(tmp_path / "models")

    assert report == UNKNOWN_STORAGE
    assert len(caplog.records) == 1


# --------------------------------------------------------------------------
# Degradation: unknown is a first-class answer, never an exception
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "failure"),
    [
        ("a permissions failure", PermissionError(13, "Permission denied")),
        ("a path that vanished between the check and the call", FileNotFoundError(2, "No such")),
        ("an unsupported platform", NotImplementedError("disk_usage is unsupported here")),
        ("an exotic filesystem", OSError(75, "Value too large for defined data type")),
    ],
)
def test_a_failing_primitive_degrades_to_unknown(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    label: str,
    failure: BaseException,
) -> None:
    """Every one of these is a report of ``null`` figures, not an exception.

    Feature 018's precedent: a probe that cannot answer logs a warning and
    contributes nothing rather than breaking the surface it feeds.
    """
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    report = storage_report(tmp_path, FailingReader(failure))

    assert report == UNKNOWN_STORAGE, label
    assert report.free_bytes is None
    assert report.total_bytes is None
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_a_reading_that_makes_no_sense_degrades_to_unknown(tmp_path: Path) -> None:
    """A reader answering something unusable is the same event as one that failed."""

    class Nonsense:
        total = "plenty"
        free = None

    def unusable(path: Path) -> object:
        return Nonsense()

    report = storage_report(tmp_path, cast(DiskUsageReader, unusable))

    assert report == UNKNOWN_STORAGE
