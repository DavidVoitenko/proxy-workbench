"""Shared fixtures for the engine tests: a measurement row needs its whole key.

The schema is versioned (CONTRACTS §3.3).  ``results`` is keyed by
``(profile_id, profile_revision, access_id, access_revision, endpoint_id, job_id)``
and ``candidates``/``candidate_seen`` carry a mandatory ``endpoint_id``, so the
old positional inserts this repository used everywhere no longer describe a row:
they are exactly the writes the newer schema has to refuse.

These helpers are what a test uses instead.  They go through ``db.upsert_endpoint``
and ``db.add_member``, the same two calls the engine makes, so a fixture can never
accidentally build a row the running program could not have written.
"""
from __future__ import annotations

import json
from itertools import islice
import sqlite3

from proxy_workbench import core, db
from proxy_workbench.proxytool import PUBLIC_ACCESS_ID

__all__ = ['add_candidate', 'add_candidates', 'mark_seen', 'store_candidate_seen',
           'store_profile', 'store_result', 'legacy_database']

INSERT_CANDIDATE = 'INSERT OR IGNORE INTO candidates(proxy, endpoint_id) VALUES (?, ?)'
INSERT_SEEN = 'INSERT OR IGNORE INTO candidate_seen(proxy, source, endpoint_id) VALUES (?, ?, ?)'


def _canonical(value):
    """Accept a proxy string or the one-element tuple the old fixtures passed."""
    if isinstance(value, (tuple, list)):
        if len(value) != 1:
            raise ValueError('expected a single address')
        return value[0]
    return value


def add_candidate(conn, values, *, collection_id=None, origin='public'):
    """Add one address to the public base, creating its endpoint and membership."""
    proxy = _canonical(values)
    endpoint = db.upsert_endpoint(conn, proxy)
    conn.execute(INSERT_CANDIDATE, (proxy, endpoint))
    db.add_member(conn, collection_id or db.PUBLIC_COLLECTION_ID, endpoint, origin=origin)
    return endpoint


def add_candidates(conn, rows, *, collection_id=None, origin='public'):
    """Add a streaming fixture in bounded transactions, keeping a caller's transaction."""
    rows = iter(rows)
    while batch := tuple(islice(rows, 500)):
        # The engine connection uses autocommit. Without a transaction, a large
        # fixture flushes three separate writes per address and spends minutes
        # preparing the test on Windows before the first probe can run.
        conn.execute('SAVEPOINT workbench_fixture_candidates')
        try:
            for row in batch:
                add_candidate(conn, row, collection_id=collection_id, origin=origin)
        except BaseException:
            conn.execute('ROLLBACK TO SAVEPOINT workbench_fixture_candidates')
            raise
        finally:
            conn.execute('RELEASE SAVEPOINT workbench_fixture_candidates')


def mark_seen(conn, values, source=None, *, collection_id=None):
    """Record that a source list offered this address, and add it to the scope."""
    if source is not None:
        proxy, source = values
    else:
        proxy, source = values
    endpoint = add_candidate(conn, proxy, collection_id=collection_id)
    conn.execute(INSERT_SEEN, (proxy, source, endpoint))
    # ``candidate_meta`` keeps the first source that delivered an address, and a
    # test that asserts on it writes the row itself.
    conn.execute('INSERT OR IGNORE INTO candidate_meta(proxy, country, source) VALUES (?, NULL, ?)',
                 (proxy, source))
    return endpoint


# The old call sites used this name for the two-value insert.
store_candidate_seen = mark_seen


def store_profile(conn, profile, config, **fields):
    """Register a check profile; the legacy ``profiles`` table gained columns."""
    columns = ['id', 'config', 'digest', 'created_at']
    values = [profile, config if isinstance(config, str) else json.dumps(config),
              fields.get('digest', profile), fields.get('created_at')]
    extra = [name for name in fields if name not in ('digest', 'created_at')]
    columns += extra
    values += [fields[name] for name in extra]
    conn.execute(f"INSERT OR IGNORE INTO profiles({', '.join(columns)}) "
                 f"VALUES ({', '.join('?' * len(columns))})", values)
    return profile


