"""F12: the asyncio pipeline — bounded fetch, dedup, staged probes, find-N.

This module is a leaf.  It owns no database, no source fetcher of its own and
no socket: the byte stream arrives through an injected :class:`SourceSpec`, the
addresses are canonicalised by the single normaliser of the project
(``proxytool.normalize``, see :func:`normalize_default`), and every measurement
goes through an injected runner whose contract is documented on
:class:`Runners`.  The integration wires ``probes.run_plan`` and the existing
HTTP transport to that contract (CONTRACTS.ru.md §1.2, HANDOFF §2.2).

The shape of the chain is the one F12 asks for::

    bounded source fetch -> parser -> normalization/dedup
        -> cheap probe -> basic probe -> necessary expensive probes

with first results delivered as they arrive, prior known-good ordered with an
age correction, find-N, backpressure, adaptive concurrency and per-host /
per-target limits.

Three rules run through the whole module and are asserted by the tests:

* **Resources, not workers.**  The worker count is *derived* from the fd, RAM
  and in-flight budgets (:meth:`Budgets.worker_ceiling`), and the ceiling moves
  with the observed success rate (:class:`AdaptiveConcurrency`).  A bigger
  number of workers is a consequence, never a knob.
* **Three different N.**  ``N endpoints``, ``N unique IPs`` and ``N confirmed
  exit IPs`` are three numbers and are never interchanged (:class:`FindPolicy`,
  :class:`Counters`).  A hostname endpoint has an address but no IP, and an
  endpoint nobody measured through a target has no exit IP.
* **No item is measured twice.**  A canonical endpoint enters the chain once
  (:class:`Ledger`), and that survives a pause/resume cycle.  A fresh prior that
  is carried instead of re-probed is not re-probed later either.

The benchmark helper (:func:`benchmark`) runs on a local deterministic fixture
built from the RFC 5737 / RFC 3849 documentation ranges and touches no network.
Its numbers describe that fixture and nothing else — see :data:`SYNTHETIC_NOTICE`.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import ipaddress
import math
import time
import tracemalloc
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Iterator, Mapping, Sequence

__all__ = [
    # stages, states, codes
    'STAGES', 'STAGE_SOURCE', 'STAGE_PARSE', 'STAGE_NORMALIZE', 'STAGE_DEDUP',
    'STAGE_CHEAP', 'STAGE_BASIC', 'STAGE_EXPENSIVE', 'PIPELINE_STAGES',
    'FIND_BY', 'COUNT_FIELD', 'EXPENSIVE_POLICIES', 'EXPENSIVE_NONE',
    'EXPENSIVE_ALL_PASSING', 'EXPENSIVE_UNTIL_N', 'RUN_STATES', 'STOP_REASONS',
    'ITEM_STATES', 'EMITTED_ITEM_STATES', 'DEADLINE_EXCEEDED',
    'E_LIMIT_BUDGET', 'E_LIMIT_BODY', 'E_VALIDATION_FIELD', 'E_VALIDATION_SCHEMA',
    # errors
    'PipelineError', 'ValidationError', 'BudgetExhausted',
    # budgets and governance
    'Budgets', 'Limits', 'TargetPolicy', 'ResourceGate', 'ResourceSnapshot',
    'AdaptiveConcurrency', 'ConcurrencySample', 'HostLimiter',
    # clock
    'PipelineClock', 'SystemClock',
    # prior
    'PriorItem', 'PriorIndex', 'decay_weight', 'AGE_HALFLIFE_S',
    # items and results
    'Item', 'item_from_endpoint', 'StageLimit', 'StageOutcome', 'ItemResult', 'Counters',
    'FindPolicy', 'FindProgress', 'RunMetrics', 'RunResult', 'Ledger',
    # sources
    'SourceSpec', 'parse_lines', 'parse_delimited', 'normalize_default',
    # pipeline
    'Runners', 'PipelineConfig', 'PipelineControl', 'Pipeline', 'run_pipeline',
    # benchmarks
    'BENCHMARK_SCHEMA', 'BENCHMARK_FIXTURE', 'SYNTHETIC_NOTICE', 'Benchmark', 'benchmark',
    'fixture_digest', 'fixture_normalize', 'fixture_runner', 'fixture_source', 'synthetic_endpoints',
]

# --------------------------------------------------------------------------
# stages, states and codes
# --------------------------------------------------------------------------

STAGE_SOURCE = 'source'
STAGE_PARSE = 'parse'
STAGE_NORMALIZE = 'normalize'
STAGE_DEDUP = 'dedup'
STAGE_CHEAP = 'cheap'
STAGE_BASIC = 'basic'
STAGE_EXPENSIVE = 'expensive'

#: Execution order.  The first four are the front half and run in the feeder;
#: the last three are the measurement half and run in the workers.
STAGES = (STAGE_SOURCE, STAGE_PARSE, STAGE_NORMALIZE, STAGE_DEDUP,
          STAGE_CHEAP, STAGE_BASIC, STAGE_EXPENSIVE)
PIPELINE_STAGES = frozenset(STAGES)
MEASUREMENT_PIPELINE_STAGES = (STAGE_CHEAP, STAGE_BASIC, STAGE_EXPENSIVE)

# The three counts F12 refuses to conflate.
FIND_BY = ('endpoint', 'ip', 'exit')

#: Which counter each find-N mode advances.  ``exit`` is the only unit that
#: needs a confirmed exit address, which is why it is not the same as ``ip``.
COUNT_FIELD = {
    'endpoint': 'passed_endpoints',
    'ip': 'passed_unique_ips',
    'exit': 'passed_exit_ips',
}

EXPENSIVE_NONE = 'none'
EXPENSIVE_ALL_PASSING = 'all_passing'
EXPENSIVE_UNTIL_N = 'until_n'
EXPENSIVE_POLICIES = (EXPENSIVE_NONE, EXPENSIVE_ALL_PASSING, EXPENSIVE_UNTIL_N)

RUN_STATES = ('complete', 'partial', 'want_reached', 'paused', 'cancelled', 'budget')

#: A subset of ``jobs.ITEM_STATES`` (CONTRACTS §6.3).  The pipeline emits only
#: the three it can justify, so ``jobs`` maps a result onto its own state
#: machine without a second table.  ``partial`` and ``blocked`` stay reserved.
ITEM_STATES = ('unreachable', 'done', 'failed', 'partial', 'blocked')

#: The subset the pipeline actually produces.  ``jobs`` maps these onto its own
#: state machine; ``partial`` and ``blocked`` are left to it.
EMITTED_ITEM_STATES = ('unreachable', 'done', 'failed')

STOP_REASONS = ('items_exhausted', 'want_reached', 'paused', 'cancelled',
                'budget_exhausted', 'deadline_exceeded', 'stopped')

# Codes come from the LIMIT and VALIDATION domains of CONTRACTS §5.4; the
# pipeline introduces no new domain.  A deadline is a budget, so it carries the
# bare measurement-style reason the run stopped rather than a TIME code that
# belongs to a row.
E_LIMIT_BUDGET = 'E_LIMIT_BUDGET'
E_LIMIT_BODY = 'E_LIMIT_BODY'
E_VALIDATION_FIELD = 'E_VALIDATION_FIELD'
E_VALIDATION_SCHEMA = 'E_VALIDATION_SCHEMA'
DEADLINE_EXCEEDED = 'DEADLINE_EXCEEDED'

#: Agreement with ``proxytool.DEFAULT_SOURCE_MAX_LINE_BYTES``.
DEFAULT_MAX_LINE_BYTES = 64 * 1024


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class PipelineError(Exception):
    """Base error.  ``code`` is a stable ``E_*`` or measurement code."""

    code = E_VALIDATION_SCHEMA

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self):
        return f'{self.code}: {self.message}'


class ValidationError(PipelineError):
    code = E_VALIDATION_FIELD


class BudgetExhausted(PipelineError):
    """A *total* budget is spent, which ends the run.

    A *ceiling* (adaptive concurrency, fds, RAM, queue depth) never raises: it
    blocks the caller instead, which is the backpressure the chain needs.
    """

    code = E_LIMIT_BUDGET


# --------------------------------------------------------------------------
# clock
# --------------------------------------------------------------------------


class PipelineClock:
    """Time source seam.

    ``remaining_s`` maps a monotonic deadline to a *real* timeout.  A virtual
    clock returns ``None`` and relies on the boundary checks alone, which is
    what makes a virtual benchmark exactly repeatable.
    """

    def monotonic(self) -> float:  # pragma: no cover - interface
        raise NotImplementedError

    def time(self) -> float:  # pragma: no cover - interface
        raise NotImplementedError

    def cpu(self) -> float:  # pragma: no cover - interface
        raise NotImplementedError

    def remaining_s(self, deadline: float) -> float | None:
        return max(0.0, deadline - self.monotonic())


class SystemClock(PipelineClock):
    """Default: ``perf_counter`` for durations, wall clock for timestamps,
    ``process_time`` for the CPU budget."""

    def monotonic(self) -> float:
        return time.perf_counter()

    def time(self) -> float:
        return time.time()

    def cpu(self) -> float:
        return time.process_time()


# --------------------------------------------------------------------------
# budgets
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Budgets:
    """Every ceiling and total of a run, in one place with its unit (CONTRACTS §5.5)."""

    # ceilings — hitting them blocks, it never ends the run
    max_inflight: int = 64
    max_open_fds: int = 256
    fds_per_request: int = 3
    max_ram_bytes: int = 64 * 1024 * 1024
    ram_per_inflight_bytes: int = 256 * 1024
    max_queue_items: int = 128
    max_results_pending: int = 64
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES
    # totals — hitting them ends the run with state='budget'
    max_requests: int | None = 200_000
    max_bytes: int | None = 512 * 1024 * 1024
    max_source_bytes: int | None = 32 * 1024 * 1024
    max_items: int | None = None
    max_cpu_seconds: float | None = None
    deadline_s: float | None = None
    max_stored_results: int = 4096

    def __post_init__(self):
        for name in ('max_inflight', 'max_open_fds', 'fds_per_request', 'max_queue_items',
                     'max_results_pending', 'max_line_bytes', 'max_stored_results'):
            self._int(name, getattr(self, name), 1)
        for name in ('max_requests', 'max_bytes', 'max_source_bytes', 'max_items'):
            value = getattr(self, name)
            if value is not None:
                self._int(name, value, 0)
        for name in ('max_ram_bytes', 'ram_per_inflight_bytes'):
            self._int(name, getattr(self, name), 1)
        for name in ('max_cpu_seconds', 'deadline_s'):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)
                                      or not math.isfinite(value) or value <= 0):
                raise ValidationError(E_VALIDATION_FIELD,
                                      f'budgets.{name}: ожидается число > 0 или None, получено {value!r}.')
        if self.fd_ceiling < 1 or self.ram_ceiling < 1:
            raise ValidationError(E_VALIDATION_FIELD,
                                  'Бюджеты FD и RAM не оставляют ни одного слота: увеличьте max_open_fds '
                                  'или max_ram_bytes либо уменьшите fds_per_request / ram_per_inflight_bytes.')

    @staticmethod
    def _int(name: str, value: Any, low: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value < low:
            raise ValidationError(E_VALIDATION_FIELD,
                                  f'budgets.{name}: ожидается целое >= {low}, получено {value!r}.')

    @property
    def fd_ceiling(self) -> int:
        """How many in-flight probes the file-descriptor budget allows."""
        return self.max_open_fds // self.fds_per_request

    @property
    def ram_ceiling(self) -> int:
        """How many in-flight probes the RAM budget allows."""
        return self.max_ram_bytes // self.ram_per_inflight_bytes

    def worker_ceiling(self) -> int:
        """The worker count, derived — never configured directly (F12)."""
        return max(1, min(self.max_inflight, self.fd_ceiling, self.ram_ceiling))

    def to_public(self) -> dict:
        return {
            'max_inflight': self.max_inflight, 'max_open_fds': self.max_open_fds,
            'fds_per_request': self.fds_per_request, 'max_ram_bytes': self.max_ram_bytes,
            'ram_per_inflight_bytes': self.ram_per_inflight_bytes, 'fd_ceiling': self.fd_ceiling,
            'ram_ceiling': self.ram_ceiling, 'worker_ceiling': self.worker_ceiling(),
            'max_queue_items': self.max_queue_items, 'max_line_bytes': self.max_line_bytes,
            'max_requests': self.max_requests, 'max_bytes': self.max_bytes,
            'max_source_bytes': self.max_source_bytes, 'max_items': self.max_items,
            'max_cpu_seconds': self.max_cpu_seconds, 'deadline_s': self.deadline_s,
        }


@dataclass(frozen=True)
class TargetPolicy:
    """One target's own limits, so a single slow target cannot eat the job."""

    target_id: str
    max_inflight: int = 4
    max_requests: int | None = None

    def __post_init__(self):
        if not isinstance(self.target_id, str) or not self.target_id:
            raise ValidationError(E_VALIDATION_FIELD, 'TargetPolicy.target_id: непустая строка.')
        if not isinstance(self.max_inflight, int) or isinstance(self.max_inflight, bool) or self.max_inflight < 1:
            raise ValidationError(E_VALIDATION_FIELD, 'TargetPolicy.max_inflight: целое >= 1.')
        if self.max_requests is not None and (not isinstance(self.max_requests, int)
                                              or isinstance(self.max_requests, bool)
                                              or self.max_requests < 0):
            raise ValidationError(E_VALIDATION_FIELD, 'TargetPolicy.max_requests: целое >= 0 или None.')


