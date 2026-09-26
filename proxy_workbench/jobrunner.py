"""Execute the durable API/GUI queue through the existing scan engine.

One runner is shared by a data folder. The operating-system workbench lock also
excludes CLI scans; an occupied writer leaves jobs queued for the next poll.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from . import db, jobs, scheduler
from .maintenance import exclusive_lock


def resolve_profile(workbench, profile_id=None, revision=None):
    """Resolve a pinned named revision, legacy profile, or the configured default."""
    from . import proxytool
    conn = workbench.conn
    wanted = str(profile_id or '')
    if wanted and wanted != 'unprofiled':
        row = conn.execute('SELECT id, config, revision FROM profiles WHERE id=?',
                           (f'{wanted}@{int(revision or 1)}',)).fetchone()
        if row is None:
            row = conn.execute('SELECT id, config, revision FROM profiles WHERE id=?', (wanted,)).fetchone()
        if row is None:
            raise jobs.Validation('profile is unavailable: ' + wanted)
        identifier = str(row[0]).rsplit('@', 1)[0] if row[2] else str(row[0])
        return identifier, int(row[2] or revision or 1), json.loads(row[1])
    try:
        previous = (workbench.data / 'last-profile.txt').read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError):
        previous = ''
    if previous and previous != 'unprofiled':
        try:
            return resolve_profile(workbench, previous, revision)
        except jobs.Validation:
            pass
    row = conn.execute('SELECT id, revision FROM profiles WHERE is_default=1 '
                       'ORDER BY revision DESC LIMIT 1').fetchone()
    if row is not None:
        return resolve_profile(workbench, str(row[0]), row[1])
    path = workbench.data / 'gui-targets.json'
    args = proxytool.parser().parse_args(['scan', '--data', str(workbench.data)])
    args.config = path if path.is_file() else None
    config = proxytool.target_config(args)
    encoded = json.dumps(config, sort_keys=True)
    identifier = hashlib.sha256(encoded.encode()).hexdigest()[:20]
    db_conn = workbench.conn
    db_conn.execute('INSERT OR IGNORE INTO profiles(id, config, digest, created_at) VALUES (?,?,?,?)',
                    (identifier, encoded, identifier, workbench.clock()))
    db_conn.commit()
    return identifier, 1, config


def _scan_config(workbench, config):
    """Convert selected service rules into concrete catalog probes."""
    from . import profiles, proxytool, servicecatalog
    targets = config.get('targets') or []
    if targets and all(isinstance(target, dict) and target.get('url') for target in targets):
        return dict(config), None, None
    spec = profiles.ProfileSpec.from_dict(config)
    catalog = servicecatalog.load_catalog()
    resolved, groups = [], {}
    for rule in spec.targets:
        if not rule.enabled:
            continue
        preset = catalog.preset(rule.id)
        indices = []
        for target in servicecatalog.build_targets((preset,)):
            indices.append(len(resolved))
            resolved.append(target)
        groups[rule.id] = indices
    args = proxytool.parser().parse_args(['scan', '--data', str(workbench.data)])
    args.attempts = spec.attempts
    if spec.budget.max_duration_s is not None:
        args.timeout = min(args.timeout, spec.budget.max_duration_s)
    return proxytool.validate_targets(resolved, args), spec, groups


def queue_items(workbench, collection_id):
    """Freeze the collection membership when submitting a check."""
    from . import proxytool
    if not workbench.conn.execute('SELECT 1 FROM collections WHERE id=?', (collection_id,)).fetchone():
        raise jobs.Validation('collection is unavailable: ' + collection_id)
    return [jobs.QueueItem(endpoint_id=row[0], access_id=proxytool.PUBLIC_ACCESS_ID, access_revision=1)
            for row in workbench.conn.execute('SELECT endpoint_id FROM membership '
                                              'WHERE collection_id=? ORDER BY endpoint_id',
                                              (collection_id,))]


def submit_schedule(data, run, *, db_path=None):
    """Create exactly one durable job for a requested schedule occurrence."""
    from . import proxytool
    with proxytool.Workbench(data, db_path=db_path) as workbench:
        existing = workbench.conn.execute('SELECT id FROM job WHERE idempotency_key=?',
                                          (run.run_id,)).fetchone()
        if existing is not None:
            return workbench.jobs().job(existing[0])
        action = run.action or ('refill' if run.pool_id else 'check')
        collection = run.collection_id or db.PUBLIC_COLLECTION_ID
        profile_id, revision = None, None
        if run.pool_id:
            pool = workbench.pools().require(run.pool_id)
            collection, profile_id, revision = pool.collection_id, pool.profile_id, pool.profile_revision
        if action not in ('source', 'refill'):
            profile_id, revision, _ = resolve_profile(workbench, profile_id, revision)
        kind = {'source': 'collect', 'refill': 'pool_refill'}.get(action, action)
        if kind == 'pool_refill' and not run.pool_id:
            raise jobs.Validation('refill requires a pool')
        filters = {'schedule_run_id': run.run_id, 'schedule_id': run.schedule_id,
                   'pool_id': run.pool_id, 'stages': list(run.stages)}
        budgets = {'max_requests': run.budgets.requests, 'max_bytes': run.budgets.bytes,
                   'max_seconds': run.budgets.seconds, 'workers': run.budgets.concurrency}
        scope = jobs.Scope(collection_id=str(collection), profile_id=profile_id or 'unprofiled',
                           profile_revision=int(revision or 1), filters=filters, budgets=budgets)
        items = queue_items(workbench, collection) if kind in ('check', 'recheck', 'pool_recheck') else ()
        workbench.conn.commit()
        return workbench.jobs().submit(kind, scope, items, idempotency_key=run.run_id)


def _minimum(*values):
    values = [value for value in values if value is not None]
    return min(values) if values else None


class JobRunner:
    def __init__(self, data, *, db_path=None, scan=None, collect=None, poll_seconds=1.0):
        self.data = Path(data).resolve()
        self.db_path = Path(db_path).resolve() if db_path else self.data / db.DB_FILENAME
        self.scan = scan
        self.collect = collect
        self.poll_seconds = poll_seconds
        self._work_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._users = 0
        self.last_error = None

    def start(self):
        """Acquire one surface's use of the shared background worker."""
        with self._state_lock:
            self._users += 1
            if self._thread is None or not self._thread.is_alive():
                self._stop = threading.Event()
                self._thread = threading.Thread(target=self._loop, daemon=True,
                                                name='workbench-job-runner')
                self._thread.start()
            else:
                self._stop.clear()
        return self

    def stop(self, timeout=5.0):
        """Release one surface; another API/GUI owner keeps its worker alive."""
        with self._state_lock:
            self._users = max(0, self._users - 1)
            if self._users:
                return
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _loop(self):
        while not self._stop.is_set():
            error = None
            try:
                from .schedule_runtime import runtime_for
                runtime_for(self.data, db_path=self.db_path).tick()
            except Exception as exc:
                error = getattr(exc, 'code', None) or type(exc).__name__
            try:
                self.run_pending()
            except Exception as exc:
                error = getattr(exc, 'code', None) or type(exc).__name__
            self.last_error = error
            self._stop.wait(self.poll_seconds)

    def run_pending(self, limit=1):
        """Run bounded queued work; never steal the CLI's writer or duplicate a job."""
        if not self.db_path.is_file() or not self._work_lock.acquire(blocking=False):
            return []
        try:
            with contextlib.ExitStack() as stack:
                try:
                    stack.enter_context(exclusive_lock(self.data / 'workbench.lock'))
                except RuntimeError:
                    return []
                from . import proxytool
                workbench = stack.enter_context(proxytool.Workbench(self.data, db_path=self.db_path))
                store = workbench.jobs()
                # A live CLI worker holds the same OS lock. Do not overwrite
                # any stale state here: recovery remains an explicit job action.
                if store.jobs(state='running', limit=1):
                    store.recover()
                    return []
                pending = list(reversed(store.jobs(state='queued')))
                reports = []
                for job in pending[:max(0, int(limit))]:
                    try:
                        reports.append(self._execute(workbench, job))
                    except jobs.Busy:
                        break
                return reports
        finally:
            self._work_lock.release()

    async def _controlled(self, workbench, job, operation):
        """Observe control on a fresh connection while the engine awaits I/O."""
        task = asyncio.create_task(operation)
        control = db.connect(self.db_path, read_only=True)
        last_heartbeat = time.monotonic()
        try:
            while not task.done():
                await asyncio.wait((task,), timeout=0.1)
                if task.done():
                    break
                row = control.execute('SELECT state FROM job WHERE id=?', (job.id,)).fetchone()
                state = row[0] if row else 'cancelled'
                if state != 'running' or self._stop.is_set():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    if state == 'running':
                        workbench.jobs().pause(job.id, reason_code=jobs.CODE_INTERRUPTED)
                    return None
                if time.monotonic() - last_heartbeat >= 2:
                    workbench.jobs().heartbeat(job.id, runner='durable')
                    last_heartbeat = time.monotonic()
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            control.close()

    def _execute(self, workbench, job):
        from . import api, proxytool
        store = workbench.jobs()
        if store.job(job.id).state != 'queued':
            return {'job_id': job.id, 'state': store.job(job.id).state}
        filters, limits = dict(job.scope.filters), dict(job.scope.budgets)
        previous_cost = (store.checkpoint(job.id, 'execution') or {}).get('state') or {}
        for field in ('requests', 'bytes', 'seconds'):
            key = 'max_' + field
            if limits.get(key) is not None:
                limits[key] = max(0, limits[key] - previous_cost.get(field, 0))
        state, started = {}, time.monotonic()
        try:
            if job.kind not in ('check', 'scan', 'quick_test', 'recheck', 'pool_recheck',
                                'collect', 'source', 'pool_refill', 'export'):
                raise jobs.Validation('unsupported queued job kind: ' + job.kind)
            stage = scheduler.SOURCE if job.kind in ('collect', 'source') else scheduler.PROBE
            stages = filters.get('stages')
            if stages is not None and stage not in stages and job.kind != 'export':
                raise jobs.Validation('the scheduled power policy does not allow this work')
            schedule_id = filters.get('schedule_id')
            if schedule_id:
                schedule_engine = scheduler.Scheduler(workbench.schedules(), clock=workbench.clock)
                spec = schedule_engine.get(schedule_id)
                if spec is None:
                    raise jobs.Validation('the schedule was removed before execution')
                if schedule_engine.blocked_by(schedule_id) is not None:
                    store.cancel(job.id, reason_code='E_SCHEDULE_PAUSED')
                    self._record_schedule(workbench, store.job(job.id), {})
                    return {'job_id': job.id, 'state': 'cancelled', 'reason': 'schedule_paused'}
                ledger = schedule_engine.ledger(schedule_id)
                snapshot = ledger.snapshot()
                for field in ('requests', 'bytes', 'seconds'):
                    view = getattr(snapshot, field)
                    if view.limit is not None:
                        limits['max_' + field] = _minimum(limits.get('max_' + field), view.remaining)
                if any(limits.get(key) is not None and limits[key] <= 0
                       for key in ('max_requests', 'max_bytes', 'max_seconds')):
                    store.cancel(job.id, reason_code='E_LIMIT_BUDGET')
                    self._record_schedule(workbench, store.job(job.id), {})
                    return {'job_id': job.id, 'state': 'cancelled', 'reason': 'schedule_budget'}
                workbench.schedules().save_state(schedule_id, schedule_engine.state(schedule_id))
            store.start(job.id)
            store.heartbeat(job.id, runner='durable')
            if any(limits.get(key) is not None and limits[key] <= 0
                   for key in ('max_requests', 'max_bytes', 'max_seconds')):
                state.update(state='partial', stop_reason='E_LIMIT_BUDGET')
            elif job.kind in ('collect', 'source'):
                urls = proxytool.resolve_collect_sources(SimpleNamespace(data=self.data, sources=None,
                                                                         no_sources=False))
                collector = self.collect or proxytool.collect
                kwargs = {'collection_id': job.scope.collection_id, 'quiet': True}
                for key in ('max_requests', 'max_bytes', 'max_items'):
                    if limits.get(key) is not None:
                        kwargs[key] = int(limits[key])
                want = int(filters.get('want') or 0)
                if want:
                    existing = workbench.conn.execute('SELECT count(*) FROM membership WHERE collection_id=?',
                                                       (job.scope.collection_id,)).fetchone()[0]
                    kwargs['max_items'] = _minimum(kwargs.get('max_items'), max(0, want - existing))
                async def collect_once():
                    operation = collector(workbench.conn, urls, [], **kwargs)
                    if limits.get('max_seconds') is not None:
                        return await asyncio.wait_for(operation, limits['max_seconds'])
                    return await operation
                result = asyncio.run(self._controlled(workbench, job, collect_once()))
                if result is not None:
                    state.update({key: result[key] for key in ('raw_rows', 'unique', 'blocked', 'sources_total')
                                  if key in result})
                    spent = result.get('budget') or {}
                    state['requests'] = spent.get('requests', sum(item.get('attempts', 0)
                                                                  for item in result.get('sources', ())))
                    state['bytes'] = spent.get('bytes', sum(item.get('bytes', 0)
                                                            for item in result.get('sources', ())))
                    if result.get('budget_exhausted') or (result.get('sources') and
                            all(item.get('error') for item in result['sources'])):
                        state['state'] = 'partial'
                    if want:
                        available = workbench.conn.execute('SELECT count(*) FROM membership WHERE collection_id=?',
                                                           (job.scope.collection_id,)).fetchone()[0]
                        if available >= want:
                            state.update(state='complete', stop_reason='want_reached')
                        else:
                            state.update(state='partial', stop_reason='want_short_endpoint')
            elif job.kind == 'pool_refill':
                pool_id = filters.get('pool_id')
                if not pool_id:
                    raise jobs.Validation('pool_refill requires filters.pool_id')
                result = workbench.pool_refill(pool_id, api.pool_candidate_source(workbench.conn),
                                               budget=limits.get('max_requests'))
                state.update(result.to_dict() if hasattr(result, 'to_dict') else {'served': result.served})
            elif job.kind == 'export':
                profile, revision, _ = resolve_profile(workbench, job.scope.profile_id,
                                                       job.scope.profile_revision)
                state.update(proxytool.export(workbench.conn, profile, self.data / 'exports',
                                              collection_id=job.scope.collection_id,
                                              profile_revision=revision,
                                              active_profile_path=self.data / 'last-profile.txt'))
            else:
                profile, revision, raw = resolve_profile(workbench, job.scope.profile_id,
                                                         job.scope.profile_revision)
                config, named, groups = _scan_config(workbench, raw)
                if named is not None:
                    limits['max_requests'] = _minimum(limits.get('max_requests'), named.budget.max_probes)
                    limits['max_seconds'] = _minimum(limits.get('max_seconds'), named.budget.max_duration_s)
                if job.kind == 'quick_test':
                    config = dict(config, attempts=1)
                if stages is not None:
                    if scheduler.SPEEDTEST not in stages:
                        config.pop('speedtest', None)
                    if scheduler.JUDGE not in stages:
                        config.pop('anonymity', None)
                probe = proxytool.check_proxy
                if named is not None:
                    async def named_probe(proxy, config, rate, own_ips=None):
                        row = await proxytool.check_proxy(proxy, config, rate, own_ips)
                        evidence = []
                        for target_id, indices in groups.items():
                            subsets = [[sample for sample in row['samples'] if sample['target'] == index]
                                       for index in indices]
                            ok = min(sum(sample['ok'] for sample in subset) for subset in subsets)
                            latency = max((sample['ms'] for subset in subsets for sample in subset
                                           if sample['ok']), default=None)
                            evidence.append(named.evidence(target_id, ok=ok, attempts=config['attempts'],
                                                           latency_ms=latency))
                        from . import profiles
                        decision = profiles.evaluate(named, evidence)
                        row['profile_verdict'] = decision
                        row['min_target_reliability'] = 1.0 if decision['pass'] else 0.0
                        row['is_working'] = bool(decision['pass'])
                        return row
                    probe = named_probe
                scanner = self.scan or proxytool.scan
                kwargs = dict(profile_id=profile, profile_revision=revision,
                              collection_id=job.scope.collection_id, job_id=job.id, job_store=store,
                              # A submitted job freezes the items it promises
                              # to measure. Completed items are excluded by the
                              # engine's job scope when this same job resumes.
                              recheck=True,
                              min_success=0.0 if named is not None else 2 / 3,
                              protocol=filters.get('protocol', 'all'),
                              want=int(filters.get('want') or 0),
                              deadline_s=limits.get('max_seconds') or limits.get('timeout_s'),
                              max_requests=limits.get('max_requests'), max_bytes=limits.get('max_bytes'),
                              workers=int(limits.get('workers') or 128), probe=probe,
                              progress=False, run_state=state)
                asyncio.run(self._controlled(workbench, job, scanner(workbench.conn, config, **kwargs)))
                if state.get('store_failures'):
                    state.update(state='partial', stop_reason='E_DATA_WRITE')
                current = store.job(job.id)
                if current.state == 'running':
                    proxytool.export(workbench.conn, profile, self.data / 'exports',
                                     collection_id=job.scope.collection_id, profile_revision=revision,
                                     run_state=state, diagnostic=state.get('state') != 'complete',
                                     active_profile_path=self.data / 'last-profile.txt')
            current = store.job(job.id)
            if current.state == 'running':
                store.finish(job.id, state='partial' if state.get('state') == 'partial' else 'succeeded',
                             reason_code=state.get('stop_reason') or jobs.CODE_OK)
        except jobs.Busy:
            raise
        except Exception as exc:
            workbench.conn.rollback()
            code = getattr(exc, 'code', None) or jobs.CODE_INTERRUPTED
            current = store.job(job.id)
            if not current.terminal and current.state != 'paused':
                store.fail(job.id, reason_code=code, detail=type(exc).__name__)
            state['error'] = code
        state['seconds'] = time.monotonic() - started
        # A resumed job measures only its unfinished items. Persist cumulative
        # job cost so schedule accounting can add precisely the new work even
        # when this segment is smaller than the segment before the pause.
        for field in ('requests', 'bytes', 'seconds'):
            state[field] = state.get(field, 0) + previous_cost.get(field, 0)
        current = store.job(job.id)
        store.save_checkpoint(job.id, 'execution', state)
        self._record_schedule(workbench, current, state)
        return {'job_id': job.id, 'state': current.state, 'result': state}

    def _record_schedule(self, workbench, job, result):
        filters = job.scope.filters
        run_id, schedule_id = filters.get('schedule_run_id'), filters.get('schedule_id')
        if not run_id or not schedule_id:
            return
        store = workbench.schedules()
        record = workbench.conn.execute('SELECT counters_json FROM schedule_run WHERE id=?',
                                        (run_id,)).fetchone()
        if record is None:
            return
        previous = json.loads(record[0] or '{}')
        usage = previous.get('usage') or {}
        state = store.load_state(schedule_id)
        if state is not None:
            for field in ('requests', 'bytes', 'seconds'):
                delta = max(0, result.get(field, 0) - usage.get(field, 0))
                setattr(state.counters, field, getattr(state.counters, field) + delta)
            store.save_state(schedule_id, state)
        payload = {**previous, 'job_id': job.id, 'usage': {key: result.get(key, 0)
                                                         for key in ('requests', 'bytes', 'seconds')}}
        workbench.conn.execute('UPDATE schedule_run SET state=?, finished_at=?, counters_json=? WHERE id=?',
                               (job.state, workbench.clock() if job.terminal else None,
                                json.dumps(payload, sort_keys=True), run_id))
        workbench.conn.commit()


_RUNNERS = {}
_RUNNERS_LOCK = threading.Lock()


def runner_for(data, *, db_path=None):
    target = (Path(db_path) if db_path else Path(data) / db.DB_FILENAME).resolve()
    with _RUNNERS_LOCK:
        if target not in _RUNNERS:
            _RUNNERS[target] = JobRunner(data, db_path=target)
        return _RUNNERS[target]
