"""Tests for the per-account exponential backoff."""

from __future__ import annotations

from mailwatch.backoff import ConnectionBackoff


def _backoff(**kw) -> ConnectionBackoff:
    kw.setdefault("base_delay", 60)
    kw.setdefault("jitter", 0)
    return ConnectionBackoff(**kw)


def test_first_failure_delay_is_base():
    assert _backoff().record_failure("a@x") == 60


def test_delay_grows_exponentially_and_caps():
    b = _backoff(base_delay=60, factor=2, max_delay=300)
    delays = [b.record_failure("a@x") for _ in range(5)]
    assert delays == [60, 120, 240, 300, 300]


def test_should_attempt_gates_within_window():
    now = [1000.0]
    b = ConnectionBackoff(base_delay=60, jitter=0, clock=lambda: now[0])
    assert b.should_attempt("a@x") is True
    b.record_failure("a@x")
    now[0] += 30
    assert b.should_attempt("a@x") is False
    now[0] += 30
    assert b.should_attempt("a@x") is True


def test_success_resets_counter_and_window():
    now = [0.0]
    b = ConnectionBackoff(base_delay=60, jitter=0, clock=lambda: now[0])
    b.record_failure("a@x")
    b.record_failure("a@x")
    assert b.consecutive_failures("a@x") == 2
    b.record_success("a@x")
    assert b.consecutive_failures("a@x") == 0
    assert b.should_attempt("a@x") is True


def test_accounts_are_independent():
    b = _backoff()
    b.record_failure("a@x")
    assert b.should_attempt("b@x") is True
    assert b.consecutive_failures("b@x") == 0


def test_jitter_stays_within_bounds():
    b = ConnectionBackoff(base_delay=100, jitter=0.2, clock=lambda: 0.0)
    first = b.record_failure("a@x")
    assert 80 <= first <= 120
    assert all(b.record_failure("a@x") >= 0 for _ in range(20))