@dataclass(frozen=True)
class Limits:
    """Per-host and per-target limits; everything not named here is global."""

    max_per_host: int = 1
    min_host_interval_s: float = 0.0
    targets: tuple[TargetPolicy, ...] = ()

    def __post_init__(self):
        if not isinstance(self.max_per_host, int) or isinstance(self.max_per_host, bool) or self.max_per_host < 1:
            raise ValidationError(E_VALIDATION_FIELD, 'limits.max_per_host: целое >= 1.')
        if (not isinstance(self.min_host_interval_s, (int, float)) or isinstance(self.min_host_interval_s, bool)
                or not math.isfinite(self.min_host_interval_s) or self.min_host_interval_s < 0):
            raise ValidationError(E_VALIDATION_FIELD, 'limits.min_host_interval_s: число >= 0.')
        seen = set()
        for policy in self.targets:
            if policy.target_id in seen:
                raise ValidationError(E_VALIDATION_FIELD,
                                      f'limits.targets: повторяется target_id={policy.target_id!r}.')
            seen.add(policy.target_id)

    def to_public(self) -> dict:
        return {'max_per_host': self.max_per_host, 'min_host_interval_s': self.min_host_interval_s,
                'targets': [{'target_id': item.target_id, 'max_inflight': item.max_inflight,
                             'max_requests': item.max_requests} for item in self.targets]}


# --------------------------------------------------------------------------
# resource governance
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceSnapshot:
    """What was reserved at the peak, and what was spent in total."""

    inflight: int = 0
    peak_inflight: int = 0
    peak_fds: int = 0
    peak_ram_bytes: int = 0
    requests: int = 0
    bytes: int = 0
    cpu_s: float = 0.0
    blocked_waits: int = 0

    def to_public(self) -> dict:
        return {'inflight': self.inflight, 'peak_inflight': self.peak_inflight, 'peak_fds': self.peak_fds,
                'peak_ram_bytes': self.peak_ram_bytes, 'requests': self.requests, 'bytes': self.bytes,
                'cpu_s': round(self.cpu_s, 3), 'blocked_waits': self.blocked_waits}


class ResourceGate:
    """Admission control for the measurement half, in resource units.

    :meth:`acquire` blocks while a *ceiling* (adaptive concurrency, fds, RAM) is
    full; :meth:`check_totals` ends the run when a *total* (requests, bytes, CPU,
    deadline) is spent.  The two are deliberately different — a run that is
    merely waiting must never be reported as out of budget.
    """

    def __init__(self, budgets: Budgets, clock: PipelineClock, *, limit_provider=None,
                 deadline: float | None = None):
        self.budgets = budgets
        self.clock = clock
        self.deadline = deadline
        self._limit_provider = limit_provider or (lambda: budgets.max_inflight)
        self._cond = asyncio.Condition()
        self._inflight = 0
        self._fds = 0
        self._ram = 0
        self._peak_inflight = 0
        self._peak_fds = 0
        self._peak_ram = 0
        self._requests = 0
        self._bytes = 0
        self._blocked_waits = 0
        self._cpu_start = clock.cpu()

    # -- totals -------------------------------------------------------------

    @property
    def cpu_s(self) -> float:
        return self.clock.cpu() - self._cpu_start

    @property
    def requests(self) -> int:
        return self._requests

    @property
    def bytes(self) -> int:
        return self._bytes

    def check_totals(self) -> None:
        """Raise :class:`BudgetExhausted` when a total budget is spent."""
        budgets = self.budgets
        if budgets.max_requests is not None and self._requests >= budgets.max_requests:
            raise BudgetExhausted(E_LIMIT_BUDGET,
                                  f'Лимит запросов исчерпан: {self._requests} из {budgets.max_requests}.')
        if budgets.max_bytes is not None and self._bytes >= budgets.max_bytes:
            raise BudgetExhausted(E_LIMIT_BUDGET,
                                  f'Лимит байтов исчерпан: {self._bytes} из {budgets.max_bytes}.')
        if budgets.max_cpu_seconds is not None and self.cpu_s >= budgets.max_cpu_seconds:
            raise BudgetExhausted(E_LIMIT_BUDGET,
                                  f'Лимит CPU исчерпан: {self.cpu_s:.2f} с из {budgets.max_cpu_seconds:g} с.')
        if self.deadline is not None and self.clock.monotonic() >= self.deadline:
            raise BudgetExhausted(DEADLINE_EXCEEDED, 'Общий срок задания исчерпан.')

    def remaining_requests(self) -> int | None:
        if self.budgets.max_requests is None:
            return None
        return max(0, self.budgets.max_requests - self._requests)

    def remaining_bytes(self) -> int | None:
        if self.budgets.max_bytes is None:
            return None
        return max(0, self.budgets.max_bytes - self._bytes)

    def remaining_s(self) -> float | None:
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - self.clock.monotonic())

    # -- reservations -------------------------------------------------------

    def _limit(self) -> int:
        """The in-flight ceiling right now: the adaptive limit, never above
        the budget's own."""
        return max(1, min(int(self._limit_provider()), self.budgets.max_inflight))

    async def acquire(self, *, fds: int = 1, ram: int = 0) -> None:
        """Reserve one in-flight slot plus its fds and RAM, waiting if needed."""
        self.check_totals()
        waited = False
        async with self._cond:
            while True:
                if (self._inflight < self._limit()
                        and self._fds + fds <= self.budgets.max_open_fds
                        and self._ram + ram <= self.budgets.max_ram_bytes):
                    break
                if not waited:
                    waited = True
                    self._blocked_waits += 1
                await self._cond.wait()
            self._inflight += 1
            self._fds += fds
            self._ram += ram
            self._peak_inflight = max(self._peak_inflight, self._inflight)
            self._peak_fds = max(self._peak_fds, self._fds)
            self._peak_ram = max(self._peak_ram, self._ram)

    async def release(self, *, fds: int = 1, ram: int = 0, requests: int = 0, bytes: int = 0) -> None:
        """Give the slot back and charge what the measurement actually spent."""
        async with self._cond:
            self._inflight = max(0, self._inflight - 1)
            self._fds = max(0, self._fds - fds)
            self._ram = max(0, self._ram - ram)
            self._requests += max(0, int(requests))
            self._bytes += max(0, int(bytes))
            self._cond.notify_all()

    def note_queue(self, size: int, high_water: int) -> int:
        return high_water if size <= high_water else size

    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(inflight=self._inflight, peak_inflight=self._peak_inflight,
                               peak_fds=self._peak_fds, peak_ram_bytes=self._peak_ram,
                               requests=self._requests, bytes=self._bytes, cpu_s=self.cpu_s,
                               blocked_waits=self._blocked_waits)


@dataclass(frozen=True)
class ConcurrencySample:
    limit: int
    window: int
    success_rate: float | None
    mean_latency_s: float | None
    increases: int
    decreases: int

    def to_public(self) -> dict:
        return {'limit': self.limit, 'window': self.window,
                'success_rate': None if self.success_rate is None else round(self.success_rate, 3),
                'mean_latency_s': None if self.mean_latency_s is None else round(self.mean_latency_s, 4),
                'increases': self.increases, 'decreases': self.decreases}


class AdaptiveConcurrency:
    """AIMD on a success-rate window, clamped to its own bounds.

    The value it returns is an upper bound the :class:`ResourceGate` waits on,
    and the fds/RAM budgets are applied on top of it, so raising ``maximum``
    cannot escape the resource budgets.
    """

    def __init__(self, *, minimum: int = 1, maximum: int = 64, target_success: float = 0.8,
                 window: int = 16, increase: int = 2, decrease_factor: float = 0.5):
        for name, value, low in (('minimum', minimum, 1), ('maximum', maximum, 1),
                                 ('window', window, 1), ('increase', increase, 1)):
            if not isinstance(value, int) or isinstance(value, bool) or value < low:
                raise ValidationError(E_VALIDATION_FIELD,
                                      f'AdaptiveConcurrency.{name}: целое >= {low}, получено {value!r}.')
        if maximum < minimum:
            raise ValidationError(E_VALIDATION_FIELD, 'AdaptiveConcurrency.maximum меньше minimum.')
        if not 0.0 < target_success <= 1.0:
            raise ValidationError(E_VALIDATION_FIELD, 'AdaptiveConcurrency.target_success: доля в (0, 1].')
        if not 0.0 < decrease_factor < 1.0:
            raise ValidationError(E_VALIDATION_FIELD, 'AdaptiveConcurrency.decrease_factor: доля в (0, 1).')
        self.minimum = minimum
        self.maximum = maximum
        self.target_success = float(target_success)
        self.window = window
        self.increase = increase
        self.decrease_factor = float(decrease_factor)
        self._limit = max(minimum, min(maximum, window))
        self._results: deque[bool] = deque(maxlen=window)
        self._latencies: deque[float] = deque(maxlen=window)
        self._increases = 0
        self._decreases = 0

    @property
    def limit(self) -> int:
        return self._limit

    def observe(self, ok: bool, latency_s: float | None = None) -> bool:
        """Feed one finished measurement.  Returns True when the limit moved."""
        self._results.append(bool(ok))
        if latency_s is not None:
            self._latencies.append(float(latency_s))
        if len(self._results) < self.window:
            return False
        success = sum(1 for item in self._results if item) / len(self._results)
        before = self._limit
        if success >= self.target_success:
            self._limit = min(self.maximum, self._limit + self.increase)
            if self._limit != before:
                self._increases += 1
        elif success < self.target_success * 0.5:
            self._limit = max(self.minimum, int(self._limit * self.decrease_factor))
            if self._limit != before:
                self._decreases += 1
        return self._limit != before

    def snapshot(self) -> ConcurrencySample:
        success = sum(1 for item in self._results if item) / len(self._results) if self._results else None
        latency = sum(self._latencies) / len(self._latencies) if self._latencies else None
        return ConcurrencySample(limit=self._limit, window=len(self._results), success_rate=success,
                                 mean_latency_s=latency, increases=self._increases, decreases=self._decreases)


