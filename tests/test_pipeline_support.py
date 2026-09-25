"""Shared fakes for the F12 pipeline tests.

Everything here is local: no socket, no DNS, no clock dependency and no
public proxy.  The addresses come from the RFC 5737 / RFC 3849 documentation
ranges, and the clock is injectable, so a test can pin time instead of sleeping.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import pipeline as pl


class FakeClock(pl.PipelineClock):
    """A clock that only moves when a test moves it.

    ``remaining_s`` returns ``None``, so a run with a virtual clock is bounded by
    the pipeline's own boundary checks instead of by a real wall-clock timeout.
    That is what makes the timing assertions in the tests exact.
    """

    def __init__(self, *, start=1000.0, cpu=0.0):
        self.now = float(start)
        self.cpu_now = float(cpu)

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def cpu(self):
        return self.cpu_now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now

    def remaining_s(self, deadline):
        return None


class StepClock(FakeClock):
    """A virtual clock that advances by a fixed step on every reading, so a run
    of ``n`` stages takes exactly ``n * step`` of virtual time."""

    def __init__(self, *, step=0.5, start=1000.0, cpu_step=0.0):
        super().__init__(start=start)
        self.step = float(step)
        self.cpu_step = float(cpu_step)

    def monotonic(self):
        value = self.now
        self.now += self.step
        return value

    def time(self):
        return self.monotonic()

    def cpu(self):
        value = self.cpu_now
        self.cpu_now += self.cpu_step
        return value


def normalize(value):
    """The fixture normaliser: documentation ranges only, by design."""
    return pl.fixture_normalize(value)


def chunk_source(source_id, endpoints, *, chunk=32, calls=None, suffix=''):
    """A source that hands out the corpus in fixed chunks and counts its pulls.

    ``suffix`` is appended to every line, which is how a test puts an
    unparseable token into an otherwise good corpus.
    """
    body = ('\n'.join(f'{item}{suffix}' for item in endpoints) + '\n').encode('utf-8')
    chunks = [body[start:start + chunk] for start in range(0, len(body), chunk)] or [b'']

    async def fetch():
        for piece in chunks:
            if calls is not None:
                calls.append(len(piece))
            yield piece

    return pl.SourceSpec(source_id=source_id, fetch=fetch)


def recording_runner(*, ok=True, latency_s=0.0, requests=1, body_bytes=0, exit_ip=None,
                     value=None, code=None, failed_stage=None, calls=None, budget=None,
                     raises=None, action=None):
    """A runner with the pipeline's contract that records what it was asked.

    ``budget`` makes the runner respect ``limit.remaining_requests`` itself,
    which is how a test shows that the cap handed to a runner is a cap.
    ``action`` is awaited after the outcome is built, which is how a test pauses
    or cancels the chain from the middle of a measurement.
    """
    records = [] if calls is None else calls

    async def runner(item, *, stage, limit):
        records.append((stage, item.endpoint, limit))
        if budget is not None and limit.remaining_requests is not None and limit.remaining_requests <= 0:
            return pl.StageOutcome(stage, False, code=pl.E_LIMIT_BUDGET, failed_stage='target',
                                   detail='runner stopped on the handed-in cap')
        if latency_s:
            await asyncio.sleep(latency_s)
        if raises is not None:
            raise raises
        outcome = pl.StageOutcome(kind=stage, ok=ok, code=None if ok else (code or 'UNREACHABLE'),
                                  failed_stage=None if ok else (failed_stage or 'tcp'),
                                  latency_ms=latency_s * 1000.0, bytes=body_bytes, requests=requests,
                                  exit_ip=exit_ip, value=value)
        if action is not None:
            await action(item, stage, outcome)
        return outcome

    return runner


def exit_runner(exits, calls=None, latency_s=0.0):
    """A runner that reports one of ``exits`` per *endpoint*, in arrival order.

    Keying by endpoint rather than by call is what a real probe does: the same
    endpoint answers with the same exit address in every stage, and two different
    endpoints may share one exit address.  The last value repeats once the list
    is exhausted.
    """
    records = [] if calls is None else calls
    order = list(exits)
    seen: dict[str, str | None] = {}

    async def runner(item, *, stage, limit):
        records.append((stage, item.endpoint))
        if item.endpoint not in seen:
            index = min(len(seen), max(0, len(order) - 1))
            seen[item.endpoint] = order[index] if order else None
        if latency_s:
            await asyncio.sleep(latency_s)
        return pl.StageOutcome(stage, True, latency_ms=latency_s * 1000.0 or 1.0, requests=1, bytes=16,
                               exit_ip=seen[item.endpoint])

    return runner


def config(sources, runners, **kwargs):
    """A :class:`PipelineConfig` with the fixture normaliser by default.

    A stage whose runner is ``None`` is switched off unless the caller said
    otherwise: asking for a probe that was not given is a configuration error in
    the module, and a test should not have to spell that out every time.
    """
    defaults = {'sources': tuple(sources), 'runners': runners, 'normalize': normalize}
    if runners.cheap is None:
        defaults['run_cheap'] = False
    if runners.basic is None:
        defaults['run_basic'] = False
    if runners.expensive is None:
        defaults['run_expensive'] = False
    defaults.update(kwargs)
    return pl.PipelineConfig(**defaults)


def addresses(count):
    return pl.synthetic_endpoints(count)
