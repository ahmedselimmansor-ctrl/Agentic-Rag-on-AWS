"""Circuit breaker state machine and turn budgets."""

from __future__ import annotations

import asyncio

import pytest

from app.services.resilience import (
    BreakerConfig,
    BudgetExceeded,
    CircuitBreaker,
    CircuitOpen,
    State,
    TurnBudget,
    breaker_health,
    get_breaker,
    reset_all,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_all()
    yield
    reset_all()


async def boom() -> None:
    raise RuntimeError("provider is down")


async def fine() -> str:
    return "ok"


# ------------------------------------------------------------- breaker -----
async def test_starts_closed_and_passes_calls_through():
    breaker = CircuitBreaker("test")
    assert breaker.state is State.closed
    assert await breaker.call(fine) == "ok"


async def test_opens_after_the_failure_threshold():
    breaker = CircuitBreaker("test", BreakerConfig(failure_threshold=3))

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await breaker.call(boom)

    assert breaker.state is State.open


async def test_open_circuit_rejects_without_calling_the_provider():
    """The whole point: stop sending traffic to something known to be down."""
    breaker = CircuitBreaker("test", BreakerConfig(failure_threshold=1))
    with pytest.raises(RuntimeError):
        await breaker.call(boom)

    called = False

    async def should_not_run() -> None:
        nonlocal called
        called = True

    with pytest.raises(CircuitOpen):
        await breaker.call(should_not_run)

    assert called is False


async def test_only_consecutive_failures_count():
    """A blip amid healthy traffic must not accumulate toward opening."""
    breaker = CircuitBreaker("test", BreakerConfig(failure_threshold=3))

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call(boom)
    await breaker.call(fine)  # resets the streak
    with pytest.raises(RuntimeError):
        await breaker.call(boom)

    assert breaker.state is State.closed


async def test_probes_after_the_recovery_window():
    breaker = CircuitBreaker(
        "test", BreakerConfig(failure_threshold=1, recovery_seconds=0.05)
    )
    with pytest.raises(RuntimeError):
        await breaker.call(boom)
    assert breaker.state is State.open

    await asyncio.sleep(0.06)
    assert await breaker.call(fine) == "ok"
    assert breaker.state is State.half_open  # needs 2 successes by default


async def test_closes_only_after_enough_successful_probes():
    breaker = CircuitBreaker(
        "test",
        BreakerConfig(failure_threshold=1, recovery_seconds=0.05, success_threshold=2),
    )
    with pytest.raises(RuntimeError):
        await breaker.call(boom)
    await asyncio.sleep(0.06)

    await breaker.call(fine)
    assert breaker.state is State.half_open
    await breaker.call(fine)
    assert breaker.state is State.closed


async def test_a_failed_probe_reopens_immediately():
    """One lucky probe must not reopen the floodgates onto a still-sick provider."""
    breaker = CircuitBreaker(
        "test", BreakerConfig(failure_threshold=1, recovery_seconds=0.05)
    )
    with pytest.raises(RuntimeError):
        await breaker.call(boom)
    await asyncio.sleep(0.06)

    with pytest.raises(RuntimeError):
        await breaker.call(boom)
    assert breaker.state is State.open

    # And the recovery clock restarted, so it is not immediately probeable.
    with pytest.raises(CircuitOpen):
        await breaker.call(fine)


async def test_acquire_gates_streaming_callers():
    breaker = CircuitBreaker("test", BreakerConfig(failure_threshold=1))
    await breaker.acquire()
    await breaker.record_failure()

    with pytest.raises(CircuitOpen):
        await breaker.acquire()


async def test_registry_returns_one_breaker_per_name():
    assert get_breaker("embeddings") is get_breaker("embeddings")
    assert get_breaker("embeddings") is not get_breaker("rerank")


async def test_health_reports_state_and_counters():
    breaker = get_breaker("x", BreakerConfig(failure_threshold=1))
    with pytest.raises(RuntimeError):
        await breaker.call(boom)
    with pytest.raises(CircuitOpen):
        await breaker.call(fine)

    health = breaker_health()["x"]
    assert health["state"] == "open"
    assert health["total_failures"] == 1
    assert health["rejected"] == 1


# -------------------------------------------------------------- budget -----
def test_fresh_budget_is_not_exhausted():
    assert TurnBudget().exhausted() is None


def test_token_ceiling():
    budget = TurnBudget(max_tokens=100)
    budget.add_tokens(prompt=60, completion=30)
    assert budget.exhausted() is None

    budget.add_tokens(completion=20)
    assert budget.exhausted() == "token"


def test_step_ceiling():
    budget = TurnBudget(max_steps=2)
    budget.start_step()
    assert budget.exhausted() is None
    budget.start_step()
    assert budget.exhausted() == "step"


def test_tool_call_ceiling():
    """A step cap does not bound an agent that makes many calls per step."""
    budget = TurnBudget(max_steps=99, max_tool_calls=3)
    budget.add_tool_calls(3)
    assert budget.exhausted() == "tool-call"


def test_time_ceiling():
    budget = TurnBudget(max_seconds=0.0)
    assert budget.exhausted() == "time"
    assert budget.remaining_seconds == 0.0


def test_check_raises_with_the_reason():
    budget = TurnBudget(max_tokens=10)
    budget.add_tokens(completion=50)

    with pytest.raises(BudgetExceeded) as exc:
        budget.check()
    assert exc.value.kind == "token"


def test_snapshot_exposes_what_the_trace_needs():
    budget = TurnBudget(max_tokens=1000)
    budget.add_tokens(prompt=100, completion=50)
    budget.start_step()
    budget.add_tool_calls(2)

    snapshot = budget.snapshot()
    assert snapshot["tokens"] == 150
    assert snapshot["steps"] == 1
    assert snapshot["tool_calls"] == 2
    assert snapshot["exhausted"] is None