class HostLimiter:
    """At most ``max_per_host`` measurements per host address, plus an optional
    minimum interval between two measurements of the same host."""

    def __init__(self, *, max_per_host: int = 1, min_interval_s: float = 0.0,
                 clock: PipelineClock | None = None):
        self.max_per_host = max(1, int(max_per_host))
        self.min_interval_s = float(min_interval_s)
        self.clock = clock or SystemClock()
        self._cond = asyncio.Condition()
        self._busy: dict[str, int] = {}
        self._last: dict[str, float] = {}
        self.waits = 0

    def free_now(self, host: str) -> bool:
        if self._busy.get(host, 0) >= self.max_per_host:
            return False
        if self.min_interval_s <= 0:
            return True
        last = self._last.get(host)
        return last is None or self.clock.monotonic() - last >= self.min_interval_s

    async def acquire(self, host: str) -> None:
        waited = False
        async with self._cond:
            while not self.free_now(host):
                if not waited:
                    waited = True
                    self.waits += 1
                await self._cond.wait()
            self._busy[host] = self._busy.get(host, 0) + 1

    async def release(self, host: str) -> None:
        async with self._cond:
            self._busy[host] = max(0, self._busy.get(host, 0) - 1)
            if self._busy[host] == 0:
                self._last[host] = self.clock.monotonic()
            self._cond.notify_all()


# --------------------------------------------------------------------------
# prior known-good, corrected for age
# --------------------------------------------------------------------------

AGE_HALFLIFE_S = 900.0


def decay_weight(value: float, age_s: float, *, halflife_s: float = AGE_HALFLIFE_S) -> float:
    """Half-life decay of a prior value.

    Age is clamped at zero, so a prior stamped in the future (a clock anomaly,
    the case ``core.time_state_of`` reports as ``time_future``) is ordered as
    brand new rather than promoted above a fresh one.
    """
    if not isinstance(halflife_s, (int, float)) or isinstance(halflife_s, bool) or halflife_s <= 0:
        raise ValidationError(E_VALIDATION_FIELD, 'halflife_s: конечное число > 0.')
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValidationError(E_VALIDATION_FIELD, f'decay_weight.value: конечное число, получено {value!r}.')
    if not isinstance(age_s, (int, float)) or isinstance(age_s, bool) or not math.isfinite(age_s):
        raise ValidationError(E_VALIDATION_FIELD, f'decay_weight.age_s: конечное число, получено {age_s!r}.')
    return float(value) * 2.0 ** (-max(0.0, float(age_s)) / float(halflife_s))


@dataclass(frozen=True)
class PriorItem:
    """One previously measured endpoint, with the time it was measured."""

    endpoint: str
    checked_at: float
    value: float = 1.0
    exit_ip: str | None = None

    def age(self, now: float) -> float:
        return float(now) - float(self.checked_at)

    def weight(self, now: float, *, halflife_s: float = AGE_HALFLIFE_S) -> float:
        return decay_weight(self.value, self.age(now), halflife_s=halflife_s)


class PriorIndex:
    """Known-good endpoints ordered by their age-corrected weight.

    A prior is *carried* while it is fresh (``age <= max_age_s``) and re-probed
    the moment it is not.  Carry is a decision about the queue, never about
    freshness: a carried result still carries its ``checked_at`` and
    ``age_seconds`` so ``core.admit`` can judge it, and a decayed value is
    reported as decayed.
    """

    def __init__(self, entries: Iterable[PriorItem] = (), *, now: float, max_age_s: float,
                 halflife_s: float = AGE_HALFLIFE_S):
        for name, value in (('now', now), ('max_age_s', max_age_s), ('halflife_s', halflife_s)):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                raise ValidationError(E_VALIDATION_FIELD, f'PriorIndex.{name}: конечное число.')
        if max_age_s < 0 or halflife_s <= 0:
            raise ValidationError(E_VALIDATION_FIELD, 'PriorIndex.max_age_s >= 0 и halflife_s > 0.')
        self.now = float(now)
        self.max_age_s = float(max_age_s)
        self.halflife_s = float(halflife_s)
        self._by_endpoint: dict[str, PriorItem] = {}
        self._future: set[str] = set()
        for entry in entries:
            known = self._by_endpoint.get(entry.endpoint)
            if known is not None and float(entry.checked_at) < float(known.checked_at):
                # A stale copy of a known-good endpoint must not demote it.
                continue
            self._by_endpoint[entry.endpoint] = entry
            if float(entry.checked_at) > self.now:
                self._future.add(entry.endpoint)

    def __len__(self) -> int:
        return len(self._by_endpoint)

    def __contains__(self, endpoint: str) -> bool:
        return endpoint in self._by_endpoint

    def get(self, endpoint: str) -> PriorItem | None:
        return self._by_endpoint.get(endpoint)

    def age(self, endpoint: str) -> float | None:
        entry = self._by_endpoint.get(endpoint)
        return None if entry is None else entry.age(self.now)

    def weight(self, endpoint: str) -> float:
        entry = self._by_endpoint.get(endpoint)
        return 0.0 if entry is None else entry.weight(self.now, halflife_s=self.halflife_s)

    def is_fresh(self, endpoint: str) -> bool:
        entry = self._by_endpoint.get(endpoint)
        return entry is not None and entry.age(self.now) <= self.max_age_s

    @property
    def future_endpoints(self) -> tuple[str, ...]:
        """Priors stamped in the future — a clock anomaly, not a fresh result."""
        return tuple(sorted(self._future))

    def ranked(self) -> tuple[tuple[str, float], ...]:
        """Endpoint and decayed weight, best first, ties broken by name so the
        order is reproducible instead of dependent on dict order."""
        return tuple((endpoint, self.weight(endpoint)) for endpoint in
                     sorted(self._by_endpoint, key=lambda name: (-self.weight(name), name)))

    def ranked_endpoints(self) -> tuple[str, ...]:
        return tuple(endpoint for endpoint, _ in self.ranked())


# --------------------------------------------------------------------------
# items, stages and results
# --------------------------------------------------------------------------


def _ip_of(address: str | None) -> str | None:
    if not address:
        return None
    try:
        return ipaddress.ip_address(address).compressed
    except ValueError:
        return None


@dataclass(frozen=True)
class Item:
    """One canonical endpoint on its way through the chain.

    ``address`` is what the pipeline can count as an IP only when it really is
    one: a hostname endpoint has an address but no IP, and the counters say so
    instead of guessing.
    """

    endpoint: str
    scheme: str
    host: str
    port: int
    address: str
    ip: str | None = None
    source_id: str | None = None
    prior: PriorItem | None = None

    @property
    def key(self) -> str:
        return self.endpoint

    def with_prior(self, prior: PriorItem | None) -> 'Item':
        if prior is None or prior == self.prior:
            return self
        return Item(self.endpoint, self.scheme, self.host, self.port, self.address, self.ip,
                    self.source_id, prior)

    def to_public(self) -> dict:
        return {'endpoint': self.endpoint, 'scheme': self.scheme, 'host': self.host, 'port': self.port,
                'address': self.address, 'ip': self.ip, 'source_id': self.source_id,
                'prior': None if self.prior is None else
                {'checked_at': self.prior.checked_at, 'value': self.prior.value, 'exit_ip': self.prior.exit_ip}}


def item_from_endpoint(endpoint: str, *, source_id: str | None = None,
                       prior: PriorItem | None = None) -> Item:
    """Split a canonical ``scheme://host:port`` into an :class:`Item`.

    The canonical form is the one the project's single normaliser produces
    (``_normalize_proxy`` in ``proxytool.py``): lower-case host, IPv6 in square
    brackets, no credentials, no path.
    """
    if not isinstance(endpoint, str) or '://' not in endpoint:
        raise ValidationError(E_VALIDATION_FIELD,
                              f'Канонический адрес ожидается как scheme://host:port, получено {endpoint!r}.')
    scheme, _, rest = endpoint.partition('://')
    host, _, port = rest.rpartition(':')
    if not scheme or not host or not port.isdigit():
        raise ValidationError(E_VALIDATION_FIELD, f'Адрес без схемы, узла или порта: {endpoint!r}.')
    bare = host[1:-1] if host.startswith('[') and host.endswith(']') else host
    ip = _ip_of(bare)
    return Item(endpoint=endpoint, scheme=scheme, host=bare, port=int(port), address=bare, ip=ip,
                source_id=source_id, prior=prior)


@dataclass(frozen=True)
class StageLimit:
    """The hard cap handed to a runner.

    The budget is enforced by the thing that spends it, not only observed
    afterwards, so ``max_requests`` and ``remaining_bytes`` are what the runner
    must respect and ``remaining_s`` is the time left before the job's own
    deadline.
    """

    stage: str
    target_id: str | None = None
    max_requests: int = 1
    max_bytes: int = 0
    remaining_requests: int | None = None
    remaining_bytes: int | None = None
    remaining_s: float | None = None
    deadline_s: float | None = None

    def to_public(self) -> dict:
        return {'stage': self.stage, 'target_id': self.target_id, 'max_requests': self.max_requests,
                'max_bytes': self.max_bytes, 'remaining_requests': self.remaining_requests,
                'remaining_bytes': self.remaining_bytes, 'remaining_s': self.remaining_s,
                'deadline_s': self.deadline_s}


@dataclass(frozen=True)
class StageOutcome:
    """What one stage measured.

    ``kind`` is the pipeline stage; ``failed_stage`` is the measurement stage
    the failure belongs to, in the same vocabulary as ``probes.STAGES``
    (``tcp``, ``handshake``, ``target``, ``assert``, …), so an F10 funnel can
    count them without a second table.
    """

    kind: str
    ok: bool
    code: str | None = None
    failed_stage: str | None = None
    latency_ms: float | None = None
    bytes: int = 0
    requests: int = 0
    exit_ip: str | None = None
    value: float | None = None
    detail: str | None = None

    def __post_init__(self):
        if self.kind not in PIPELINE_STAGES:
            raise ValidationError(E_VALIDATION_FIELD,
                                  f'StageOutcome.kind={self.kind!r} не является этапом конвейера {STAGES}.')
        for name in ('bytes', 'requests'):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValidationError(E_VALIDATION_FIELD, f'StageOutcome.{name}: целое >= 0.')

    @property
    def latency_s(self) -> float | None:
        return None if self.latency_ms is None else self.latency_ms / 1000.0

    def to_public(self) -> dict:
        return {'kind': self.kind, 'ok': self.ok, 'code': self.code, 'failed_stage': self.failed_stage,
                'latency_ms': self.latency_ms, 'bytes': self.bytes, 'requests': self.requests,
                'exit_ip': self.exit_ip, 'value': self.value, 'detail': self.detail}


