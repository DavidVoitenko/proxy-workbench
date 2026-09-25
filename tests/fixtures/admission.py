"""Shared admission fixture for every module's tests (CONTRACTS §2.3, HANDOFF §2.1).

Seventeen modules work against one admission contract, so they must work against
one fixture too. Import this instead of assembling an observation row by hand --
otherwise the modules disagree about field names, about reason codes and about
which cases exist at all.

    from tests.fixtures.admission import ADMISSION_CASES, admission_row, insert_rows

The fixture is deliberately free of imports from the package, so a module that only
tests a pure function can use it without touching SQLite. `insert_rows` imports
:mod:`proxy_workbench.db` lazily, and requires a database migrated by
``db.migrate()`` -- never a schema declared by the caller.

Time is fixed: every case is expressed as an offset from :data:`NOW`, so a test
never depends on the wall clock and every module ages the same rows the same way.

Reason codes
------------
Rejected rows carry the canonical codes of §5.4, domain ``TIME``. §2.4 writes the
same states without the ``E_`` prefix; §5.4 is declared the single canon ("`E_*`
стабильны и документированы; перевод существует отдельно от кода"), so the codes
here are the `E_*` ones and :data:`REASON_ALIASES` translates the §2.4 spelling.
Two literals are *not* error codes and are marked as such below: ``admitted`` for an
included row (no positive code exists in §5.4) and ``blocked`` for a policy denial
that happens before admission. Both are open questions for the integrator; see
``docs/integration/HANDOFF/db.md``.
"""
from __future__ import annotations

import hashlib
import json

#: Fixed reference time of the fixture (2027-01-15T08:00:00Z). Never use the clock.
NOW = 1_800_000_000.0
HOUR = 3600.0
DAY = 24 * HOUR

REASON_ADMITTED = "admitted"
REASON_BLOCKED = "blocked"

TIME_OK = "admitted"
TIME_UNKNOWN = "E_TIME_UNKNOWN"
TIME_FUTURE = "E_TIME_FUTURE"
CLOCK_ROLLBACK = "E_TIME_CLOCK_ROLLBACK"
TTL_EXPIRED = "E_TIME_TTL_EXPIRED"
#: Not an error code: the row is fine, the policy denied it before admission.
DENIED = "blocked"

#: §2.4 spelling -> §5.4 canon. One mapping, so no module invents its own.
REASON_ALIASES = {
    "time_ok": TIME_OK,
    "time_unknown": TIME_UNKNOWN,
    "time_future": TIME_FUTURE,
    "clock_rollback": CLOCK_ROLLBACK,
    "TTL_EXPIRED": TTL_EXPIRED,
}

#: Host that stands for a denylisted address. `.invalid` never resolves (RFC 2606),
#: so no test in the suite can reach the network through it.
BLOCKED_HOST = "denied.example.invalid"


def _case(name, canonical, *, checked_at, valid_until, admitted, reason, **extra):
    case = {
        "name": name,
        "canonical": canonical,
        "checked_at": NOW + checked_at if checked_at is not None else None,
        "valid_until": NOW + valid_until if valid_until is not None else None,
        "admitted": admitted,
        "reason_code": reason,
    }
    case.update(extra)
    return case


#: The shared case list: a mixed-age snapshot plus every explicit time state of §2.4.
#: `last_seen_now` is only set where the case needs the per-endpoint clock state.
ADMISSION_CASES = (
    _case("fresh_ok", "http://192.0.2.10:8080", checked_at=-60, valid_until=2 * HOUR,
          admitted=True, reason=TIME_OK),
    _case("still_valid", "https://198.51.100.20:3128", checked_at=-600, valid_until=60,
          admitted=True, reason=TIME_OK,
          note="expires in a minute: a mixed-age snapshot must not lose the fresh rows"),
    _case("expired_ttl", "socks5://203.0.113.30:1080", checked_at=-3 * HOUR, valid_until=-10,
          admitted=False, reason=TTL_EXPIRED),
    _case("unknown_time", "http://192.0.2.40:8080", checked_at=None, valid_until=None,
          admitted=False, reason=TIME_UNKNOWN,
          note="defect 1: a new row without a recorded time is not infinitely fresh"),
    _case("future_time", "socks5://198.51.100.50:1080", checked_at=DAY, valid_until=2 * DAY,
          admitted=False, reason=TIME_FUTURE),
    _case("clock_rollback", "http://203.0.113.60:8080", checked_at=-30, valid_until=2 * HOUR,
          admitted=False, reason=CLOCK_ROLLBACK, last_seen_now=NOW + HOUR,
          note="the endpoint was already seen with a later clock; a new measurement is required"),
    _case("blocked", f"http://{BLOCKED_HOST}:8080", checked_at=-60, valid_until=2 * HOUR,
          admitted=False, reason=DENIED, denylisted=True,
          note="denied by policy before any measurement"),
)

