"""Circuit breakers and per-turn budgets.

Two distinct failure modes, two mechanisms:

**A provider is down.** Retrying into a dead dependency turns one outage into
queued requests, exhausted connections, and a slow failure for everyone —
including the parts of the system that do not need that provider. The breaker
fails fast instead, and probes occasionally to notice recovery.

**A single turn runs away.** An agent loop can burn tokens and wall-clock in
ways a per-request timeout does not catch: six tool rounds, each individually
fast. The budget bounds the whole turn, so one pathological question cannot
consume a task's capacity.

Both degrade rather than crash. Retrieval falling back to sparse-only is a
worse answer; a hung request is no answer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class State(str, Enum):
    closed = "closed"        # healthy, calls pass through
    open = "open"            # failing, calls rejected immediately
    half_open = "half_open"  # probing, one call allowed through


class CircuitOpen(RuntimeError):
    """Raised instead of calling a provider known to be failing."""

    def __init__(self, name: str, retry_after: float) -> None:
        self.name = name
        self.retry_after = retry_after
        super().__init__(
            f"{name} is unavailable (circuit open, retrying in {retry_after:.0f}s)"
        )


@dataclass
class BreakerConfig:
    # Consecutive failures before opening. Low enough to react, high enough
    # that a single blip does not trip it.
    failure_threshold: int = 5
    # How long to stay open before probing.
    recovery_seconds: float = 30.0
    # Consecutive successes in half-open before closing again. >1 so a single
    # lucky probe does not reopen the floodgates onto a still-sick provider.
    success_threshold: int = 2


@dataclass
class BreakerStats:
    state: State = State.closed
    failures: int = 0
    successes: int = 0
    opened_at: float = 0.0
    total_calls: int = 0
    total_failures: int = 0
    total_rejected: int = 0


class CircuitBreaker:
    """One breaker per provider. Async-safe."""

    def __init__(self, name: str, config: BreakerConfig | None = None) -> None:
        self.name = name
        self.config = config or BreakerConfig()
        self._stats = BreakerStats()
        self._lock = asyncio.Lock()

    @property
    def state(self) -> State:
        return self._stats.state

    @property
    def stats(self) -> BreakerStats:
        return self._stats

    def _retry_after(self, now: float) -> float:
        return max(0.0, self.config.recovery_seconds - (now - self._stats.opened_at))

    async def _before_call(self) -> None:
        async with self._lock:
            self._stats.total_calls += 1
            if self._stats.state is State.open:
                now = time.monotonic()
                if now - self._stats.opened_at >= self.config.recovery_seconds:
                    logger.info("circuit %s: open -> half_open (probing)", self.name)
                    self._stats.state = State.half_open
                    self._stats.successes = 0
                else:
                    self._stats.total_rejected += 1
                    raise CircuitOpen(self.name, self._retry_after(now))

    async def record_success(self) -> None:
        async with self._lock:
            if self._stats.state is State.half_open:
                self._stats.successes += 1
                if self._stats.successes >= self.config.success_threshold:
                    logger.info("circuit %s: half_open -> closed (recovered)", self.name)
                    self._stats.state = State.closed
                    self._stats.failures = 0
                    self._stats.successes = 0
            else:
                # Only *consecutive* failures count, so a healthy call resets.
                self._stats.failures = 0

    async def record_failure(self) -> None:
        async with self._lock:
            self._stats.total_failures += 1

            if self._stats.state is State.half_open:
                # The probe failed; go straight back to open and restart the clock.
                logger.warning("circuit %s: half_open -> open (probe failed)", self.name)
                self._stats.state = State.open
                self._stats.opened_at = time.monotonic()
                self._stats.successes = 0
                return

            self._stats.failures += 1
            if self._stats.failures >= self.config.failure_threshold:
                logger.error(
                    "circuit %s: closed -> open after %d consecutive failures",
                    self.name,
                    self._stats.failures,
                )
                self._stats.state = State.open
                self._stats.opened_at = time.monotonic()

    async def acquire(self) -> None:
        """Entry point for callers that cannot use `call()`.

        A streaming generator outlives the coroutine that started it, so the
        caller must gate on entry and report the outcome itself once the stream
        finishes or raises.
        """
        await self._before_call()

    async def call(self, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """Run `fn` through the breaker. Raises CircuitOpen without calling it
        when the provider is known to be down."""
        await self._before_call()
        try:
            result = await fn(*args, **kwargs)
        except Exception:
            await self.record_failure()
            raise
        await self.record_success()
        return result

    async def reset(self) -> None:
        async with self._lock:
            self._stats = BreakerStats()


# One breaker per provider: embeddings failing must not stop generation, and
# vice versa. Sharing one would couple independent dependencies.
_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(name: str, config: BreakerConfig | None = None) -> CircuitBreaker:
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(name, config)
    return _breakers[name]


def all_breakers() -> dict[str, CircuitBreaker]:
    return dict(_breakers)


def breaker_health() -> dict[str, dict[str, Any]]:
    """For the readiness endpoint — an open breaker is a real degradation even
    though every individual component is technically 'up'."""
    return {
        name: {
            "state": breaker.state.value,
            "consecutive_failures": breaker.stats.failures,
            "total_calls": breaker.stats.total_calls,
            "total_failures": breaker.stats.total_failures,
            "rejected": breaker.stats.total_rejected,
        }
        for name, breaker in _breakers.items()
    }


def reset_all() -> None:
    """Test hook — module-level breakers otherwise leak state between tests."""
    _breakers.clear()


# ============================================================== budgets =====
class BudgetExceeded(RuntimeError):
    """The turn hit a ceiling. The caller should finish with what it has."""

    def __init__(self, kind: str, used: float, limit: float) -> None:
        self.kind = kind
        self.used = used
        self.limit = limit
        super().__init__(f"turn exceeded its {kind} budget ({used:.0f} of {limit:.0f})")


@dataclass
class TurnBudget:
    """Bounds one agent turn across tokens, wall-clock, and tool rounds.

    A per-request timeout does not catch an agent that makes six individually
    fast tool calls, and a step cap does not catch one enormous context. Both
    ceilings are needed.
    """

    max_tokens: int = 100_000
    max_seconds: float = 300.0
    max_steps: int = 6
    max_tool_calls: int = 12

    tokens_used: int = 0
    steps_used: int = 0
    tool_calls_used: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.max_seconds - self.elapsed)

    def add_tokens(self, prompt: int = 0, completion: int = 0) -> None:
        self.tokens_used += prompt + completion

    def start_step(self) -> None:
        self.steps_used += 1

    def add_tool_calls(self, count: int = 1) -> None:
        self.tool_calls_used += count

    def exhausted(self) -> str | None:
        """The reason this turn should stop, or None to continue."""
        if self.tokens_used >= self.max_tokens:
            return "token"
        if self.elapsed >= self.max_seconds:
            return "time"
        if self.steps_used >= self.max_steps:
            return "step"
        if self.tool_calls_used >= self.max_tool_calls:
            return "tool-call"
        return None

    def check(self) -> None:
        """Raise if any ceiling is hit. Prefer `exhausted()` inside the agent
        loop — running out of budget should produce a final answer, not an
        error the user sees."""
        kind = self.exhausted()
        if kind == "token":
            raise BudgetExceeded("token", self.tokens_used, self.max_tokens)
        if kind == "time":
            raise BudgetExceeded("time", self.elapsed, self.max_seconds)
        if kind == "step":
            raise BudgetExceeded("step", self.steps_used, self.max_steps)
        if kind == "tool-call":
            raise BudgetExceeded("tool-call", self.tool_calls_used, self.max_tool_calls)

    def snapshot(self) -> dict[str, Any]:
        return {
            "tokens": self.tokens_used,
            "tokens_limit": self.max_tokens,
            "steps": self.steps_used,
            "tool_calls": self.tool_calls_used,
            "elapsed_ms": int(self.elapsed * 1000),
            "exhausted": self.exhausted(),
        }