@dataclass(frozen=True)
class ItemResult:
    """One endpoint's terminal outcome, delivered the moment it is decided."""

    endpoint: str
    state: str
    ok: bool
    stages: tuple[StageOutcome, ...] = ()
    reason_code: str | None = None
    exit_ip: str | None = None
    address: str | None = None
    carried: bool = False
    age_seconds: float | None = None
    checked_at: float | None = None
    value: float | None = None
    started_at: float = 0.0
    finished_at: float = 0.0
    requests: int = 0
    bytes: int = 0

    def __post_init__(self):
        if self.state not in ITEM_STATES:
            raise ValidationError(E_VALIDATION_FIELD, f'ItemResult.state={self.state!r} не в {ITEM_STATES}.')

    @property
    def duration_s(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    @property
    def last_latency_s(self) -> float | None:
        for outcome in reversed(self.stages):
            if outcome.latency_s is not None:
                return outcome.latency_s
        return None

    def to_public(self) -> dict:
        return {'endpoint': self.endpoint, 'state': self.state, 'ok': self.ok,
                'reason_code': self.reason_code, 'exit_ip': self.exit_ip, 'address': self.address,
                'carried': self.carried, 'age_seconds': self.age_seconds, 'checked_at': self.checked_at,
                'value': self.value, 'duration_s': round(self.duration_s, 4), 'requests': self.requests,
                'bytes': self.bytes, 'stages': [item.to_public() for item in self.stages]}


@dataclass
class Counters:
    """Every number the run reports, including the three different N.

    ``unique_endpoints`` counts canonical addresses, ``unique_ips`` counts
    distinct IP literals among them, and ``confirmed_exit_ips`` counts exit
    addresses a target actually reported.  The last two are strictly smaller
    than the first, and the pipeline never reports one number for all three.
    """

    # front half
    source_bytes: int = 0
    sources_read: int = 0
    sources_truncated: int = 0
    parsed: int = 0
    normalized: int = 0
    rejected: int = 0
    duplicates: int = 0
    resume_skips: int = 0
    # measurements
    cheap_checked: int = 0
    cheap_passed: int = 0
    basic_checked: int = 0
    basic_passed: int = 0
    expensive_checked: int = 0
    expensive_passed: int = 0
    carried: int = 0
    # the three N
    unique_endpoints: int = 0
    unique_ips: int = 0
    unique_addresses: int = 0
    confirmed_exit_ips: int = 0
    passed_endpoints: int = 0
    passed_unique_ips: int = 0
    passed_exit_ips: int = 0
    # resources
    requests: int = 0
    bytes: int = 0
    cpu_s: float = 0.0
    stage_failures: dict = field(default_factory=dict)

    def to_public(self) -> dict:
        data = {name: getattr(self, name) for name in
                ('source_bytes', 'sources_read', 'sources_truncated', 'parsed', 'normalized', 'rejected',
                 'duplicates', 'resume_skips', 'cheap_checked', 'cheap_passed', 'basic_checked',
                 'basic_passed', 'expensive_checked', 'expensive_passed', 'carried', 'unique_endpoints',
                 'unique_ips', 'unique_addresses', 'confirmed_exit_ips', 'passed_endpoints',
                 'passed_unique_ips', 'passed_exit_ips', 'requests', 'bytes')}
        data['cpu_s'] = round(self.cpu_s, 3)
        data['stage_failures'] = dict(sorted(self.stage_failures.items()))
        return data


@dataclass(frozen=True)
class FindPolicy:
    """When to stop.  ``what`` decides which of the three N is counted."""

    n: int = 0
    what: str = 'exit'

    def __post_init__(self):
        if not isinstance(self.n, int) or isinstance(self.n, bool) or self.n < 0:
            raise ValidationError(E_VALIDATION_FIELD, f'FindPolicy.n: целое >= 0, получено {self.n!r}.')
        if self.what not in FIND_BY:
            raise ValidationError(E_VALIDATION_FIELD,
                                  f'FindPolicy.what={self.what!r}; допустимо {FIND_BY} — N endpoint, '
                                  'N уникальных IP и N подтверждённых exit-IP это разные числа.')

    @property
    def enabled(self) -> bool:
        return self.n > 0

    def to_public(self) -> dict:
        return {'n': self.n, 'what': self.what, 'counted': COUNT_FIELD[self.what] if self.enabled else None}


@dataclass(frozen=True)
class FindProgress:
    """How the N is being satisfied, with all three counts side by side."""

    target: int
    what: str
    met: int
    unique_endpoints: int
    unique_ips: int
    confirmed_exit_ips: int
    satisfied: bool
    complete: bool = True

    @property
    def remaining(self) -> int:
        return max(0, self.target - self.met)

    def to_public(self) -> dict:
        return {'target': self.target, 'what': self.what, 'met': self.met, 'remaining': self.remaining,
                'unique_endpoints': self.unique_endpoints, 'unique_ips': self.unique_ips,
                'confirmed_exit_ips': self.confirmed_exit_ips, 'satisfied': self.satisfied,
                'complete': self.complete}


# --------------------------------------------------------------------------
# sources, parsers, normalisation
# --------------------------------------------------------------------------


def parse_lines(data: bytes) -> Iterator[str]:
    """Line parser for the built-in text list format.

    One address per line, like the existing collector (``collect`` in
    ``proxytool.py`` feeds a whole line to the normaliser), with blank lines and
    ``#`` comments skipped and everything from an inline ``#`` dropped.  The
    pipeline deliberately does not invent a second address grammar: a line that
    does not normalise is rejected and counted, not guessed at.
    """
    if not data:
        return
    for raw in bytes(data).decode('utf-8', 'replace').replace('\r\n', '\n').split('\n'):
        line = raw.split('#', 1)[0].strip()
        if line:
            yield line


def parse_delimited(data: bytes, *, sep: str = ',') -> Iterator[str]:
    """Delimited parser for CSV-ish lists; the first column is the address."""
    if not data:
        return
    for raw in bytes(data).decode('utf-8', 'replace').replace('\r\n', '\n').split('\n'):
        line = raw.strip()
        if not line or line[0] in '#;':
            continue
        first = line.split(sep, 1)[0].strip()
        if first:
            yield first


def normalize_default(value: str) -> str | None:
    """The project's single normaliser, imported lazily.

    The pipeline deliberately has no second proxy normaliser (HANDOFF §2.2): it
    calls ``proxytool.normalize`` for public addresses.  A caller that needs a
    different scope passes its own callable as ``config.normalize`` — for
    instance ``proxytool.normalize_custom`` for a user's own hostnames.
    """
    from . import proxytool  # late import: proxytool pulls in httpx and the package root
    return proxytool.normalize(value)


@dataclass(frozen=True)
class SourceSpec:
    """A source as the pipeline consumes it: a *factory* of a byte stream.

    A factory (not a live stream) is what makes a resumed run correct: a pause
    stops the chain, and resuming calls the factory again instead of trying to
    rewind a half-read connection.  Endpoints already admitted are recognised by
    the ledger and reported as ``resume_skips``, never measured twice.
    """

    source_id: str
    fetch: Callable[[], Any]
    max_bytes: int | None = None

    def __post_init__(self):
        if not isinstance(self.source_id, str) or not self.source_id:
            raise ValidationError(E_VALIDATION_FIELD, 'SourceSpec.source_id: непустая строка.')
        if not callable(self.fetch):
            raise ValidationError(E_VALIDATION_FIELD, 'SourceSpec.fetch: нужен вызываемый объект.')
        if self.max_bytes is not None and (not isinstance(self.max_bytes, int)
                                           or isinstance(self.max_bytes, bool) or self.max_bytes < 1):
            raise ValidationError(E_VALIDATION_FIELD, 'SourceSpec.max_bytes: целое >= 1 или None.')

    async def open(self) -> AsyncIterator[bytes]:
        """Call the factory and return the byte stream.

        ``fetch`` is a zero-argument callable that returns either an async
        iterator of chunks (an ``async def`` generator) or an awaitable that
        resolves to one, which is what an ``httpx`` streaming response gives.
        """
        stream = self.fetch()
        if inspect.isawaitable(stream):
            stream = await stream
        return stream


# --------------------------------------------------------------------------
# control
# --------------------------------------------------------------------------


class PipelineControl:
    """Pause, resume and cancel for the whole chain.

    ``cancel`` is cooperative and produces a ``RunResult``; cancelling the
    asyncio task instead still propagates, and the chain is torn down either
    way.  Pause is a clean stop: in-flight measurements finish, nothing new is
    fed, and the run returns the endpoints it has not touched.
    """

    def __init__(self):
        self._paused = asyncio.Event()
        self._cancelled = asyncio.Event()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def cancel(self) -> None:
        self._cancelled.set()

    def reset(self) -> None:
        self._paused.clear()
        self._cancelled.clear()

    def to_public(self) -> dict:
        return {'paused': self.paused, 'cancelled': self.cancelled}


# --------------------------------------------------------------------------
# metrics and run result
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunMetrics:
    """Timing and resource facts of one run.

    ``items_per_s`` describes *this run against whatever its runners talk to*.
    It is not a claim about the speed of live public proxies; only the
    :func:`benchmark` helper produces a number with its fixture attached.
    """

    wall_s: float
    time_to_first_s: float | None = None
    time_to_first_pass_s: float | None = None
    time_to_n_s: float | None = None
    measured: int = 0
    workers: int = 0
    peak_inflight: int = 0
    peak_fds: int = 0
    peak_ram_reserved_bytes: int = 0
    queue_high_water: int = 0
    blocked_waits: int = 0
    cpu_s: float = 0.0
    host_waits: int = 0
    concurrency: dict = field(default_factory=dict)

    @property
    def items_per_s(self) -> float:
        return self.measured / self.wall_s if self.wall_s > 0 else 0.0

    def to_public(self) -> dict:
        return {
            'wall_s': round(self.wall_s, 4),
            'time_to_first_s': None if self.time_to_first_s is None else round(self.time_to_first_s, 4),
            'time_to_first_pass_s': (None if self.time_to_first_pass_s is None
                                     else round(self.time_to_first_pass_s, 4)),
            'time_to_n_s': None if self.time_to_n_s is None else round(self.time_to_n_s, 4),
            'measured': self.measured, 'items_per_s': round(self.items_per_s, 3), 'workers': self.workers,
            'peak_inflight': self.peak_inflight, 'peak_fds': self.peak_fds,
            'peak_ram_reserved_bytes': self.peak_ram_reserved_bytes,
            'queue_high_water': self.queue_high_water, 'blocked_waits': self.blocked_waits,
            'cpu_s': round(self.cpu_s, 3), 'host_waits': self.host_waits,
            'concurrency': dict(self.concurrency),
        }


@dataclass(frozen=True)
class RunResult:
    """Everything one call to :meth:`Pipeline.run` decided.

    ``remaining`` is the endpoints this run *committed to but did not measure* —
    admitted to the ledger and never finished.  It is not a work list: a run that
    stopped early never read the rest of the source, so those endpoints are not in
    it.  ``feed_complete`` says which case this is, and a resume covers both: the
    source is read again, the endpoints in ``remaining`` and the ones never read
    are measured, and the ones already measured are reported as ``resume_skips``.
    """

    state: str
    reason: str
    counters: Counters
    find: FindProgress
    metrics: RunMetrics
    results: tuple[ItemResult, ...] = ()
    passed: tuple[ItemResult, ...] = ()
    remaining: tuple[str, ...] = ()
    feed_complete: bool = False
    resources: ResourceSnapshot | None = None

    def __post_init__(self):
        if self.state not in RUN_STATES:
            raise ValidationError(E_VALIDATION_FIELD, f'RunResult.state={self.state!r} не в {RUN_STATES}.')

    @property
    def stopped_early(self) -> bool:
        return self.state in ('want_reached', 'cancelled', 'paused', 'partial')

    def to_public(self) -> dict:
        return {'state': self.state, 'reason': self.reason, 'feed_complete': self.feed_complete,
                'counters': self.counters.to_public(), 'find': self.find.to_public(),
                'metrics': self.metrics.to_public(),
                'resources': None if self.resources is None else self.resources.to_public(),
                'passed': [item.to_public() for item in self.passed],
                'remaining': list(self.remaining)}


# --------------------------------------------------------------------------
# ledger
# --------------------------------------------------------------------------


class Ledger:
    """One canonical endpoint enters the chain once.

    An endpoint is admitted when it is *queued*, not when it is parsed, so an
    item that was not handed to the queue (a paused run, a stopped run) is
    still pending and is measured on the next run.  Because the ledger lives on
    the :class:`Pipeline`, no endpoint is measured twice across pause/resume.
    """

    def __init__(self):
        self.admitted: set[str] = set()
        self.finished: set[str] = set()
        self.released: set[str] = set()
        self.reason_codes: dict[str, str] = {}

    def admit(self, endpoint: str) -> bool:
        if endpoint in self.admitted:
            return False
        self.admitted.add(endpoint)
        self.released.discard(endpoint)
        return True

    def release(self, endpoint: str) -> None:
        """Give an endpoint back: it was dequeued and then skipped because the
        run was stopping, so it was never measured and a resumed run must pick it
        up again."""
        if endpoint in self.admitted:
            self.admitted.discard(endpoint)
            self.released.add(endpoint)

    def finish(self, endpoint: str, reason_code: str | None = None) -> None:
        self.finished.add(endpoint)
        if reason_code:
            self.reason_codes[endpoint] = reason_code

    @property
    def pending(self) -> set[str]:
        """What a resumed run still has to measure."""
        return (self.admitted - self.finished) | (self.released - self.finished)

    def to_public(self) -> dict:
        return {'admitted': len(self.admitted), 'finished': len(self.finished),
                'released': len(self.released), 'pending': len(self.pending)}


# --------------------------------------------------------------------------
# runners
# --------------------------------------------------------------------------

#: A runner is ``async def runner(item, *, stage, limit) -> ...`` and may return
#: a :class:`StageOutcome`, a mapping with the same field names, or a plain
#: boolean (``True`` = the stage passed).  Anything else fails that item rather
#: than being silently treated as a pass.
Runner = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class Runners:
    """The measurement half, injected.

    The pipeline owns budgets, limits, ordering and accounting; a runner owns
    one socket conversation and nothing else.  Integration wires
    ``probes.run_plan``-shaped coroutines here (F01 modes are their business)
    and reports what it actually spent in ``StageOutcome.bytes`` / ``.requests``.
    """

    cheap: Runner | None = None
    basic: Runner | None = None
    expensive: Runner | None = None

    def for_stage(self, stage: str) -> Runner | None:
        return getattr(self, stage, None)

    def enabled(self) -> tuple[str, ...]:
        return tuple(stage for stage in MEASUREMENT_PIPELINE_STAGES if self.for_stage(stage) is not None)


def _coerce_outcome(value: Any, stage: str) -> StageOutcome:
    if isinstance(value, StageOutcome):
        if value.kind == stage:
            return value
        return StageOutcome(stage, value.ok, value.code, value.failed_stage, value.latency_ms,
                            value.bytes, value.requests, value.exit_ip, value.value, value.detail)
    if isinstance(value, Mapping):
        fields = {name: value[name] for name in
                  ('code', 'failed_stage', 'latency_ms', 'bytes', 'requests', 'exit_ip', 'value', 'detail')
                  if name in value}
        fields['ok'] = bool(value.get('ok', False))
        return StageOutcome(stage, **fields)
    if isinstance(value, bool):
        return StageOutcome(stage, value)
    raise ValidationError(E_VALIDATION_FIELD,
                          f'runner этапа {stage!r} вернул {type(value).__name__}; допустимы StageOutcome, '
                          'словарь с теми же полями или bool.')


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineConfig:
    """The whole configuration of a run, in one immutable value."""

    sources: tuple[SourceSpec, ...] = ()
    budgets: Budgets = Budgets()
    limits: Limits = Limits()
    find: FindPolicy = FindPolicy()
    priors: PriorIndex | None = None
    runners: Runners = Runners()
    normalize: Callable[[str], str | None] = normalize_default
    parse: Callable[[bytes], Iterable[str]] = parse_lines
    #: ``True`` feeds a line-oriented parser chunk by chunk; ``False`` buffers a
    #: whole (already byte-bounded) document, which is what a JSON or HTML
    #: parser needs.
    parse_streaming: bool = True
    run_cheap: bool = True
    run_basic: bool = True
    run_expensive: bool = True
    expensive_policy: str = EXPENSIVE_UNTIL_N
    carry_fresh_prior: bool = True
    on_result: Callable[[ItemResult], Any] | None = None

    def __post_init__(self):
        if self.expensive_policy not in EXPENSIVE_POLICIES:
            raise ValidationError(E_VALIDATION_FIELD,
                                  f'expensive_policy={self.expensive_policy!r} не в {EXPENSIVE_POLICIES}.')
        if not callable(self.normalize):
            raise ValidationError(E_VALIDATION_FIELD, 'config.normalize: нужен вызываемый объект.')
        if not callable(self.parse):
            raise ValidationError(E_VALIDATION_FIELD, 'config.parse: нужен вызываемый объект.')
        if not isinstance(self.parse_streaming, bool):
            raise ValidationError(E_VALIDATION_FIELD, 'config.parse_streaming: bool.')
        if self.run_cheap and self.runners.cheap is None:
            raise ValidationError(E_VALIDATION_FIELD,
                                  'run_cheap=True, но runners.cheap не задан: дешёвая проба не может быть '
                                  'пропущена молча.')
        if self.run_basic and self.runners.basic is None:
            raise ValidationError(E_VALIDATION_FIELD, 'run_basic=True, но runners.basic не задан.')
        if (self.run_expensive and self.expensive_policy != EXPENSIVE_NONE
                and self.runners.expensive is None):
            raise ValidationError(E_VALIDATION_FIELD, 'run_expensive=True, но runners.expensive не задан.')
        if (self.run_expensive and self.expensive_policy != EXPENSIVE_NONE
                and self.runners.expensive is not None and not self.basic_required):
            # The chain is cheap -> basic -> expensive: a verdict the basic probe
            # has not produced is the only thing the expensive probe may refine.
            # Configuring it without the basic stage would drop it silently.
            raise ValidationError(E_VALIDATION_FIELD,
                                  'Дорогая проба идёт после базовой: задайте runners.basic, '
                                  'иначе этап не будет выполнен вовсе.')
        if self.priors is not None and not isinstance(self.priors, PriorIndex):
            raise ValidationError(E_VALIDATION_FIELD, 'config.priors: PriorIndex или None.')
        if self.on_result is not None and not callable(self.on_result):
            raise ValidationError(E_VALIDATION_FIELD, 'config.on_result: нужен вызываемый объект.')

    @property
    def cheap_required(self) -> bool:
        return self.run_cheap and self.runners.cheap is not None

    @property
    def basic_required(self) -> bool:
        return self.run_basic and self.runners.basic is not None

    @property
    def expensive_required(self) -> bool:
        return (self.run_expensive and self.expensive_policy != EXPENSIVE_NONE
                and self.runners.expensive is not None)

    def to_public(self) -> dict:
        return {'sources': [item.source_id for item in self.sources], 'budgets': self.budgets.to_public(),
                'limits': self.limits.to_public(), 'find': self.find.to_public(),
                'run_cheap': self.cheap_required, 'run_basic': self.basic_required,
                'run_expensive': self.expensive_required, 'expensive_policy': self.expensive_policy,
                'carry_fresh_prior': self.carry_fresh_prior, 'parse_streaming': self.parse_streaming,
                'stages': list(self.runners.enabled())}


# --------------------------------------------------------------------------
# the pipeline
# --------------------------------------------------------------------------

#: A later reason never overwrites an earlier one of higher priority: a
#: cancelled run is not relabelled ``want_reached`` by a late success, and a
#: satisfied N is not hidden by a budget that ran out in the same breath.
_STOP_PRIORITY = {'items_exhausted': 0, 'stopped': 0, 'deadline_exceeded': 1,
                  'budget_exhausted': 1, 'paused': 2, 'want_reached': 3, 'cancelled': 4}
_STATE_OF_REASON = {'want_reached': 'want_reached', 'paused': 'paused', 'cancelled': 'cancelled',
                    'budget_exhausted': 'budget', 'deadline_exceeded': 'budget', 'stopped': 'partial',
                    'items_exhausted': 'complete'}


class Pipeline:
    """The chain, over an arbitrary set of injected sources and runners.

    One :class:`Pipeline` may be run more than once: a run after a pause
    continues the same ledger, so no endpoint is measured twice, and a cancel
    ends it for good.
    """

    def __init__(self, config: PipelineConfig, *, clock: PipelineClock | None = None,
                 control: PipelineControl | None = None,
                 concurrency: AdaptiveConcurrency | None = None):
        self.config = config
        self.clock = clock or SystemClock()
        self.control = control or PipelineControl()
        self.ledger = Ledger()
        self.concurrency = concurrency or AdaptiveConcurrency(
            minimum=1, maximum=max(1, config.budgets.worker_ceiling()),
            target_success=0.8, window=16, increase=2, decrease_factor=0.5)
        self.last_result: RunResult | None = None
        self._stopped = asyncio.Event()
        self._reason: str | None = None
        self._state: str | None = None
        self._queue = asyncio.Queue(maxsize=config.budgets.max_queue_items)
        self._closing = False
        self._drained = asyncio.Event()
        self._gate: ResourceGate | None = None
        self._hosts = HostLimiter(max_per_host=config.limits.max_per_host,
                                  min_interval_s=config.limits.min_host_interval_s, clock=self.clock)
        self._target_policies = {item.target_id: item for item in config.limits.targets}
        self._target_inflight: dict[str, int] = {}
        self._target_used: dict[str, int] = {}
        self._target_cond = asyncio.Condition()
        self._counters = Counters()
        self._results: list[ItemResult] = []
        self._passed: list[ItemResult] = []
        self._addresses: set[str] = set()
        self._ips: set[str] = set()
        self._passed_addresses: set[str] = set()
        self._passed_ips: set[str] = set()
        self._passed_exit_ips: set[str] = set()
        self._find_satisfied = False
        self._queue_high_water = 0
        self._started = 0.0
        self._first_at: float | None = None
        self._first_pass_at: float | None = None
        self._time_to_n: float | None = None
        self._measured = 0
        self._feed_complete = False
        self._admitted_at_start: set[str] = set()
        self._deadline: float | None = None

    # -- public API ---------------------------------------------------------

    async def run(self) -> RunResult:
        """Run to the end and return the summary.  Equivalent to draining
        :meth:`stream` into a list."""
        async for _ in self.stream():
            pass
        result = self.last_result
        if result is None:  # pragma: no cover - stream always assigns it
            raise PipelineError(E_VALIDATION_SCHEMA, 'stream() не вернул результат.')
        return result

    def stream(self) -> AsyncIterator[ItemResult]:
        """Yield every item result as it is decided (F12 "по мере поступления").

        The consumer's own queue is bounded, so a slow consumer applies
        backpressure instead of growing memory; a consumer that stops reading
        cannot wedge the run, because the put is abandoned once the run stops.
        The :class:`RunResult` lands in :attr:`last_result`.
        """
        return self._stream()

    async def _stream(self) -> AsyncIterator[ItemResult]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.config.budgets.max_results_pending)
        holder: dict[str, RunResult] = {}

        async def emit(result: ItemResult) -> None:
            while True:
                if self._stopped.is_set():
                    return
                try:
                    await asyncio.wait_for(queue.put(result), 0.05)
                    return
                except TimeoutError:
                    continue

        async def drive() -> None:
            holder['result'] = await self._execute(emit)

        task = loop.create_task(drive())
        try:
            while True:
                getter = loop.create_task(queue.get())
                done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    yield getter.result()
                    continue
                getter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await getter
                while not queue.empty():
                    yield queue.get_nowait()
                if not task.done():
                    await task
                result = holder.get('result')
                if result is None:  # pragma: no cover - drive always assigns it
                    raise PipelineError(E_VALIDATION_SCHEMA, 'Выполнение не вернуло результат.')
                return
        finally:
            if not task.done():
                self._stop('stopped', 'consumer_gone')
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            while not queue.empty():
                queue.get_nowait()

    # -- stop handling ------------------------------------------------------

    def _stop(self, reason: str, detail: str | None = None) -> None:
        """Record why the chain stops.  The first reason with the highest
        priority wins, so a cancelled run is not relabelled ``want_reached``
        by a late success and a satisfied N is not hidden by a budget."""
        if self._reason is not None and _STOP_PRIORITY.get(reason, 0) <= _STOP_PRIORITY.get(self._reason, 0):
            return
        self._reason = reason
        self._state = _STATE_OF_REASON.get(reason, 'complete')
        if detail and reason not in ('items_exhausted', 'complete'):
            self._counters.stage_failures.setdefault('stop', detail)
        self._stopped.set()

    def _check_control(self) -> bool:
        """Fold a pause or a cancel into the stop reason.  True = keep going."""
        if self.control.cancelled:
            self._stop('cancelled', 'cancelled')
            return False
        if self.control.paused:
            self._stop('paused', 'paused')
            return False
        return not self._stopped.is_set()

    # -- the run ------------------------------------------------------------

    async def _execute(self, emit) -> RunResult:
        config = self.config
        self._stopped.clear()
        self._reason = None
        self._state = None
        self._admitted_at_start = set(self.ledger.admitted)
        self._started = self.clock.monotonic()
        self._first_at = self._first_pass_at = self._time_to_n = None
        self._measured = 0
        self._queue_high_water = 0
        self._queue = asyncio.Queue(maxsize=config.budgets.max_queue_items)
        # A resumed run continues the same N: the unit counts are projections of
        # the cumulative sets, so an N that was already satisfied stays satisfied
        # instead of being counted a second time.
        if config.find.enabled and self._find_met_count() >= config.find.n:
            self._find_satisfied = True
            self._time_to_n = 0.0
        else:
            self._find_satisfied = False
        self._feed_complete = False
        self._closing = False
        self._drained.clear()
        self._target_inflight.clear()
        self._target_used.clear()
        self._deadline = None if config.budgets.deadline_s is None else self._started + config.budgets.deadline_s
        self._gate = ResourceGate(config.budgets, self.clock, limit_provider=lambda: self.concurrency.limit,
                                  deadline=self._deadline)
        await self._seed_priors()

        workers = config.budgets.worker_ceiling()
        try:
            hard = self.clock.remaining_s(self._deadline) if self._deadline is not None else None
            if hard is None:
                await self._supervise(workers, emit)
            else:
                async with asyncio.timeout(max(0.001, hard)):
                    await self._supervise(workers, emit)
        except TimeoutError:
            self._stop('deadline_exceeded', DEADLINE_EXCEEDED)
        except BudgetExhausted as exc:
            self._stop('budget_exhausted', exc.code)
        except asyncio.CancelledError:
            self._stop('cancelled', 'task_cancelled')
            self.last_result = self._snapshot()
            raise
        except BaseExceptionGroup as group:
            self._stop('stopped', f'group:{_group_reason(group)}')
        except Exception as exc:  # a broken source or runner must not take the chain down
            self._stop('stopped', type(exc).__name__)
        if self._reason is None:
            # "Every item was measured" and "the chain ended with items left"
            # are different outcomes, and the second one is not a success.
            if self._feed_complete and not self.ledger.pending:
                self._stop('items_exhausted', 'items_exhausted')
            else:
                self._stop('stopped', 'items_left')
        result = self._snapshot()
        self.last_result = result
        return result

    async def _supervise(self, workers: int, emit) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self._feeder(emit))
            for _ in range(workers):
                group.create_task(self._worker(emit))

    # -- front half: fetch, parse, normalize, dedup -------------------------

    async def _seed_priors(self) -> None:
        """Priors enter through the same queue and the same ledger as everything
        else, so a carried prior is indistinguishable from any other item."""
        priors = self.config.priors
        if priors is None:
            return
        for endpoint in priors.ranked_endpoints():
            if not self._check_control():
                return
            await self._enqueue(item_from_endpoint(endpoint, prior=priors.get(endpoint)))

    async def _feeder(self, emit) -> None:
        try:
            for source in self.config.sources:
                if self._stopped.is_set() or not self._check_control():
                    return
                if not await self._feed_source(source, emit):
                    return
            # Only a source that was read to its very end makes the feed complete.
            self._feed_complete = True
        except BudgetExhausted as exc:
            self._stop('budget_exhausted', exc.code)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # One broken source stops the chain, and says so; it never looks
            # like "the corpus was exhausted".
            self._stop('stopped', f'source:{type(exc).__name__}')
        finally:
            self._drain_workers()

    def _drain_workers(self) -> None:
        """Let every worker leave, from the feeder's ``finally``.

        Nothing here awaits.  A blocking queue put would need a worker to take an
        item, and while the run is being torn down the workers may already be
        gone, so the feeder would wait for a wakeup that can never come.  Closing
        the feed and setting the event is enough: a worker that is idle is
        waiting on exactly that event, and a worker that still has an item in
        hand measures it first and then leaves.
        """
        self._closing = True
        self._drained.set()

    async def _next_item(self) -> Item | None:
        """The next queued item, or ``None`` once the feed is closed and dry.

        A worker that finds the queue empty waits on the item *and* on the close
        at the same time, so it cannot be left behind on a queue that will never
        be filled again.  The two tasks are only created when the worker has
        nothing to do anyway.
        """
        if not self._queue.empty():
            return await self._queue.get()
        if self._closing:
            return None
        getter = asyncio.ensure_future(self._queue.get())
        closer = asyncio.ensure_future(self._drained.wait())
        try:
            await asyncio.wait({getter, closer}, return_when=asyncio.FIRST_COMPLETED)
            return getter.result() if getter.done() else None
        finally:
            for task in (getter, closer):
                if not task.done():
                    task.cancel()

    async def _feed_source(self, source: SourceSpec, emit) -> bool:
        """Read one source to its end.  False means it was not: a stop, a spent
        byte budget or a broken stream, and the feed is not complete."""
        budgets = self.config.budgets
        cap = budgets.max_source_bytes
        if source.max_bytes is not None:
            cap = source.max_bytes if cap is None else min(cap, source.max_bytes)
        used = 0
        self._counters.sources_read += 1
        stream = await source.open()
        if self.config.parse_streaming:
            buffer = bytearray()
            async for chunk in stream:
                if self._stopped.is_set() or not self._check_control():
                    return False
                if cap is not None and used >= cap:
                    self._truncate()
                    return False
                if cap is not None and used + len(chunk) > cap:
                    chunk = chunk[:max(0, cap - used)]
                    self._truncate()
                used += len(chunk)
                self._counters.source_bytes += len(chunk)
                # A line may straddle two chunks, so the buffer offset of the
                # current chunk's first byte is tracked explicitly: the carry
                # from the previous chunk must not shift the newline positions.
                offset = len(buffer)
                buffer += chunk
                start = 0
                while True:
                    index = chunk.find(b'\n', start)
                    if index < 0:
                        break
                    end = offset + (index - start) + 1
                    await self._feed_line(bytes(buffer[:end - 1]), source, emit)
                    del buffer[:end]
                    offset = 0
                    start = index + 1
                    if cap is not None and used >= cap:
                        self._truncate()
                        return False
                if len(buffer) > budgets.max_line_bytes:
                    await self._feed_line(bytes(buffer), source, emit)
                    buffer.clear()
            if buffer:
                await self._feed_line(bytes(buffer), source, emit)
        else:
            document = bytearray()
            async for chunk in stream:
                if self._stopped.is_set() or not self._check_control():
                    return False
                if cap is not None and used >= cap:
                    self._truncate()
                    return False
                if cap is not None and used + len(chunk) > cap:
                    chunk = chunk[:max(0, cap - used)]
                    self._truncate()
                used += len(chunk)
                self._counters.source_bytes += len(chunk)
                document += chunk
            await self._feed_tokens(self._parse(bytes(document)), source, emit)
        return True

    def _truncate(self) -> None:
        """A source hit its byte budget.  That is a budget stop, not an
        interruption, and it carries the BODY code of CONTRACTS §5.4."""
        self._counters.sources_truncated += 1
        self._stop('budget_exhausted', E_LIMIT_BODY)

    def _parse(self, data: bytes) -> Iterable[str]:
        try:
            return self.config.parse(data)
        except Exception as exc:
            # A parser that cannot read one line is recorded and skipped, not
            # turned into a silent empty result.
            self._counters.stage_failures.setdefault(STAGE_PARSE, type(exc).__name__)
            return ()

    async def _feed_line(self, line: bytes, source: SourceSpec, emit) -> None:
        await self._feed_tokens(self._parse(line), source, emit)

    async def _feed_tokens(self, tokens: Iterable[str], source: SourceSpec, emit) -> None:
        try:
            for token in tokens:
                if self._stopped.is_set():
                    return
                self._counters.parsed += 1
                canonical = self._normalize(token)
                if canonical is None:
                    self._counters.rejected += 1
                    continue
                self._counters.normalized += 1
                if canonical in self.ledger.admitted:
                    # Seen twice: within this run (duplicate) or already admitted
                    # before a pause (resume skip).  Neither is measured again.
                    if canonical in self._admitted_at_start:
                        self._counters.resume_skips += 1
                    else:
                        self._counters.duplicates += 1
                    continue
                await self._enqueue(self._build_item(canonical, source.source_id))
        except asyncio.CancelledError:
            raise
        except BudgetExhausted:
            raise
        except Exception as exc:
            self._counters.stage_failures.setdefault('queue', type(exc).__name__)

    def _normalize(self, token: str) -> str | None:
        try:
            value = self.config.normalize(token)
        except Exception as exc:
            self._counters.stage_failures.setdefault(STAGE_NORMALIZE, type(exc).__name__)
            return None
        return value if isinstance(value, str) and value else None

    def _build_item(self, canonical: str, source_id: str | None) -> Item:
        item = item_from_endpoint(canonical, source_id=source_id)
        priors = self.config.priors
        if priors is not None and canonical in priors:
            item = item.with_prior(priors.get(canonical))
        return item

    async def _enqueue(self, item: Item) -> bool:
        """The one bounded hand-off.

        The endpoint is admitted to the ledger only when the queue accepted it,
        so an item nobody got to is still pending and is measured on the next
        run.  While the queue is full the feeder stops pulling bytes from the
        source: that is where backpressure comes from.
        """
        while True:
            if self._stopped.is_set() or not self._check_control():
                return False
            max_items = self.config.budgets.max_items
            if max_items is not None and len(self.ledger.admitted) >= max_items:
                raise BudgetExhausted(E_LIMIT_BUDGET,
                                      f'Лимит адресов исчерпан: {len(self.ledger.admitted)} из {max_items}.')
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
                # The queue is full: yield to the workers and try again.  Nothing
                # is pulled from the source while this loop runs, which is the
                # backpressure the chain needs.
                self._queue_high_water = self._gate.note_queue(self._queue.qsize(), self._queue_high_water)
                await asyncio.sleep(0)
                continue
            self.ledger.admit(item.endpoint)
            self._count_addresses(item)
            self._queue_high_water = self._gate.note_queue(self._queue.qsize(), self._queue_high_water)
            return True

    def _count_addresses(self, item: Item) -> None:
        self._addresses.add(item.address)
        if item.ip is not None:
            self._ips.add(item.ip)
        self._counters.unique_addresses = len(self._addresses)
        self._counters.unique_ips = len(self._ips)
        self._counters.unique_endpoints = len(self.ledger.admitted)

    # -- back half: cheap, basic, necessary expensive -----------------------

    async def _worker(self, emit) -> None:
        while True:
            item = await self._next_item()
            if item is None:
                return
            try:
                if self._stopped.is_set() or not self._check_control():
                    self.ledger.release(item.endpoint)
                    continue
                await self._hosts.acquire(item.address)
                try:
                    await self._check(item, emit)
                finally:
                    await self._hosts.release(item.address)
            except asyncio.CancelledError:
                raise
            except BudgetExhausted:
                raise
            except Exception as exc:  # one item must never take a worker down
                self._counters.stage_failures.setdefault('worker', type(exc).__name__)
                self.ledger.finish(item.endpoint, type(exc).__name__)
            finally:
                self._queue.task_done()

    async def _check(self, item: Item, emit) -> None:
        if self._carries(item):
            await self._record(self._carried(item), emit)
            return
        started_wall = self.clock.time()
        started = self.clock.monotonic()
        stages: list[StageOutcome] = []
        if self.config.cheap_required:
            outcome = await self._stage(item, STAGE_CHEAP)
            stages.append(outcome)
            self._counters.cheap_checked += 1
            self._counters.cheap_passed += int(outcome.ok)
            if not outcome.ok:
                self.ledger.finish(item.endpoint, outcome.code)
                await self._record(self._result(item, stages, started_wall, started), emit)
                return
        if not self.config.basic_required:
            self.ledger.finish(item.endpoint)
            await self._record(self._result(item, stages, started_wall, started), emit)
            return
        outcome = await self._stage(item, STAGE_BASIC)
        stages.append(outcome)
        self._counters.basic_checked += 1
        self._counters.basic_passed += int(outcome.ok)
        if not outcome.ok:
            self.ledger.finish(item.endpoint, outcome.code)
            await self._record(self._result(item, stages, started_wall, started), emit)
            return
        if self.config.expensive_required and self._wants_expensive(item, stages):
            outcome = await self._stage(item, STAGE_EXPENSIVE)
            stages.append(outcome)
            self._counters.expensive_checked += 1
            self._counters.expensive_passed += int(outcome.ok)
        self.ledger.finish(item.endpoint)
        await self._record(self._result(item, stages, started_wall, started), emit)

    def _carries(self, item: Item) -> bool:
        """A prior is carried instead of re-probed only while it is fresh."""
        priors = self.config.priors
        return (self.config.carry_fresh_prior and priors is not None and item.prior is not None
                and priors.is_fresh(item.endpoint))

    def _carried(self, item: Item) -> ItemResult:
        prior = item.prior
        now = self.clock.time()
        outcome = StageOutcome(STAGE_BASIC, True, detail='carried from a fresh prior')
        return ItemResult(endpoint=item.endpoint, state='done', ok=True, stages=(outcome,),
                          exit_ip=prior.exit_ip, address=item.address, carried=True,
                          age_seconds=prior.age(now), checked_at=prior.checked_at, value=prior.value,
                          started_at=now, finished_at=now, requests=0, bytes=0)

    def _wants_expensive(self, item: Item, stages: Sequence[StageOutcome]) -> bool:
        """Whether this item needs the expensive probe at all (F12).

        ``all_passing`` charges every passing item.  ``until_n`` charges only the
        ones that can still move the number the caller asked for: an item whose
        exit address is already confirmed, or whose address IP is already
        counted, cannot add a new unit, so a judge or bandwidth probe on it would
        spend requests and bytes for a number nothing is waiting for.  A sweep
        with no N keeps paying for every passing item, because then there is no
        unit to be stingy about.
        """
        policy = self.config.expensive_policy
        if policy == EXPENSIVE_NONE:
            return False
        if policy == EXPENSIVE_ALL_PASSING:
            return True
        if not self.config.find.enabled:
            return True
        if self._find_satisfied:
            return False
        what = self.config.find.what
        if what == 'exit':
            known = {stage.exit_ip for stage in stages if stage.exit_ip}
            if known and known <= self._passed_exit_ips:
                return False
        elif what == 'ip':
            address_ip = _ip_of(item.address)
            if address_ip is not None and address_ip in self._passed_ips:
                return False
        return True

    async def _stage(self, item: Item, stage: str) -> StageOutcome:
        runner = self.config.runners.for_stage(stage)
        gate = self._gate
        if runner is None or gate is None:  # pragma: no cover - validated in PipelineConfig
            raise ValidationError(E_VALIDATION_FIELD, f'Для этапа {stage!r} нет runner или gate.')
        target_id = self._pick_target(stage)
        # The per-target slot and its share of the request budget are taken
        # together, under one lock: a check followed by a request would let N
        # concurrent stages all pass the check and then overshoot the budget.
        taken = False
        if target_id is not None:
            if not await self._target_acquire(target_id):
                return StageOutcome(stage, False, code=E_LIMIT_BUDGET, failed_stage='target',
                                    detail=f'Лимит запросов цели {target_id!r} исчерпан.')
            taken = True
        try:
            return await self._measure(item, stage, runner, gate, target_id, taken)
        finally:
            if taken:
                await self._target_release(target_id)

    async def _measure(self, item: Item, stage: str, runner, gate, target_id, taken) -> StageOutcome:
        limit = StageLimit(stage=stage, target_id=target_id,
                           max_requests=gate.remaining_requests() if gate.remaining_requests() else 1,
                           max_bytes=gate.remaining_bytes() or 0,
                           remaining_requests=gate.remaining_requests(),
                           remaining_bytes=gate.remaining_bytes(), remaining_s=gate.remaining_s(),
                           deadline_s=gate.deadline)
        fds = self.config.budgets.fds_per_request
        ram = self.config.budgets.ram_per_inflight_bytes
        outcome: StageOutcome | None = None
        requests = 0
        try:
            await gate.acquire(fds=fds, ram=ram)
        except BudgetExhausted as exc:
            # A spent total ends the run; the item is reported as unmeasured
            # rather than as a measurement that failed.
            self._stop('budget_exhausted', exc.code)
            return StageOutcome(stage, False, code=exc.code, detail=exc.message)
        try:
            try:
                outcome = _coerce_outcome(await runner(item, stage=stage, limit=limit), stage)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # One broken proxy, one broken runner call: the item fails and
                # the chain continues.  A pipeline error keeps its stable
                # ``E_*`` code; anything else is recorded by class name, as the
                # existing scan does with a transport exception.
                code = exc.code if isinstance(exc, PipelineError) else type(exc).__name__
                outcome = StageOutcome(stage, False, code=code, detail=str(exc)[:200])
            requests = max(1, outcome.requests)
            self.concurrency.observe(outcome.ok, outcome.latency_s)
        finally:
            await gate.release(fds=fds, ram=ram, requests=requests,
                               bytes=0 if outcome is None else outcome.bytes)
        if outcome is None:  # pragma: no cover - only reachable via cancellation
            raise ValidationError(E_VALIDATION_FIELD, f'Этап {stage!r} не дал результата.')
        return outcome

    # -- per-target limits --------------------------------------------------

    def _pick_target(self, stage: str) -> str | None:
        """The expensive stage's target, preferring the one that has been used
        least.  Cheap and basic are the pipeline's own probes and have no
        configured target."""
        if stage != STAGE_EXPENSIVE or not self._target_policies:
            return None
        return min(self._target_policies, key=lambda name: (self._target_used.get(name, 0), name))

    def target_used(self, target_id: str) -> int:
        """How many requests the target has already been given."""
        return self._target_used.get(target_id, 0)

    def _target_exhausted(self, target_id: str) -> bool:
        policy = self._target_policies.get(target_id)
        if policy is None or policy.max_requests is None:
            return False
        return self._target_used.get(target_id, 0) >= policy.max_requests

    async def _target_acquire(self, target_id: str) -> bool:
        """Take one in-flight slot and one unit of the target's request budget.

        Both happen under the same condition lock, so a stage either gets both
        or gets neither: a budget that could be overshot by concurrency is not
        a budget.  False means the target is out of requests, and the caller
        must not run a probe for it.
        """
        policy = self._target_policies.get(target_id, TargetPolicy(target_id))
        async with self._target_cond:
            while True:
                if policy.max_requests is not None and self._target_used.get(target_id, 0) >= policy.max_requests:
                    return False
                if self._target_inflight.get(target_id, 0) < policy.max_inflight:
                    break
                await self._target_cond.wait()
            self._target_inflight[target_id] = self._target_inflight.get(target_id, 0) + 1
            if policy.max_requests is not None:
                self._target_used[target_id] = self._target_used.get(target_id, 0) + 1
        return True

    async def _target_release(self, target_id: str) -> None:
        async with self._target_cond:
            self._target_inflight[target_id] = max(0, self._target_inflight.get(target_id, 0) - 1)
            self._target_cond.notify_all()

    # -- recording ----------------------------------------------------------

    def _result(self, item: Item, stages: Sequence[StageOutcome], started_wall: float,
                started: float) -> ItemResult:
        ok = bool(stages) and all(outcome.ok for outcome in stages)
        if not stages:
            state = 'unreachable'
        elif not ok:
            first = next(outcome for outcome in stages if not outcome.ok)
            state = 'unreachable' if first.kind == STAGE_CHEAP or first.failed_stage == 'tcp' else 'failed'
        else:
            state = 'done'
        exit_ip = next((outcome.exit_ip for outcome in reversed(stages) if outcome.exit_ip), None)
        value = next((outcome.value for outcome in reversed(stages) if outcome.value is not None), None)
        reason = next((outcome.code for outcome in stages if not outcome.ok and outcome.code), None)
        finished = self.clock.monotonic()
        return ItemResult(
            endpoint=item.endpoint, state=state, ok=ok, stages=tuple(stages), reason_code=reason,
            exit_ip=exit_ip, address=item.address, carried=False, age_seconds=None,
            checked_at=self.clock.time(), value=value, started_at=started_wall,
            finished_at=started_wall + (finished - started),
            requests=sum(max(1, outcome.requests) for outcome in stages),
            bytes=sum(outcome.bytes for outcome in stages))

    async def _record(self, result: ItemResult, emit) -> None:
        """The one place a result becomes visible: metrics, counters, find-N,
        retained results, the ``on_result`` hook and the consumer's stream, in
        that order."""
        now = self.clock.monotonic()
        self._measured += 1
        if self._first_at is None:
            self._first_at = now - self._started
        counters = self._counters
        if result.carried:
            counters.carried += 1
        if result.ok:
            if self._first_pass_at is None:
                self._first_pass_at = now - self._started
            counters.passed_endpoints += 1
            if result.address and result.address not in self._passed_addresses:
                self._passed_addresses.add(result.address)
            # The find-N check runs *before* this item joins the sets, so a unit
            # is counted exactly once no matter which unit the caller asked for,
            # and ``find.met`` can never disagree with the counter it advances.
            self._advance_find(result)
            address_ip = _ip_of(result.address)
            if address_ip and address_ip not in self._passed_ips:
                self._passed_ips.add(address_ip)
                counters.passed_unique_ips = len(self._passed_ips)
            if result.exit_ip and result.exit_ip not in self._passed_exit_ips:
                self._passed_exit_ips.add(result.exit_ip)
                counters.passed_exit_ips = len(self._passed_exit_ips)
                counters.confirmed_exit_ips = len(self._passed_exit_ips)
        if len(self._results) < self.config.budgets.max_stored_results:
            self._results.append(result)
        if result.ok:
            self._passed.append(result)
        if self.config.on_result is not None:
            self._call_hook(self.config.on_result, result)
        await emit(result)
        self._stop_on_budget_or_want()

    def _stop_on_budget_or_want(self) -> None:
        gate = self._gate
        if gate is None:  # pragma: no cover - the gate exists before any stage runs
            return
        try:
            gate.check_totals()
        except BudgetExhausted as exc:
            self._stop('budget_exhausted', exc.code)
            return
        if self._find_satisfied:
            self._stop('want_reached', 'want_reached')

    def _call_hook(self, hook, *args) -> None:
        """A hook must not be able to break the chain.  A coroutine hook is
        closed rather than awaited, because the record path is synchronous in
        its bookkeeping and an unawaited coroutine would leak a warning."""
        try:
            outcome = hook(*args)
        except Exception:
            return
        if asyncio.iscoroutine(outcome):
            with contextlib.suppress(RuntimeError):
                outcome.close()

    def _find_met_count(self) -> int:
        """How many units of the requested kind have been confirmed so far.

        It is a projection of the cumulative sets rather than an own counter, so
        a resumed run continues the same N instead of counting the same exit
        addresses a second time.
        """
        policy = self.config.find
        if not policy.enabled:
            return 0
        if policy.what == 'endpoint':
            return self._counters.passed_endpoints
        if policy.what == 'ip':
            return len(self._passed_ips)
        return len(self._passed_exit_ips)

    def _advance_find(self, result: ItemResult) -> None:
        """Count toward N in exactly one of the three units the caller asked
        for, and never in a unit the result does not carry: a hostname endpoint
        has no IP to count, and an endpoint nobody measured through a target has
        no confirmed exit IP."""
        policy = self.config.find
        if not policy.enabled or self._find_satisfied:
            return
        what = policy.what
        if what == 'endpoint':
            counted = True
        elif what == 'ip':
            address_ip = _ip_of(result.address)
            counted = address_ip is not None and address_ip not in self._passed_ips
        else:
            counted = bool(result.exit_ip) and result.exit_ip not in self._passed_exit_ips
        if not counted:
            return
        if self._find_met_count() + 1 >= policy.n:
            self._find_satisfied = True
            if self._time_to_n is None:
                self._time_to_n = self.clock.monotonic() - self._started

    # -- summary ------------------------------------------------------------

    def _snapshot(self) -> RunResult:
        config = self.config
        gate = self._gate
        resources = gate.snapshot() if gate is not None else ResourceSnapshot()
        counters = self._counters
        counters.requests = resources.requests
        counters.bytes = resources.bytes
        counters.cpu_s = resources.cpu_s
        find = FindProgress(target=config.find.n, what=config.find.what, met=self._find_met_count(),
                            unique_endpoints=counters.unique_endpoints, unique_ips=counters.unique_ips,
                            confirmed_exit_ips=counters.confirmed_exit_ips,
                            satisfied=self._find_satisfied, complete=self._feed_complete)
        metrics = RunMetrics(
            wall_s=self.clock.monotonic() - self._started, time_to_first_s=self._first_at,
            time_to_first_pass_s=self._first_pass_at, time_to_n_s=self._time_to_n, measured=self._measured,
            workers=config.budgets.worker_ceiling(), peak_inflight=resources.peak_inflight,
            peak_fds=resources.peak_fds, peak_ram_reserved_bytes=resources.peak_ram_bytes,
            queue_high_water=self._queue_high_water, blocked_waits=resources.blocked_waits,
            cpu_s=resources.cpu_s, host_waits=self._hosts.waits,
            concurrency=self.concurrency.snapshot().to_public())
        return RunResult(state=self._state or 'complete', reason=self._reason or 'items_exhausted',
                         counters=counters, find=find, metrics=metrics, results=tuple(self._results),
                         passed=tuple(self._passed), remaining=tuple(sorted(self.ledger.pending)),
                         feed_complete=self._feed_complete, resources=resources)