CASES_BY_NAME = {case["name"]: case for case in ADMISSION_CASES}


def admission_row(name, **overrides):
    """One fixture row, as the result/observation layer would hold it.

    The payload keeps the field names the current `summarize()` writes, so a module
    can compare against real rows instead of inventing a parallel vocabulary.
    """
    case = dict(CASES_BY_NAME[name])
    case.update(overrides)
    canonical = case["canonical"]
    scheme, _, rest = canonical.partition("://")
    host, _, port = rest.partition(":")
    payload = {
        "name": case["name"],
        "proxy": canonical,
        "scheme": scheme,
        "host": host,
        "port": int(port) if port.isdigit() else None,
        "country": "DE" if not case.get("denylisted") else None,
        "latency_ms": 120.0,
        "jitter_ms": 5.0,
        "reliability": 0.9,
        "min_target_reliability": 0.8,
        "successes": 3,
        "requests": 3,
        "checks": 3,
        "passes": 3,
        "score": 80.0,
        "checked_at": case["checked_at"],
        "valid_until": case["valid_until"],
        "error": None,
        "history": {"checks": 3, "passes": 3, "first_checked": case["checked_at"],
                    "last_ok": case["checked_at"]},
    }
    return {
        "case": case,
        "canonical": canonical,
        "endpoint_id": hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16],
        "profile_id": "fixture-profile",
        "profile_revision": 1,
        "access_id": "fixture-access",
        "access_revision": 1,
        "job_id": "fixture-job",
        "checked_at": case["checked_at"],
        "valid_until": case["valid_until"],
        "payload": payload,
        "admitted": case["admitted"],
        "reason_code": case["reason_code"],
        "last_seen_now": case.get("last_seen_now"),
        "denylisted": bool(case.get("denylisted")),
    }


def rows(*names):
    """Fixture rows by name; with no names, all of them in declaration order."""
    return [admission_row(name) for name in (names or [case["name"] for case in ADMISSION_CASES])]


#: The shared expectation of §2.3: exact equality of sorted `(canonical, reason)`
#: sets. Comparing these tuples is what "same set" means; nothing looser.
EXPECTED_ADMITTED = tuple(sorted(
    (case["canonical"], case["reason_code"])
    for case in ADMISSION_CASES if case["admitted"]))
EXPECTED_REJECTED = tuple(sorted(
    (case["canonical"], case["reason_code"])
    for case in ADMISSION_CASES if not case["admitted"]))
EXPECTED_ALL = tuple(sorted(
    (case["canonical"], case["reason_code"]) for case in ADMISSION_CASES))

#: A digest of the fixture itself, so a module can put it in its status or scope
#: digest and prove it tested against the same rows as everybody else (§4.2).
FIXTURE_DIGEST = hashlib.sha256(json.dumps(
    {"now": NOW, "cases": [(case["name"], case["canonical"], case["checked_at"],
                            case["valid_until"], case["admitted"], case["reason_code"])
                           for case in ADMISSION_CASES]},
    sort_keys=True).encode("utf-8")).hexdigest()


def insert_rows(conn, names=None, *, collection_id=None):
    """Write fixture rows into a database migrated by ``db.migrate()``.

    Columns are named explicitly, which is the only way the new code is allowed to
    write (§3.2). Returns the rows that were written.
    """
    from proxy_workbench import db

    written = rows(*(names or []))
    for row in written:
        db.upsert_endpoint(conn, row["canonical"])
        if collection_id:
            db.add_member(conn, collection_id, row["endpoint_id"], origin="import")
        conn.execute(
            "INSERT INTO accesses(id, endpoint_id, mode, access_revision, created_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (row["access_id"], row["endpoint_id"], "public", row["access_revision"], NOW))
        conn.execute(
            "INSERT INTO results(profile, proxy, payload, endpoint_id, access_id,"
            " access_revision, profile_id, profile_revision, job_id, checked_at, valid_until)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (row["profile_id"], row["canonical"], json.dumps(row["payload"], sort_keys=True),
             row["endpoint_id"], row["access_id"], row["access_revision"],
             row["profile_id"], row["profile_revision"], row["job_id"],
             row["checked_at"], row["valid_until"]))
    return written


if __name__ == '__main__':  # a quick self-check, not a test
    print("cases:", len(ADMISSION_CASES))
    print("admitted:", len(EXPECTED_ADMITTED), "rejected:", len(EXPECTED_REJECTED))
    print("digest:", FIXTURE_DIGEST)
    for case in ADMISSION_CASES:
        print(f"  {case['name']:<14} {case['canonical']:<40} {case['reason_code']}")
