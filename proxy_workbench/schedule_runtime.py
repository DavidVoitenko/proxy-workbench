"""Durable calendar dispatch shared by desktop and headless workers.

The job runner owns the timer. Desktop wake events call the same runtime; OS
locks serialize calendar decisions with execution and preserve spent budgets.
"""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
import sqlite3
import threading

from . import db, scheduler
from .maintenance import exclusive_lock


class ScheduleRuntime:
    def __init__(self, data, *, db_path=None):
        self.data = Path(data).resolve()
        self.db_path = Path(db_path).resolve() if db_path else self.data / db.DB_FILENAME
        self._lock = threading.Lock()
        self._wake = threading.Event()

    def tick(self, *, woke=False):
        """Submit due and manually requested runs, preserving wake while busy."""
        if woke:
            self._wake.set()
        if not self._lock.acquire(blocking=False):
            return {'busy': True, 'submitted': []}
        try:
            with contextlib.ExitStack() as stack:
                try:
                    stack.enter_context(exclusive_lock(self.data / 'schedule-runtime.lock'))
                    stack.enter_context(exclusive_lock(self.data / 'workbench.lock'))
                except RuntimeError:
                    return {'busy': True, 'submitted': []}
                woke = self._wake.is_set()
                self._wake.clear()
                try:
                    return self._tick(woke=woke)
                except Exception:
                    if woke:
                        self._wake.set()
                    raise
        finally:
            self._lock.release()

    def _tick(self, *, woke=False):
        # Fresh state observes edits made by another interface or process.
        # Connections never cross threads; requested rows survive a restart.
        from .jobrunner import submit_schedule
        connection, _ = db.open_db(self.db_path)
        try:
            engine = scheduler.Scheduler(scheduler.SqliteScheduleStore(connection))
            if woke:
                engine.mark_wake()
            report = engine.tick()
            requested = connection.execute(
                "SELECT id, schedule_id, started_at, counters_json FROM schedule_run "
                "WHERE state='requested' ORDER BY started_at, id LIMIT 64").fetchall()
            submitted, failed, skipped = [], [], []
            for run_id, schedule_id, started_at, encoded in requested:
                spec = engine.get(schedule_id)
                if spec is None:
                    connection.execute("UPDATE schedule_run SET state='cancelled', finished_at=? "
                                       "WHERE id=? AND state='requested'", (report.at, run_id))
                    connection.commit()
                    skipped.append(run_id)
                    continue
                # Requests left by an interrupted host obey the same
                # catch-up policy as the calendar. An old slot cannot run
                # after a newer slot has already replaced it.
                if woke or not spec.catch_up:
                    newer = connection.execute(
                        'SELECT id FROM schedule_run WHERE schedule_id=? AND started_at>? LIMIT 1',
                        (schedule_id, started_at)).fetchone()
                    if newer:
                        connection.execute("UPDATE schedule_run SET state='skipped', finished_at=?, reason=? "
                                           "WHERE id=? AND state='requested'",
                                           (report.at, scheduler.REASON_COALESCED, run_id))
                        connection.commit()
                        skipped.append(run_id)
                        continue
                if engine.blocked_by(schedule_id, at=report.at) is not None:
                    continue
                try:
                    payload = json.loads(encoded or '{}')
                    run = scheduler.RunRequest(
                        schedule_id=schedule_id, run_id=run_id,
                        reason=str(payload.get('reason') or scheduler.REASON_DUE),
                        scheduled_for=float(payload.get('scheduled_for', started_at)),
                        requested_at=float(payload.get('requested_at', started_at)),
                        missed=int(payload.get('missed') or 0), coalesced=bool(payload.get('coalesced')),
                        pool_id=payload.get('pool_id') or spec.pool_id,
                        stages=tuple(payload.get('stages') or scheduler.WORKBENCH_CLASSES),
                        budgets=scheduler.Budgets.from_dict(payload.get('budgets')),
                        action=payload.get('action') or spec.effective_action,
                        collection_id=payload.get('collection_id') or spec.collection_id)
                    job = submit_schedule(self.data, run, db_path=self.db_path)
                    payload['job_id'] = job.id
                    connection.execute("UPDATE schedule_run SET state=?, counters_json=? "
                                       "WHERE id=? AND state='requested'",
                                       (job.state, json.dumps(payload, sort_keys=True), run_id))
                    connection.commit()
                    submitted.append(job.id)
                except Exception as exc:
                    code = getattr(exc, 'code', '') or 'E_STATE_SCHEDULE_FAILED'
                    if code == 'E_CONFLICT_BUSY' or isinstance(exc, sqlite3.OperationalError):
                        continue
                    connection.execute("UPDATE schedule_run SET state='failed', finished_at=?, reason=? "
                                       "WHERE id=? AND state='requested'", (report.at, code, run_id))
                    connection.commit()
                    failed.append(dict(run_id=run_id, code=code))
            return dict(at=report.at, woke=bool(report.woke), schedules=len(report.decisions),
                        run_requests=len(report.run_requests), submitted=submitted,
                        failed=failed, skipped=skipped)
        finally:
            connection.close()

_RUNTIMES = {}
_RUNTIME_LOCK = threading.Lock()


def runtime_for(data, *, db_path=None):
    data = Path(data).resolve()
    database = Path(db_path).resolve() if db_path else data / db.DB_FILENAME
    with _RUNTIME_LOCK:
        key = str(database)
        if key not in _RUNTIMES:
            _RUNTIMES[key] = ScheduleRuntime(data, db_path=database)
        return _RUNTIMES[key]
