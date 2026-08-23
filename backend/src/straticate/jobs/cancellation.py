"""Cooperative cancellation primitives for jobs.

Cancellation is cooperative (see ARCHITECTURE.md §6): the job manager sets a
:class:`CancellationToken` and the running executor/separator checks it between
units of work (typically between chunks) via
:meth:`CancellationToken.raise_if_cancelled`.
"""


class JobCancelled(Exception):
    """Raised inside an executor when its job's cancellation was requested.

    The job manager treats an executor raising ``JobCancelled`` as a clean
    cancellation (job state ``cancelled``), never as a failure.
    """


class CancellationToken:
    """A one-way latch signalling that a job should stop.

    Once cancelled, a token stays cancelled. Setting and reading the flag are
    single attribute operations, so the token is safe to check from worker
    threads while :meth:`cancel` is called on the event loop.
    """

    __slots__ = ("_cancelled",)

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        """Request cancellation. Idempotent."""
        self._cancelled = True

    @property
    def is_cancelled(self) -> bool:
        """Whether cancellation has been requested."""
        return self._cancelled

    def raise_if_cancelled(self) -> None:
        """Raise :class:`JobCancelled` if cancellation has been requested.

        Executors call this between units of work; the raised exception
        unwinds the executor and the job manager marks the job ``cancelled``.
        """
        if self._cancelled:
            raise JobCancelled("job cancellation was requested")
