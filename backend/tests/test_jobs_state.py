"""Tests for the job state-machine transition rules."""

import itertools

import pytest

from straticate.errors import ApplicationError
from straticate.jobs import InvalidJobTransition, assert_transition
from straticate.schemas.jobs import JobState

_PROCESSING_ORDER = [
    JobState.QUEUED,
    JobState.PREPARING,
    JobState.DECODING,
    JobState.LOADING_MODEL,
    JobState.SEPARATING,
    JobState.POST_PROCESSING,
    JobState.ENCODING,
    JobState.COMPLETED,
]

_TERMINAL = [JobState.COMPLETED, JobState.CANCELLED, JobState.FAILED]
_NON_TERMINAL = [state for state in JobState if state not in _TERMINAL]


def test_adjacent_forward_transitions_are_allowed() -> None:
    for old, new in itertools.pairwise(_PROCESSING_ORDER):
        assert_transition(old, new)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (JobState.QUEUED, JobState.SEPARATING),
        (JobState.QUEUED, JobState.COMPLETED),
        (JobState.PREPARING, JobState.ENCODING),
        (JobState.DECODING, JobState.SEPARATING),
    ],
)
def test_skipping_stages_forward_is_allowed(old: JobState, new: JobState) -> None:
    assert_transition(old, new)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (JobState.SEPARATING, JobState.DECODING),
        (JobState.ENCODING, JobState.QUEUED),
        (JobState.PREPARING, JobState.QUEUED),
    ],
)
def test_backward_transitions_raise(old: JobState, new: JobState) -> None:
    with pytest.raises(InvalidJobTransition):
        assert_transition(old, new)


@pytest.mark.parametrize("state", _NON_TERMINAL)
def test_same_state_is_not_a_transition(state: JobState) -> None:
    with pytest.raises(InvalidJobTransition):
        assert_transition(state, state)


@pytest.mark.parametrize("old", _NON_TERMINAL)
@pytest.mark.parametrize("new", [JobState.CANCELLED, JobState.FAILED])
def test_any_non_terminal_state_may_cancel_or_fail(old: JobState, new: JobState) -> None:
    assert_transition(old, new)


@pytest.mark.parametrize("old", _TERMINAL)
@pytest.mark.parametrize("new", list(JobState))
def test_terminal_states_allow_no_transitions(old: JobState, new: JobState) -> None:
    with pytest.raises(InvalidJobTransition):
        assert_transition(old, new)


def test_invalid_transition_is_a_programming_error_not_an_application_error() -> None:
    assert not issubclass(InvalidJobTransition, ApplicationError)
