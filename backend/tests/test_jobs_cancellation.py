"""Tests for the cooperative cancellation token."""

import pytest

from straticate.jobs import CancellationToken, JobCancelled


def test_fresh_token_is_not_cancelled() -> None:
    token = CancellationToken()
    assert not token.is_cancelled
    token.raise_if_cancelled()  # no-op


def test_cancel_sets_the_flag_and_raise_if_cancelled_raises() -> None:
    token = CancellationToken()
    token.cancel()
    assert token.is_cancelled
    with pytest.raises(JobCancelled):
        token.raise_if_cancelled()


def test_cancel_is_idempotent() -> None:
    token = CancellationToken()
    token.cancel()
    token.cancel()
    assert token.is_cancelled
