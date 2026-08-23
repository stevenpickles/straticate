"""Job state-machine transition rules.

The processing order is linear (see ARCHITECTURE.md §6)::

    queued → preparing → decoding → loading_model → separating
           → post_processing → encoding → completed

Rules enforced by :func:`assert_transition`:

- Moving **forward** along the processing order is allowed, including skipping
  intermediate stages (an executor may go ``queued → preparing → separating →
  completed`` when a stage does not apply to it).
- Moving backward — or "moving" to the same state — is not allowed.
- Any **non-terminal** state may transition to ``cancelled`` (user
  cancellation) or ``failed`` (error).
- Terminal states (``completed``, ``cancelled``, ``failed``) allow no further
  transitions.

Illegal transitions are programming errors, not expected application failures:
they raise :class:`InvalidJobTransition` (which is deliberately *not* an
:class:`~straticate.errors.ApplicationError`).
"""

from straticate.schemas.jobs import JobState

_PROCESSING_ORDER: tuple[JobState, ...] = (
    JobState.QUEUED,
    JobState.PREPARING,
    JobState.DECODING,
    JobState.LOADING_MODEL,
    JobState.SEPARATING,
    JobState.POST_PROCESSING,
    JobState.ENCODING,
    JobState.COMPLETED,
)

_ORDER_INDEX: dict[JobState, int] = {state: i for i, state in enumerate(_PROCESSING_ORDER)}


class InvalidJobTransition(Exception):
    """An illegal job state transition was attempted.

    This signals a programming error in the caller (manager, executor, or
    separator) — it is intentionally not an ``ApplicationError`` and never
    surfaces as a normal API error envelope.
    """


def assert_transition(old: JobState, new: JobState) -> None:
    """Validate a job state transition, raising if it is illegal.

    Args:
        old: The state the job is currently in.
        new: The state the job would move to.

    Raises:
        InvalidJobTransition: If ``old`` is terminal, if ``new`` equals
            ``old``, or if ``new`` lies backward along the processing order.
    """
    if old.is_terminal:
        raise InvalidJobTransition(
            f"cannot leave terminal state {old.value!r} (attempted transition to {new.value!r})"
        )
    if new is JobState.CANCELLED or new is JobState.FAILED:
        return
    if _ORDER_INDEX[new] <= _ORDER_INDEX[old]:
        raise InvalidJobTransition(
            f"cannot move from {old.value!r} to {new.value!r}: "
            "only forward transitions along the processing order are allowed"
        )