def _group_reason(group: BaseException) -> str:
    """The first non-nested exception class of a TaskGroup failure."""
    for error in group.exceptions:
        if isinstance(error, BaseExceptionGroup):
            return _group_reason(error)
        return type(error).__name__
    return type(group).__name__


async def run_pipeline(config: PipelineConfig, *, clock: PipelineClock | None = None,
                       control: PipelineControl | None = None,
                       concurrency: AdaptiveConcurrency | None = None) -> RunResult:
    """Convenience wrapper: build a :class:`Pipeline` and run it once."""
    return await Pipeline(config, clock=clock, control=control, concurrency=concurrency).run()


# --------------------------------------------------------------------------
# benchmarks
# --------------------------------------------------------------------------

BENCHMARK_SCHEMA = 1
BENCHMARK_FIXTURE = 'synthetic-http-v1'

SYNTHETIC_NOTICE = (
    'Локальная синтетическая фикстура: ни один публичный прокси, ни DNS, ни DNSBL и ни один сетевой '
    'запрос не использовались. Числа описывают только эту фикстуру и не являются измерением скорости '
    'живых публичных прокси.'
)

#: Documentation ranges only (RFC 5737, RFC 3849).  Nothing here is routable, so
#: a fixture can never turn into real traffic.
_FIXTURE_V4 = ('198.51.100.', '203.0.113.', '192.0.2.')
_FIXTURE_V6 = '2001:db8::'
_FIXTURE_PORTS = (8080, 3128, 1080)


