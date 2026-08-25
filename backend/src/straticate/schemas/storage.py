"""Free space on the filesystem that receives downloaded model weights."""

from pydantic import BaseModel, Field


class StorageReport(BaseModel):
    """How much room the machine running Straticate has for model weights.

    The figures describe the filesystem holding
    :attr:`straticate.config.Settings.models_dir` — the directory an install
    writes into — as read at the moment of the request. They are **not**
    cached: free space changes constantly, and a stale figure is worse than a
    fresh syscall.

    **``null`` means unknown, and unknown is a first-class answer.** A platform
    that cannot answer (a path that does not exist yet, a permissions failure,
    an exotic filesystem) gets a documented ``null`` rather than an error
    response, exactly as feature 018's device report degrades instead of
    raising. Unlike that report, the unknown value is ``null`` rather than
    ``0``: a machine with zero bytes of RAM is impossible, so ``0`` is
    unambiguous there — whereas a disk with zero bytes free is both possible
    and the single most important case to distinguish, so it must never double
    as "we could not tell".

    Both fields are unknown together: they come from one call.
    """

    free_bytes: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Bytes available to the server on the filesystem holding the models "
            "directory, or null when the host cannot report it."
        ),
    )
    total_bytes: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Total size in bytes of that filesystem, or null when the host cannot report it."
        ),
    )