def store_result(conn, values, *, profile_id=None, profile_revision=1,
                 access_id=PUBLIC_ACCESS_ID, access_revision=1, job_id='',
                 collection_id=None, valid_until=None, checked_at=None,
                 max_age_seconds=None, network_id='default', **extra):
    """Write one measurement row with the key the migrated schema requires.

    The payload is a JSON string, a mapping, or ``None``.  The scope and the
    lifetime are folded into the payload exactly as the engine's ``store()``
    writes them, so a fixture can never produce a row the running program could
    not: a positive ``checked_at`` gets a ``valid_until`` derived from the same
    policy, and the collection is the public base unless the test names another.
    Pass ``valid_until=`` (or a non-positive ``checked_at``) to write a row that
    is deliberately stale, unknown or expired.
    """
    profile, proxy, payload = values
    body = json.loads(payload) if isinstance(payload, str) else dict(payload or {})
    body.setdefault('proxy', proxy)
    collection_id = collection_id or db.PUBLIC_COLLECTION_ID
    body['collection_id'] = collection_id
    # The scope is part of the row, not of the read: a row without it cannot be
    # admitted against any scope (CONTRACTS §1.2 rules 1-2).
    body['profile_id'] = profile_id or profile
    body['profile_revision'] = int(profile_revision)
    body['access_id'] = access_id
    body['access_revision'] = int(access_revision)
    body.setdefault('network_id', network_id)
    if checked_at is None:
        checked_at = body.get('checked_at')
    if valid_until is None:
        # A lifetime already recorded in the payload is the measurement of
        # record: the fixture must not invent a fresher one.
        valid_until = body.get('valid_until')
    known = isinstance(checked_at, (int, float)) and not isinstance(checked_at, bool) and checked_at > 0
    if valid_until is None and known:
        age = max_age_seconds if max_age_seconds is not None else core.DEFAULT_MAX_AGE_SECONDS
        valid_until = checked_at + age
    if valid_until is not None:
        body['valid_until'] = valid_until
    if checked_at is not None:
        body['checked_at'] = checked_at
    endpoint = db.upsert_endpoint(conn, proxy)
    conn.execute('''INSERT OR REPLACE INTO results(
        profile, proxy, payload, endpoint_id, access_id, access_revision,
        profile_id, profile_revision, checked_at, valid_until, error_code, error_stage, job_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                 (profile, proxy, json.dumps(body, ensure_ascii=False), endpoint,
                  access_id, int(access_revision), profile_id or profile,
                  int(profile_revision), checked_at, valid_until,
                  body.get('error_code'), body.get('error_stage'), job_id or ''))
    return body


LEGACY_DDL = '''
    CREATE TABLE IF NOT EXISTS candidates(proxy TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS candidate_meta(proxy TEXT PRIMARY KEY, country TEXT);
    CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, config TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS candidate_seen(proxy TEXT NOT NULL, source TEXT NOT NULL,
        PRIMARY KEY(proxy, source)) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS results(
        profile TEXT NOT NULL, proxy TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(profile, proxy));
'''


def legacy_database(path):
    """A pre-versioning database, exactly as the 2.x engine created it.

    Schema tests need one: the migrator has to recognise this file by its
    header, and the write guard has to refuse the positional inserts that work
    here and only here.
    """
    from pathlib import Path
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_DDL)
    if 'source' not in {row[1] for row in conn.execute('PRAGMA table_info(candidate_meta)')}:
        # The one migration the unversioned engine ever had, in 1.6.
        conn.execute('ALTER TABLE candidate_meta ADD COLUMN source TEXT')
    conn.commit()
    return conn