def synthetic_endpoints(count: int) -> tuple[str, ...]:
    """A deterministic corpus of canonical endpoints from documentation ranges.

    Deterministic on purpose: the same ``count`` gives the same corpus on every
    machine, so a benchmark is comparable between runs and between revisions.
    """
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ValidationError(E_VALIDATION_FIELD,
                              f'synthetic_endpoints.count: целое >= 1, получено {count!r}.')
    out = []
    for index in range(count):
        if index % 8 == 7:
            # IPv6 is bracketed in a canonical address (proxytool._normalize_proxy),
            # and the counter starts at 1 so no host is the ``::0`` spelling whose
            # compressed form is the bare ``::``.
            host = f'[{_FIXTURE_V6}{index // 8 + 1:x}]'
        else:
            host = f'{_FIXTURE_V4[index % len(_FIXTURE_V4)]}{index % 254 + 1}'
        out.append(f'http://{host}:{_FIXTURE_PORTS[index % len(_FIXTURE_PORTS)]}')
    return tuple(out)


def fixture_digest(endpoints: Iterable[str]) -> str:
    """A content address of a corpus, so a benchmark states which fixture it ran."""
    return hashlib.sha256('\n'.join(endpoints).encode('utf-8')).hexdigest()[:20]


def fixture_normalize(value: str) -> str | None:
    """A normaliser for the documentation ranges the fixture lives in.

    The public normaliser rejects them on purpose — RFC 5737 and RFC 3849 space
    is not globally routable, and a benchmark corpus must not be.  This function
    exists only so the benchmark can drive the real chain, and it is never used
    by a run that talks to real endpoints.  It answers like the project's
    normaliser: a canonical string, or ``None`` for anything it cannot read.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    scheme = 'http'
    if '://' in text:
        scheme, _, text = text.partition('://')
        scheme = scheme.lower()
        if scheme not in ('http', 'https', 'socks4', 'socks5'):
            return None
    host, _, port = text.rpartition(':')
    if not host or not port.isdigit():
        return None
    bare = host[1:-1] if host.startswith('[') and host.endswith(']') else host
    address = _ip_of(bare)
    if address is None:
        return None
    # An IPv6 literal is bracketed in a canonical address, exactly as the
    # project's own normaliser writes it.
    canonical = f'[{address}]' if ':' in address else address
    return f'{scheme}://{canonical}:{int(port)}'


@dataclass(frozen=True)
class Benchmark:
    """A measurement of *this* fixture, on this machine, at this moment.

    ``synthetic`` is not decoration: the only way to fill a :class:`Benchmark` is
    :func:`benchmark`, which never touches the network, and every public view
    carries :data:`SYNTHETIC_NOTICE` so the number cannot be quoted as the
    throughput of live public proxies.
    """

    fixture: str
    schema: int
    digest: str
    items: int
    find_n: int
    find_what: str
    measured: int
    wall_s: float
    time_to_first_s: float
    time_to_first_pass_s: float | None
    time_to_n_s: float | None
    items_per_s: float
    peak_ram_bytes: int
    ram_delta_bytes: int
    peak_inflight: int
    requests: int
    bytes: int
    workers: int
    state: str
    synthetic: bool = True
    notice: str = SYNTHETIC_NOTICE

    def to_public(self) -> dict:
        return {'fixture': self.fixture, 'schema': self.schema, 'digest': self.digest, 'items': self.items,
                'find_n': self.find_n, 'find_what': self.find_what, 'measured': self.measured,
                'wall_s': round(self.wall_s, 4), 'time_to_first_s': round(self.time_to_first_s, 4),
                'time_to_first_pass_s': (None if self.time_to_first_pass_s is None
                                         else round(self.time_to_first_pass_s, 4)),
                'time_to_n_s': None if self.time_to_n_s is None else round(self.time_to_n_s, 4),
                'items_per_s': round(self.items_per_s, 2), 'peak_ram_bytes': self.peak_ram_bytes,
                'ram_delta_bytes': self.ram_delta_bytes, 'peak_inflight': self.peak_inflight,
                'requests': self.requests, 'bytes': self.bytes, 'workers': self.workers,
                'state': self.state, 'synthetic': self.synthetic, 'notice': self.notice}


def fixture_source(source_id: str, endpoints: Sequence[str], *, chunk: int = 64) -> SourceSpec:
    """A source that yields its corpus in bounded chunks, with no I/O at all."""
    body = ('\n'.join(endpoints) + '\n').encode('utf-8')

    async def fetch() -> AsyncIterator[bytes]:
        for start in range(0, len(body), chunk):
            yield body[start:start + chunk]

    return SourceSpec(source_id=source_id, fetch=fetch)


def fixture_runner(*, ok_every: int = 1, latency_s: float = 0.0, body_bytes: int = 1024) -> tuple[Runner, list]:
    """A local stub with the runner contract.

    It never opens a socket, and it reports the requests and bytes it is charged
    for, so the budgets stay honest instead of being bypassed by the fixture.
    """
    calls: list[str] = []

    async def runner(item: Item, *, stage: str, limit: StageLimit) -> StageOutcome:
        calls.append(item.endpoint)
        if limit.remaining_s is not None and limit.remaining_s <= 0:
            return StageOutcome(stage, False, code=DEADLINE_EXCEEDED, failed_stage='target')
        if latency_s > 0:
            await asyncio.sleep(latency_s)
        good = (len(calls) % ok_every) == 0
        return StageOutcome(kind=stage, ok=good, code=None if good else 'UNREACHABLE',
                            failed_stage=None if good else 'tcp', latency_ms=latency_s * 1000.0,
                            bytes=body_bytes, requests=1,
                            exit_ip=f'203.0.113.{len(calls) % 254 + 1}' if good else None)

    return runner, calls


def _ram_measurement(enabled: bool, block):
    """Run ``block`` and report (memory before, peak memory) of its allocations.

    Tracing is started and stopped here, so this helper owns the counters for
    the duration of the block and must not be nested inside another measurement.
    ``tracemalloc.is_running`` is missing on some builds of this interpreter, so
    nothing depends on it.  A build with no working ``tracemalloc`` at all
    reports zeros rather than raising: a benchmark that cannot see its own memory
    is no reason to fail a run, but it must not report a number it did not
    measure either.
    """
    if not enabled:
        block()
        return 0, 0
    try:
        tracemalloc.start()
    except (RuntimeError, ValueError):  # pragma: no cover - build without tracemalloc
        block()
        return 0, 0
    try:
        before = tracemalloc.get_traced_memory()[0]
        block()
        return before, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def benchmark(*, items: int = 2000, find_n: int = 20, what: str = 'exit', latency_s: float = 0.0,
              ok_every: int = 4, measure_ram: bool = True, max_queue_items: int = 128) -> Benchmark:
    """Time-to-first, time-to-N and RAM of the chain on the agreed fixture.

    Everything is local and deterministic: the corpus is
    :func:`synthetic_endpoints`, the measurement is :func:`fixture_runner`, and
    no clock, socket, DNS or third-party service is involved.  Absolute numbers
    are machine dependent; the reproducible parts are ``digest`` (the corpus),
    ``state`` (what the chain decided) and the RAM ceiling, which follows the
    budget rather than the corpus size.  Call it from synchronous code: it
    owns its own event loop.
    """
    endpoints = synthetic_endpoints(items)
    clock = SystemClock()
    cheap, cheap_calls = fixture_runner(ok_every=ok_every, latency_s=latency_s)
    basic, _ = fixture_runner(ok_every=ok_every, latency_s=latency_s)
    expensive, _ = fixture_runner(ok_every=1, latency_s=latency_s)
    config = PipelineConfig(
        sources=(fixture_source('fixture', endpoints),),
        budgets=Budgets(max_queue_items=max_queue_items, max_results_pending=max_queue_items,
                        max_requests=None, max_bytes=None),
        find=FindPolicy(n=find_n, what=what),
        runners=Runners(cheap=cheap, basic=basic, expensive=expensive),
        expensive_policy=EXPENSIVE_UNTIL_N, normalize=fixture_normalize, on_result=None)
    holder: dict[str, Any] = {}
    before, peak = _ram_measurement(
        measure_ram, lambda: holder.setdefault('result', asyncio.run(run_pipeline(config, clock=clock))))
    result = holder['result']
    return Benchmark(
        fixture=BENCHMARK_FIXTURE, schema=BENCHMARK_SCHEMA, digest=fixture_digest(endpoints), items=items,
        find_n=find_n, find_what=what, measured=result.metrics.measured, wall_s=result.metrics.wall_s,
        time_to_first_s=result.metrics.time_to_first_s or 0.0,
        time_to_first_pass_s=result.metrics.time_to_first_pass_s, time_to_n_s=result.metrics.time_to_n_s,
        items_per_s=result.metrics.items_per_s, peak_ram_bytes=peak, ram_delta_bytes=max(0, peak - before),
        peak_inflight=result.metrics.peak_inflight, requests=result.counters.requests,
        bytes=result.counters.bytes, workers=result.metrics.workers, state=result.state)
