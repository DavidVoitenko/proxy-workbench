"""Versioned SQLite schema, collections, pre-migration backup, restore and retention.

This module is the only place in the package that writes DDL. Every other module
calls :func:`migrate` (or :func:`open_db`) in its own tests and works with the
tables the migrations created -- nobody declares a schema in prose
(``docs/integration/CONTRACTS.ru.md`` §3.2, §3.3).

Public API
----------
versioning
    :data:`SCHEMA_VERSION`, :data:`APPLICATION_ID`, :func:`probe`, :func:`migrate`,
    :func:`open_db`, :func:`current_version`, :func:`assert_current`
introspection
    :func:`describe`, :func:`tables`, :func:`columns`, :func:`indexes`
endpoints
    :func:`endpoint_id`, :func:`upsert_endpoint`
collections
    :func:`create_collection`, :func:`rename_collection`, :func:`archive_collection`,
    :func:`get_collection`, :func:`list_collections`, :func:`add_member`,
    :func:`remove_member`, :func:`collection_members`, :func:`endpoint_collections`,
    :func:`legacy_summary`
scope exclusions
    :func:`add_scope_exclusion`, :func:`scope_exclusions`, :func:`excluded_addresses`,
    :func:`clear_scope_exclusions`
backup
    :func:`create_backup`, :func:`verify_backup`, :func:`list_backups`, :func:`manifest_path`
restore
    :func:`restore_preview`, :func:`restore`, :func:`rollback`, :func:`migrate_data_path`
retention and cleanup
    :func:`retention_preview`, :func:`apply_retention`, :func:`cleanup_preview`, :func:`cleanup`
secrets
    :func:`secret_bindings`, :func:`rebind_secrets`

Two rules are enforced here rather than documented and hoped for:

* **Credentials never reach this module.** ``accesses.secret_ref`` holds an opaque
  reference that ``secrets.py`` resolves; :func:`rebind_secrets` refuses values that
  look like a userinfo, so no backup manifest, preview or report can carry a secret
  value (F24, §5.1).
* **Refusals write nothing.** A database newer than this program, a foreign
  ``application_id`` or a corrupt file is rejected before the first ``CREATE``,
  ``ALTER`` or ``INSERT``, including the ``user_version`` write itself (§3.5.5).

Endpoint identity: :func:`endpoint_id` is a pure function of the canonical address
string, so the same address collected from a list and measured by a scan lands on one
``endpoints`` row without a second normalizer. Callers pass a canonical string
(``scheme://host:port``, lowercase host, no userinfo); the UNIQUE constraint on
``endpoints.canonical`` is the last line of defence.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import time
from typing import Callable

__all__ = [
    "SCHEMA_VERSION", "APPLICATION_ID", "DB_FILENAME", "BACKUP_DIRNAME",
    "PUBLIC_COLLECTION_ID", "LEGACY_COLLECTION_ID", "PUBLIC_COLLECTION_NAME",
    "LEGACY_COLLECTION_NAME", "COLLECTION_KINDS", "MIGRATIONS", "SOURCE_TABLES_DDL",
    "SOURCE_INDEXES", "SCHEDULE_RUNTIME_COLUMNS", "SCHEDULE_RUN_COLUMNS",
    "SCOPE_EXCLUSION_DDL", "DEFAULT_SCOPE",
    "DbError", "DbVersionError", "DbForeignError", "DbCorruptError", "BackupError",
    "RetentionError", "Migration", "MigrationReport", "BackupManifest", "RestorePreview",
    "RestoreReport", "RetentionPolicy", "RetentionPreview", "RetentionReport",
    "CleanupPreview", "CleanupReport", "SecretBinding", "RebindReport",
    "connect", "probe", "read_header", "migrate", "open_db", "current_version",
    "assert_current", "describe", "tables", "columns", "indexes", "primary_key",
    "database_bytes", "table_bytes", "endpoint_id", "upsert_endpoint",
    "create_collection", "rename_collection", "archive_collection", "get_collection",
    "list_collections", "add_member", "remove_member", "collection_members",
    "endpoint_collections", "legacy_summary", "add_scope_exclusion", "scope_exclusions",
    "excluded_addresses", "clear_scope_exclusions", "create_backup", "verify_backup",
    "list_backups", "manifest_path", "restore_preview", "restore", "rollback",
    "migrate_data_path", "retention_preview", "apply_retention", "cleanup_preview",
    "cleanup", "secret_bindings", "rebind_secrets", "write_json", "read_json",
    "sha256_file",
]

# ---------------------------------------------------------------------------
# version identity
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 19
#: Magic number of this package. 0 means "no application id yet" (pre-versioning file).
APPLICATION_ID = 0x50574231
DB_FILENAME = "proxies.sqlite3"
BACKUP_DIRNAME = "backups"
MANIFEST_SUFFIX = ".manifest.json"
MANIFEST_SCHEMA = 1

PUBLIC_COLLECTION_ID = "public-base"
LEGACY_COLLECTION_ID = "legacy-collected"
#: The credential-free access identity.  A measurement made without a credential is
#: attributed to it, and a legacy file that has no `accesses` rows at all measured
#: nothing else.  `proxytool.PUBLIC_ACCESS_ID` names the same identity.
PUBLIC_ACCESS_ID = "default"
PUBLIC_COLLECTION_NAME = "Публичная база"
LEGACY_COLLECTION_NAME = "Ранее собранные"
COLLECTION_KINDS = ("public", "private")
COLLECTION_ORIGINS = ("legacy", "public", "manual", "import", "gateway")

# Error codes from CONTRACTS §5.4 (domains DATA and VALIDATION).
E_VERSION_AHEAD = "E_DATA_DB_VERSION_AHEAD"
E_FOREIGN_DB = "E_DATA_DB_FOREIGN"
E_MIGRATION_FAILED = "E_DATA_MIGRATION_FAILED"
E_BACKUP_FAILED = "E_DATA_BACKUP_FAILED"
E_DB_CORRUPT = "E_DATA_DB_CORRUPT"
E_PATH_CONFLICT = "E_VALIDATION_FIELD"

_BATCH = 500


class DbError(Exception):
    """Base class for every refusal this module makes. Always carries a code."""

    def __init__(self, code, message, *, path=None):
        super().__init__(f"{code}: {message}" if not path else f"{code}: {message} ({path})")
        self.code = code
        self.message = message
        self.path = path


class DbVersionError(DbError):
    """The file was written by a newer program: refuse, do not downgrade it."""


class DbForeignError(DbError):
    """`application_id` belongs to another application: this is not our database."""


class DbCorruptError(DbError):
    """The file exists but is not a readable SQLite database; never recreate it."""


class BackupError(DbError):
    """A backup, a manifest or a checksum could not be produced or verified."""


class RetentionError(DbError):
    """A retention or cleanup request is not executable as described."""


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationReport:
    """What :func:`migrate` did, in the order it did it."""

    path: str
    status: str                          # 'created' | 'migrated' | 'current'
    previous_version: int
    schema_version: int
    application_id: int
    applied: tuple = ()                  # ((version, name), ...)
    backup: "BackupManifest | None" = None
    legacy_candidates: int = 0           # candidates moved to endpoints + legacy collection

    @property
    def changed(self):
        return bool(self.applied)

    def to_dict(self):
        return {
            "path": self.path,
            "status": self.status,
            "previous_version": self.previous_version,
            "schema_version": self.schema_version,
            "application_id": self.application_id,
            "applied": [list(item) for item in self.applied],
            "backup": self.backup.to_dict() if self.backup else None,
            "legacy_candidates": self.legacy_candidates,
        }


@dataclass(frozen=True)
class BackupManifest:
    """A technical backup: the file, its checksum and enough context to explain it."""

    path: str
    sha256: str
    bytes: int
    created_at: float
    schema_version: int
    application_id: int
    source_path: str
    reason: str
    tool: str
    manifest_schema: int = MANIFEST_SCHEMA

    def to_dict(self):
        return {
            "manifest_schema": self.manifest_schema,
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
            "application_id": self.application_id,
            "source_path": self.source_path,
            "reason": self.reason,
            "tool": self.tool,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            path=data["path"], sha256=data["sha256"], bytes=int(data["bytes"]),
            created_at=float(data["created_at"]), schema_version=int(data["schema_version"]),
            application_id=int(data["application_id"]), source_path=data["source_path"],
            reason=data["reason"], tool=data["tool"],
            manifest_schema=int(data.get("manifest_schema", MANIFEST_SCHEMA)),
        )

    def write(self):
        """Write ``<backup>.manifest.json`` next to the backup and return its path."""
        target = manifest_path(self.path)
        write_json(target, self.to_dict())
        return str(target)


@dataclass(frozen=True)
class SecretBinding:
    """An access identity pointing at a vault entry. The reference is not a secret."""

    access_id: str
    endpoint_id: str
    mode: str
    secret_ref: str

    def to_dict(self):
        return {"access_id": self.access_id, "endpoint_id": self.endpoint_id,
                "mode": self.mode, "secret_ref": self.secret_ref}


@dataclass(frozen=True)
class RestorePreview:
    """What a restore would put where, without having written anything."""

    source_path: str
    target_path: str
    files: tuple = ()                  # ((name, bytes, sha256), ...)
    secrets: tuple = ()                # (SecretBinding, ...)
    conflicts: tuple = ()              # names that already exist in the target folder
    total_bytes: int = 0
    reason: str = "restore"
    applied: bool = False

    @property
    def ok(self):
        return not self.conflicts

    def to_dict(self):
        return {
            "source_path": self.source_path,
            "target_path": self.target_path,
            "files": [list(item) for item in self.files],
            "secrets": [item.to_dict() for item in self.secrets],
            "conflicts": list(self.conflicts),
            "total_bytes": self.total_bytes,
            "reason": self.reason,
            "applied": self.applied,
            "ok": self.ok,
        }


@dataclass(frozen=True)
class RestoreReport:
    """Result of a restore: where the copy landed and what was verified."""

    preview: RestorePreview
    manifest: "BackupManifest | None" = None
    verified: bool = False
    note: str = ""

    def to_dict(self):
        return {
            "preview": self.preview.to_dict(),
            "manifest": self.manifest.to_dict() if self.manifest else None,
            "verified": self.verified,
            "note": self.note,
        }


@dataclass(frozen=True)
class RetentionPolicy:
    """Age policy for stored measurements. Defaults are starting values, not proven optima."""

    max_age_seconds: float = None       # None => age is not used as a criterion
    expired_only: bool = True           # results: only rows whose valid_until has passed
    include: tuple = ("observations", "results")
    keep_newest: int = 0                # keep the N newest rows; alone it is a full policy

    def __post_init__(self):
        unknown = [name for name in self.include if name not in RETENTION_TIME_COLUMN]
        if unknown:
            raise RetentionError(E_PATH_CONFLICT, f"unknown retention target(s): {unknown}")
        if self.max_age_seconds is not None and self.max_age_seconds < 0:
            raise RetentionError(E_PATH_CONFLICT, "max_age_seconds must not be negative")
        if self.keep_newest < 0:
            raise RetentionError(E_PATH_CONFLICT, "keep_newest must not be negative")


#: Time column each retention target is aged by (CONTRACTS §3.6).
#: `observations` has no `checked_at` column -- migration 4 defines `started_at`/`finished_at`.
RETENTION_TIME_COLUMN = {"observations": "finished_at", "results": "checked_at"}


@dataclass(frozen=True)
class RetentionPreview:
    """What retention would delete, and how much data that is."""

    policy: RetentionPolicy
    now: float
    targets: tuple = ()                 # ((table, time_column, rows, oldest, newest), ...)
    total_rows: int = 0
    database_bytes: int = 0
    target_bytes: int = 0
    blocked: tuple = ()                 # ((table, (blocker, ...), rows), ...)

    def rows_for(self, table):
        """The rows retention can actually remove. Blocked rows are not counted."""
        for name, _column, rows, _oldest, _newest in self.targets:
            if name == table:
                return rows
        return 0

    def blocked_by(self, table):
        """The referrers holding the rows of ``table`` back, or an empty tuple."""
        for name, blockers, _rows in self.blocked:
            if name == table:
                return tuple(blockers)
        return ()

    def blocked_rows(self, table=None):
        """Rows the policy matches that a referrer keeps alive."""
        if table is None:
            return sum(rows for _name, _blockers, rows in self.blocked)
        for name, _blockers, rows in self.blocked:
            if name == table:
                return rows
        return 0

    def runnable(self):
        """True when every matched row can actually be removed."""
        return not self.blocked

    def to_dict(self):
        return {
            "now": self.now,
            "max_age_seconds": self.policy.max_age_seconds,
            "expired_only": self.policy.expired_only,
            "keep_newest": self.policy.keep_newest,
            "total_rows": self.total_rows,
            "blocked_rows": self.blocked_rows(),
            "database_bytes": self.database_bytes,
            "target_bytes": self.target_bytes,
            "runnable": self.runnable(),
            "blocked": [{"table": name, "by": list(blockers), "rows": rows}
                        for name, blockers, rows in self.blocked],
            "targets": [
                {"table": name, "time_column": column, "rows": rows,
                 "oldest": oldest, "newest": newest}
                for name, column, rows, oldest, newest in self.targets
            ],
        }


@dataclass(frozen=True)
class RetentionReport:
    deleted: tuple = ()                 # ((table, rows), ...)
    freed_bytes: int = 0
    database_bytes_before: int = 0
    database_bytes_after: int = 0
    vacuumed: bool = False
    preview: "RetentionPreview | None" = None

    def to_dict(self):
        return {
            "deleted": {name: rows for name, rows in self.deleted},
            "freed_bytes": self.freed_bytes,
            "database_bytes_before": self.database_bytes_before,
            "database_bytes_after": self.database_bytes_after,
            "vacuumed": self.vacuumed,
        }


@dataclass(frozen=True)
class CleanupPreview:
    """What a data-folder cleanup would remove, and what it refuses to touch."""

    data_path: str
    remove: tuple = ()                  # ((name, bytes), ...)
    protected: tuple = ()               # ((name, bytes, reason), ...)
    total_bytes: int = 0
    applied: bool = False

    def to_dict(self):
        return {
            "data_path": self.data_path,
            "remove": [{"name": name, "bytes": size} for name, size in self.remove],
            "protected": [{"name": name, "bytes": size, "reason": reason}
                          for name, size, reason in self.protected],
            "total_bytes": self.total_bytes,
            "applied": self.applied,
        }


@dataclass(frozen=True)
class CleanupReport:
    preview: CleanupPreview
    removed: tuple = ()
    failed: tuple = ()

    def to_dict(self):
        return {
            "preview": self.preview.to_dict(),
            "removed": list(self.removed),
            "failed": [{"name": name, "error": error} for name, error in self.failed],
        }


@dataclass(frozen=True)
class RebindReport:
    """Result of remapping secret references. Values are references, never secrets."""

    dry_run: bool
    changed: tuple = ()                 # ((access_id, old_ref, new_ref), ...)
    unresolved: tuple = ()              # refs offered for remapping that no access uses
    skipped: tuple = ()                 # ((access_id, reason), ...)
    revisions: tuple = ()               # ((access_id, from_revision, to_revision, at), ...)

    def to_dict(self):
        return {
            "dry_run": self.dry_run,
            "changed": [{"access_id": a, "from": o, "to": n} for a, o, n in self.changed],
            "unresolved": list(self.unresolved),
            "skipped": [{"access_id": a, "reason": r} for a, r in self.skipped],
            "revisions": [{"access_id": a, "from": old, "to": new, "rotated_at": at}
                          for a, old, new, at in self.revisions],
        }


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _now(now):
    return time.time() if now is None else float(now)


def write_json(path, payload):
    """Write JSON atomically, so an interrupted run leaves no half-written manifest."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)
    return str(path)


def read_json(path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def sha256_file(path):
    """Chunked digest: backups are hashed by the megabyte, never slurped into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def endpoint_id(canonical):
    """Stable id of a canonical address: the same string always gives the same id."""
    return hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()[:16]


def _ident(name):
    """Quote an identifier. Names come from internal constants; the check stays."""
    if not str(name).replace("_", "").isalnum():
        raise DbError(E_PATH_CONFLICT, f"unsafe identifier: {name!r}")
    return '"' + str(name) + '"'


def _sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def columns(conn, table):
    quoted = _ident(table)
    cache = getattr(conn, '_column_cache', None)
    if cache is None:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({quoted})")]
    # SQLite's schema cookie also sees ALTER/DROP from other connections and
    # transactional rollbacks. Never keep columns based only on a file name.
    version = conn.execute('PRAGMA schema_version').fetchone()[0]
    if version != conn._column_cache_version:
        cache.clear()
        conn._column_cache_version = version
    if table not in cache:
        cache[table] = tuple(row[1] for row in conn.execute(f"PRAGMA table_info({quoted})"))
    return list(cache[table])


def primary_key(conn, table):
    rows = sorted(conn.execute(f"PRAGMA table_info({_ident(table)})"), key=lambda row: row[5])
    return [row[1] for row in rows if row[5]]


def tables(conn):
    return [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def indexes(conn):
    return [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def database_bytes(path):
    """Size of a database file including its WAL sidecars; a missing file is 0 bytes."""
    path = Path(path) if str(path) else None
    if path is None or not path.name:
        return 0
    total = path.stat().st_size if path.is_file() else 0
    for suffix in ("-wal", "-shm"):
        extra = path.with_name(path.name + suffix)
        if extra.is_file():
            total += extra.stat().st_size
    return total


def _database_file(conn):
    """Path of the main database behind a connection; '' for an in-memory one.

    `Connection.database` is not part of the documented API any more, so the path is
    read from the pragma instead of guessed.
    """
    row = conn.execute("PRAGMA database_list").fetchone()
    return row[2] if row and row[2] else ""


def table_bytes(conn, table):
    """Pages used by one table, or None when this build has no dbstat virtual table.

    `dbstat.name` holds a plain name, so the table is passed unquoted: a double
    quoted "name" would be read as an identifier and match nothing.
    """
    try:
        row = conn.execute("SELECT sum(pgsize) FROM dbstat WHERE name = ?", (str(table),)).fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row and row[0] is not None else 0


def _add_column(conn, table, column, declaration):
    """Idempotent ADD COLUMN: a column that is already there is a no-op."""
    if column in columns(conn, table):
        return False
    conn.execute(f"ALTER TABLE {_ident(table)} ADD COLUMN {_ident(column)} {declaration}")
    return True


def describe(conn):
    """Schema snapshot: every table with its columns, key and indexes."""
    return {name: {"columns": columns(conn, name),
                   "primary_key": primary_key(conn, name),
                   "indexes": [row[0] for row in conn.execute(f"PRAGMA index_list({_ident(name)})")]}
            for name in tables(conn)}


# ---------------------------------------------------------------------------
# migrations 0..18
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection, _MigrationContext], None]


def _m0(conn, context):
    """application_id, the migration journal, and the pre-versioning tables.

    The five tables at the bottom are what the unversioned ``open_db`` created
    (CONTRACTS §3.1). ``IF NOT EXISTS`` gives a fresh database the same starting
    point as a migrated legacy one, and migration 5 then breaks the positional
    writes into three of them (§3.5.2).
    """
    conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
    # Idempotent on purpose: :func:`connect` already registers it for every
    # connection, and this line only matters for a caller that built its own
    # sqlite3 connection and applies migrations by hand.  Migration 13 backfills
    # `results.endpoint_id` with one set-based statement instead of a row loop.
    conn.create_function("endpoint_id", 1, endpoint_id, deterministic=True)
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations(
        version INTEGER PRIMARY KEY, applied_at REAL, app_version TEXT, backup_path TEXT)""")
    conn.execute("CREATE TABLE IF NOT EXISTS candidates(proxy TEXT PRIMARY KEY)")
    conn.execute("""CREATE TABLE IF NOT EXISTS candidate_meta(
        proxy TEXT PRIMARY KEY, country TEXT, source TEXT)""")
    conn.execute("CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, config TEXT NOT NULL)")
    conn.execute("""CREATE TABLE IF NOT EXISTS candidate_seen(
        proxy TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY(proxy, source)) WITHOUT ROWID""")
    conn.execute("""CREATE TABLE IF NOT EXISTS results(
        profile TEXT NOT NULL, proxy TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(profile, proxy))""")


def _m1(conn, context):
    conn.execute("""CREATE TABLE IF NOT EXISTS endpoints(
        id TEXT PRIMARY KEY,
        canonical TEXT UNIQUE NOT NULL,
        host TEXT,
        port INTEGER,
        scheme TEXT,
        ip_version INTEGER,
        country TEXT,
        country_source TEXT,
        country_at REAL,
        asn INTEGER,
        provider TEXT,
        hosting INTEGER,
        cidr TEXT,
        first_seen_at REAL,
        last_seen_at REAL)""")


def _default_collections(conn, now):
    """The public base and personal collections are different rows, not one list.

    The legacy collection keeps the honest name from §3.4 -- "Ранее собранные" -- and
    is never merged into a personal collection, so a private list never starts out
    holding addresses that were collected from public sources (F02, дефект 11).
    """
    conn.executemany(
        "INSERT OR IGNORE INTO collections(id, name, kind, created_at) VALUES (?,?,?,?)",
        [(PUBLIC_COLLECTION_ID, PUBLIC_COLLECTION_NAME, "public", now),
         (LEGACY_COLLECTION_ID, LEGACY_COLLECTION_NAME, "public", now)])


def _import_legacy_candidates(conn, now):
    """Move legacy candidates into `endpoints` and the legacy public collection.

    Idempotent: both target tables have UNIQUE/PK keys, so a second run inserts
    nothing. No personal collection is touched and no name or origin is invented.
    """
    if not _table_exists(conn, "candidates"):
        return 0
    cursor = conn.execute(
        "SELECT c.proxy AS proxy, m.country AS country FROM candidates c"
        " LEFT JOIN candidate_meta m ON m.proxy = c.proxy")
    moved = 0
    while True:
        batch = cursor.fetchmany(_BATCH)
        if not batch:
            break
        conn.executemany(
            "INSERT OR IGNORE INTO endpoints(id, canonical, first_seen_at, last_seen_at, country, country_at)"
            " VALUES (?,?,?,?,?,?)",
            [(endpoint_id(row["proxy"]), row["proxy"], now, now, row["country"],
              now if row["country"] else None) for row in batch])
        conn.executemany(
            "INSERT OR IGNORE INTO membership(collection_id, endpoint_id, added_at, origin)"
            " VALUES (?,?,?,'legacy')",
            [(LEGACY_COLLECTION_ID, endpoint_id(row["proxy"]), now) for row in batch])
        moved += len(batch)
    return moved


def _m2(conn, context):
    conn.execute("""CREATE TABLE IF NOT EXISTS collections(
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        archived_at REAL,
        created_at REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS membership(
        collection_id TEXT REFERENCES collections(id),
        endpoint_id TEXT REFERENCES endpoints(id),
        added_at REAL,
        origin TEXT NOT NULL,
        PRIMARY KEY(collection_id, endpoint_id))""")
    _default_collections(conn, context.now)
    context.legacy_candidates = _import_legacy_candidates(conn, context.now)


def _m3(conn, context):
    # `secret_ref` is a reference resolved by secrets.py; no credential value is stored.
    conn.execute("""CREATE TABLE IF NOT EXISTS accesses(
        id TEXT PRIMARY KEY,
        endpoint_id TEXT NOT NULL REFERENCES endpoints(id),
        mode TEXT NOT NULL,
        secret_ref TEXT,
        access_revision INTEGER NOT NULL,
        created_at REAL,
        rotated_at REAL)""")


def _m4(conn, context):
    conn.execute("""CREATE TABLE IF NOT EXISTS observations(
        id TEXT PRIMARY KEY,
        job_id TEXT,
        endpoint_id TEXT NOT NULL REFERENCES endpoints(id),
        access_id TEXT NOT NULL REFERENCES accesses(id),
        access_revision INTEGER NOT NULL,
        profile_id TEXT NOT NULL,
        profile_revision INTEGER NOT NULL,
        started_at REAL,
        finished_at REAL,
        verdict TEXT NOT NULL,
        error_code TEXT,
        error_stage TEXT)""")


#: Columns migration 5 adds to `results`. profile_id/profile_revision are declared here
#: and nowhere else (§3.3).
RESULTS_COLUMNS = (
    ("observation_id", "TEXT REFERENCES observations(id)"),
    ("endpoint_id", "TEXT REFERENCES endpoints(id)"),
    ("access_id", "TEXT"),
    ("access_revision", "INTEGER"),
    ("profile_id", "TEXT"),
    ("profile_revision", "INTEGER"),
    ("checked_at", "REAL"),
    ("valid_until", "REAL"),
    ("error_code", "TEXT"),
    ("error_stage", "TEXT"),
    ("job_id", "TEXT"),
)
#: These NOT NULL DEFAULT columns exist so that the old positional INSERT breaks
#: immediately and without writing: `table candidates has 2 columns but 1 values
#: was supplied` (§3.5.2).
WRITE_GUARD_COLUMNS = (("candidates", "endpoint_id"), ("candidate_seen", "endpoint_id"))


def _m5(conn, context):
    for column, declaration in RESULTS_COLUMNS:
        _add_column(conn, "results", column, declaration)
    for table, column in WRITE_GUARD_COLUMNS:
        _add_column(conn, table, column, "TEXT NOT NULL DEFAULT ''")
        # The guard column carries a real value instead of staying a dead default:
        # migration 2 already created the matching endpoint for every candidate.
        conn.execute(
            f"UPDATE {_ident(table)} SET {_ident(column)} = COALESCE("
            f"(SELECT e.id FROM endpoints e WHERE e.canonical = {_ident(table)}.proxy), '')"
            f" WHERE {_ident(column)} = ''")


def _m6(conn, context):
    conn.execute("""CREATE TABLE IF NOT EXISTS job(
        id TEXT PRIMARY KEY, kind TEXT, state TEXT, scope_json TEXT, input_digest TEXT,
        profile_id TEXT, profile_revision INTEGER, collection_id TEXT, idempotency_key TEXT,
        created_at REAL, started_at REAL, finished_at REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS job_item(
        job_id TEXT NOT NULL REFERENCES job(id), item_id TEXT NOT NULL, endpoint_id TEXT,
        access_id TEXT, access_revision INTEGER, state TEXT, observation_id TEXT, error_code TEXT,
        PRIMARY KEY(job_id, item_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS job_event(
        job_id TEXT, seq INTEGER, at REAL, type TEXT, code TEXT, data_json TEXT,
        PRIMARY KEY(job_id, seq))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS checkpoint(
        job_id TEXT, name TEXT, at REAL, state_json TEXT, PRIMARY KEY(job_id, name))""")


def _m7(conn, context):
    conn.execute("""CREATE TABLE IF NOT EXISTS pools(
        id TEXT PRIMARY KEY, collection_id TEXT, profile_id TEXT, profile_revision INTEGER,
        policy_json TEXT, desired INTEGER, minimum INTEGER, reserve INTEGER, state TEXT,
        deficit_reason TEXT, next_attempt_at REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS pool_member(
        pool_id TEXT, endpoint_id TEXT, state TEXT, admitted_at REAL, released_at REAL,
        PRIMARY KEY(pool_id, endpoint_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS schedules(
        id TEXT PRIMARY KEY, pool_id TEXT, kind TEXT, interval_minutes REAL, window_json TEXT,
        timezone TEXT, quiet_hours_json TEXT, budgets_json TEXT, next_run_at REAL, enabled INTEGER)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS schedule_run(
        id TEXT PRIMARY KEY, schedule_id TEXT, started_at REAL, finished_at REAL, state TEXT,
        counters_json TEXT)""")


def _m8(conn, context):
    # verifier/verifier_salt/verifier_algo hold a one-way verifier, never the key.
    conn.execute("""CREATE TABLE IF NOT EXISTS api_keys(
        id TEXT PRIMARY KEY, prefix TEXT NOT NULL, name TEXT, purpose TEXT, created_at REAL,
        expires_at REAL, last_used_at REAL, revoked_at REAL, permissions_json TEXT NOT NULL,
        resource_scope_json TEXT NOT NULL, rate_limit_json TEXT, concurrency_json TEXT,
        rotation_grace_until REAL, verifier TEXT NOT NULL, verifier_salt TEXT NOT NULL,
        verifier_algo TEXT NOT NULL)""")


def _m9(conn, context):
    # Profiles stayed content-addressed, so `id` is the digest of the config and the
    # backfill is a fact rather than an invention. The name stays NULL: a legacy
    # profile has no name and F02 forbids making one up.
    for column, declaration in (
        ("name", "TEXT"),
        ("revision", "INTEGER NOT NULL DEFAULT 1"),
        ("parent_id", "TEXT"),
        ("digest", "TEXT NOT NULL DEFAULT ''"),
        ("created_at", "REAL"),
        ("archived_at", "REAL"),
        ("is_default", "INTEGER NOT NULL DEFAULT 0"),
    ):
        _add_column(conn, "profiles", column, declaration)
    conn.execute("UPDATE profiles SET digest = id WHERE digest = ''")


def _m10(conn, context):
    conn.execute("""CREATE TABLE IF NOT EXISTS audit_log(
        at REAL NOT NULL, key_id TEXT, operation TEXT NOT NULL, object_kind TEXT, object_id TEXT,
        scope_json TEXT, result TEXT, error_code TEXT)""")


def _m11(conn, context):
    conn.execute("""CREATE TABLE IF NOT EXISTS export_artifact(
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, collection_id TEXT, profile_id TEXT,
        profile_revision INTEGER, generation TEXT, published_at REAL, expires_at REAL,
        state TEXT, reason_code TEXT, manifest_json TEXT)""")


def _m12(conn, context):
    conn.execute("""CREATE TABLE IF NOT EXISTS import_batch(
        id TEXT PRIMARY KEY, collection_id TEXT, created_at REAL, state TEXT, report_json TEXT,
        revision INTEGER)""")


#: The `results` columns, in the order the rebuild statements use them.
RESULTS_NEW_COLUMNS = (
    "profile", "proxy", "payload", "observation_id", "endpoint_id", "access_id",
    "access_revision", "profile_id", "profile_revision", "checked_at", "valid_until",
    "error_code", "error_stage", "job_id",
)
#: The key migration 13 built.  Kept verbatim: it is the key an existing 13/14
#: database carries, and migration 15 reads it to decide whether it still has to work.
RESULTS_NEW_KEY = ("profile_id", "profile_revision", "access_id", "access_revision",
                   "endpoint_id", "job_id")
#: The key of one measurement of record.  A row is the address, not the run: two
#: access revisions of one address are two rows (F04), a repeat check in a new job is
#: the *same* row -- `job_id` stays a column naming the last job that measured the
#: address, and the per-job item lives in `job_item(job_id, item_id)` (F28, F09).
RESULTS_KEY = ("profile_id", "profile_revision", "access_id", "access_revision", "endpoint_id")
RESULTS_NEW_SQL = """CREATE TABLE results_new(
    profile TEXT NOT NULL,
    proxy TEXT NOT NULL,
    payload TEXT NOT NULL,
    observation_id TEXT REFERENCES observations(id),
    endpoint_id TEXT NOT NULL REFERENCES endpoints(id),
    access_id TEXT NOT NULL DEFAULT '',
    access_revision INTEGER NOT NULL DEFAULT 0,
    profile_id TEXT NOT NULL DEFAULT '',
    profile_revision INTEGER NOT NULL DEFAULT 0,
    checked_at REAL,
    valid_until REAL,
    error_code TEXT,
    error_stage TEXT,
    job_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(profile_id, profile_revision, access_id, access_revision, endpoint_id, job_id))"""
#: The rebuilt table of migration 15: one row per address per profile and access.
RESULTS_KEY_SQL = RESULTS_NEW_SQL.replace(
    "PRIMARY KEY(profile_id, profile_revision, access_id, access_revision, endpoint_id, job_id)",
    "PRIMARY KEY(profile_id, profile_revision, access_id, access_revision, endpoint_id)")


def _m13(conn, context):
    """The rebuild SQLite cannot avoid: a PRIMARY KEY cannot be altered otherwise.

    Legacy rows are backfilled from their own `profile`/`proxy` values, so a past
    measurement keeps a real endpoint row. The key columns are NOT NULL, so two
    access revisions of one address cannot collapse into one row, which is what F04
    requires. Migration 15 narrows the key further, to one row per address.
    Re-running on an already rebuilt table is a no-op (§3.3).

    The backfill has to reach the layer that is actually checked.  `export()` and the
    GUI table read `results.payload` and never the identity columns, so a payload left
    untouched made every historic row fail admission on a scope it was never measured
    in -- the migration claimed the rows stayed "readable" while nothing could read
    them (F02, F24).  The identity is therefore written into the payload too, and only
    where it is recoverable from the legacy file itself: `proxy`, `profile` and the
    legacy collection the candidates were imported into.  What a 2.x file simply does
    not record -- the network the check ran on, a lifetime -- is left absent on
    purpose; admission then refuses the row with that true reason instead of a scope
    mismatch, and the user's remedy is a recheck.
    """
    if tuple(primary_key(conn, "results")) == RESULTS_NEW_KEY:
        return
    now = context.now
    # `endpoint_id` carries a foreign key, so every backfilled value must already
    # exist. Addresses measured before `endpoints` existed become honest new rows.
    conn.execute(
        "INSERT OR IGNORE INTO endpoints(id, canonical, first_seen_at, last_seen_at)"
        " SELECT endpoint_id(proxy), proxy, ?, ? FROM results"
        " WHERE endpoint_id IS NULL OR endpoint_id = '' GROUP BY proxy", (now, now))
    conn.execute("UPDATE results SET endpoint_id = endpoint_id(proxy)"
                 " WHERE endpoint_id IS NULL OR endpoint_id = ''")
    conn.execute("UPDATE results SET profile_id = profile WHERE profile_id IS NULL OR profile_id = ''")
    conn.execute("UPDATE results SET access_id = '' WHERE access_id IS NULL")
    conn.execute("UPDATE results SET job_id = '' WHERE job_id IS NULL")
    conn.execute("UPDATE results SET access_revision = 0 WHERE access_revision IS NULL")
    conn.execute("UPDATE results SET profile_revision = 0 WHERE profile_revision IS NULL")
    # A pre-`profiles`-revisions file has exactly one revision of a profile -- its
    # config digest -- so revision 1 is a fact about the file, not a guess.  A 2.x
    # measurement had no access identity at all, so the credential-free public one is
    # the only identity the file can attest to.  A file that *does* carry access rows
    # keeps its empty access columns: which credential measured that address was
    # never recorded, and admission has to say exactly that.
    conn.execute("UPDATE results SET profile_revision = 1 WHERE profile_revision = 0")
    if not _has_accesses(conn):
        conn.execute("UPDATE results SET access_id = ? WHERE access_id = ''", (PUBLIC_ACCESS_ID,))
        conn.execute("UPDATE results SET access_revision = 1 WHERE access_revision = 0")
    column_list = ", ".join(_ident(name) for name in RESULTS_NEW_COLUMNS)
    conn.execute(RESULTS_NEW_SQL)
    conn.execute(f"INSERT INTO results_new ({column_list}) SELECT {column_list} FROM results")
    conn.execute("DROP TABLE results")
    conn.execute("ALTER TABLE results_new RENAME TO results")
    _backfill_payload_identity(conn)


def _backfill_payload_identity(conn):
    """Copy the recovered identity of a legacy row into its payload.

    Only keys that are absent are filled, so a row a later version already wrote is
    left exactly as it is.  ``network_id`` and ``valid_until`` are deliberately not
    in the list: inventing either would be a measurement this program never made.
    """
    rows = conn.execute(
        "SELECT rowid, proxy, payload, profile_id, profile_revision, access_id,"
        " access_revision, endpoint_id FROM results").fetchall()
    legacy = _legacy_members(conn)
    updates = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        recovered = {"proxy": row["proxy"], "profile_id": row["profile_id"],
                     "profile_revision": row["profile_revision"],
                     "access_id": row["access_id"],
                     "access_revision": row["access_revision"]}
        if row["endpoint_id"] in legacy:
            # The address was in the legacy candidate list, so the collection those
            # candidates were imported into is the one it was collected for.
            recovered["collection_id"] = LEGACY_COLLECTION_ID
        filled = {name: value for name, value in recovered.items()
                  if payload.get(name) in (None, "")}
        if not filled:
            continue
        payload.update(filled)
        updates.append((json.dumps(payload, ensure_ascii=False), row["rowid"]))
    if updates:
        conn.executemany("UPDATE results SET payload=? WHERE rowid=?", updates)


def _legacy_members(conn):
    """Endpoint ids the legacy `collect` run put into the legacy collection."""
    if not _table_exists(conn, "membership"):
        return frozenset()
    return frozenset(row[0] for row in conn.execute(
        "SELECT endpoint_id FROM membership WHERE collection_id=?", (LEGACY_COLLECTION_ID,)))


def _has_accesses(conn):
    """Whether the file records access identities at all."""
    if not _table_exists(conn, "accesses"):
        return False
    return conn.execute("SELECT 1 FROM accesses LIMIT 1").fetchone() is not None


def _m14(conn, context):
    for statement in (
        "CREATE INDEX IF NOT EXISTS results_profile_valid_until ON results(profile_id, valid_until)",
        "CREATE INDEX IF NOT EXISTS results_access ON results(access_id, access_revision)",
        "CREATE INDEX IF NOT EXISTS results_endpoint_checked ON results(endpoint_id, checked_at DESC)",
        "CREATE INDEX IF NOT EXISTS membership_endpoint ON membership(endpoint_id)",
        # §3.3 asks for checked_at here, but migration 4 gives observations
        # started_at/finished_at and §3.2 allows indexes only over existing columns.
        "CREATE INDEX IF NOT EXISTS observations_endpoint_started ON observations(endpoint_id, started_at DESC)",
        "CREATE INDEX IF NOT EXISTS job_item_state ON job_item(job_id, state)",
        "CREATE INDEX IF NOT EXISTS endpoints_canonical ON endpoints(canonical)",
        "CREATE INDEX IF NOT EXISTS api_keys_prefix ON api_keys(prefix)",
    ):
        conn.execute(statement)


def _collapse_payload(newest, group):
    """The newest payload plus the summed history of the rows it replaces.

    Collapsing must not forget what was measured: `checks`/`passes` are the counters
    ranked.csv and the GUI publish, so they are added over the whole group while the
    verdict itself stays the newest one (F28, F12).
    """
    if len(group) < 2:
        return newest["payload"]
    try:
        row = json.loads(newest["payload"])
    except (TypeError, ValueError):
        return newest["payload"]
    if not isinstance(row, dict):
        return newest["payload"]
    checks = passes = 0
    bounds = {"first_checked": None, "last_ok": None}
    for item in group:
        try:
            body = json.loads(item["payload"])
        except (TypeError, ValueError):
            continue
        history = body.get("history") if isinstance(body, dict) else None
        if not isinstance(history, dict):
            continue
        try:
            checks += int(history.get("checks") or 0)
            passes += int(history.get("passes") or 0)
        except (TypeError, ValueError):
            continue
        for field, better in (("first_checked", min), ("last_ok", max)):
            value = history.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            current = bounds[field]
            if current is None or better(value, current):
                bounds[field] = value
    if not checks and not passes:
        return newest["payload"]
    first_checked, last_ok = bounds["first_checked"], bounds["last_ok"]
    row["history"] = {"checks": checks, "passes": passes, "first_checked": first_checked,
                      "last_ok": last_ok}
    return json.dumps(row, ensure_ascii=False)


def _m15(conn, context):
    """One row per address: a generation is a set of addresses, not of measurements.

    With `job_id` still in the key a repeat check left a second row of the same
    address standing beside the first, every consumer judged the rows independently,
    and the endpoint appeared twice in every artifact (F28, §7.10). It also meant a
    fresh failure could not cancel the older success: the failed row was rejected
    while the stale one kept its own, unexpired `valid_until` (F09, defect 2).

    The newest measurement of an address therefore *replaces* the row -- the
    per-check history it carried is summed in, so nothing measured is lost -- and
    `job_id` remains a column naming the last job that touched the address. The
    per-job item is not here: it is `job_item(job_id, item_id)`, and a repeat check
    in a new job still creates a new item there.
    """
    if tuple(primary_key(conn, "results")) == RESULTS_KEY:
        return
    column_list = ", ".join(_ident(name) for name in RESULTS_NEW_COLUMNS)
    keep = {}
    for raw in conn.execute(f"SELECT rowid, {column_list} FROM results").fetchall():
        values = dict(zip(RESULTS_NEW_COLUMNS, raw[1:]))
        key = tuple(values.get(name) for name in RESULTS_KEY)
        rank = (values.get("checked_at") if isinstance(values.get("checked_at"), (int, float))
                else float("-inf"), raw[0])
        current = keep.get(key)
        if current is None or rank > current[0]:
            keep[key] = (rank, values, [values])
        else:
            current[2].append(values)
    conn.execute(RESULTS_KEY_SQL)
    placeholders = ", ".join("?" * len(RESULTS_NEW_COLUMNS))
    for _key, (_rank, newest, group) in keep.items():
        values = dict(newest)
        values["payload"] = _collapse_payload(newest, group)
        conn.execute(f"INSERT OR REPLACE INTO results_new ({column_list})"
                     f" VALUES ({placeholders})",
                     tuple(values.get(name) for name in RESULTS_NEW_COLUMNS))
    conn.execute("DROP TABLE results")
    conn.execute("ALTER TABLE results_new RENAME TO results")
    # Renaming rebuilds the table, so migration 14's indexes over `results` went with
    # the old one.  Re-create exactly those three; the rest are on other tables.
    for statement in (
        "CREATE INDEX IF NOT EXISTS results_profile_valid_until ON results(profile_id, valid_until)",
        "CREATE INDEX IF NOT EXISTS results_access ON results(access_id, access_revision)",
        "CREATE INDEX IF NOT EXISTS results_endpoint_checked ON results(endpoint_id, checked_at DESC)",
    ):
        conn.execute(statement)


# ---------------------------------------------------------------------------
# source desk, schedule runtime, scope exclusions (migrations 16..18)
# ---------------------------------------------------------------------------

#: The tables the source desk and the source views are written against.
#:
#: `source_feed` and `membership_source` are :data:`sourcedesk.REQUESTED_DDL`
#: verbatim.  That constant is the contract between the module and the
#: migrator, and HANDOFF/sources-handoff.ru.md §3.1.2 asks for it to stay a
#: string nobody outside this file executes -- so the shape is repeated here and
#: `tests/test_area_ddl_sourcedesk.py` compares the migrated table against a
#: database built from the constant itself, so the two cannot drift.
#:
#: The other five come from the generation DDL of the sources branch, moved
#: here as migrations instead of an `executescript` in that branch's `open_db`
#: (§3.1 п. 5).  One translation was applied: the branch keys its rows by the
#: address *string* (`source_generation_entry(generation_id, proxy)`), while
#: CONTRACTS §1.1 makes `endpoints(id, canonical)` the one address entity, so
#: that column is `endpoint_id` (§1.2 п. 6).
#:
#: Foreign keys are declared only where the parent is unconditionally written
#: first: a generation exists before its entries, an observation before the
#: generation that cites it.  `membership_source` deliberately has none -- the
#: declared DDL has none, and `SourceDesk.apply_plan` writes the *canonical
#: address* it got from `ImportedEndpoint.endpoint` into `endpoint_id`, so a
#: reference to `endpoints(id)` would reject the module's own writes.  That
#: two-address-models conflict is `HANDOFF/sources-handoff.ru.md` §2 C7 and it
#: belongs to `sourcedesk.py`, not to a constraint invented here.
SOURCE_TABLES_DDL = (
    """CREATE TABLE IF NOT EXISTS source_observation(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        endpoint_id TEXT NOT NULL,
        started_at REAL NOT NULL,
        ended_at REAL,
        http_state TEXT NOT NULL,
        parse_state TEXT NOT NULL,
        cache_state TEXT NOT NULL,
        outcome TEXT NOT NULL,
        status INTEGER,
        attempts INTEGER NOT NULL DEFAULT 0,
        pages INTEGER NOT NULL DEFAULT 0,
        bytes INTEGER NOT NULL DEFAULT 0,
        received INTEGER NOT NULL DEFAULT 0,
        recognized INTEGER NOT NULL DEFAULT 0,
        accepted INTEGER NOT NULL DEFAULT 0,
        rejected INTEGER NOT NULL DEFAULT 0,
        duplicate INTEGER NOT NULL DEFAULT 0,
        duplicates_existing INTEGER NOT NULL DEFAULT 0,
        blocked INTEGER NOT NULL DEFAULT 0,
        new_endpoints INTEGER NOT NULL DEFAULT 0,
        partial INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        retryable INTEGER NOT NULL DEFAULT 0,
        body_sha256 TEXT,
        fallback_used INTEGER NOT NULL DEFAULT 0,
        profile_digest TEXT,
        retry_after REAL,
        UNIQUE(run_id, source_id, endpoint_id))""",
    """CREATE TABLE IF NOT EXISTS source_generation(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id TEXT NOT NULL,
        observation_id INTEGER REFERENCES source_observation(id),
        state TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 0,
        last_good INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        record_count INTEGER NOT NULL DEFAULT 0,
        estimated_bytes INTEGER NOT NULL DEFAULT 0,
        profile_digest TEXT,
        endpoint_url TEXT)""",
    """CREATE TABLE IF NOT EXISTS source_generation_entry(
        generation_id INTEGER NOT NULL REFERENCES source_generation(id),
        endpoint_id TEXT NOT NULL,
        metadata_json TEXT,
        PRIMARY KEY(generation_id, endpoint_id)) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS source_state(
        source_id TEXT NOT NULL,
        endpoint_id TEXT NOT NULL,
        final_url TEXT,
        etag TEXT,
        last_modified TEXT,
        last_attempt_at REAL,
        last_success_at REAL,
        last_body_at REAL,
        last_304_at REAL,
        current_generation INTEGER,
        last_good_generation INTEGER,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        backoff_until REAL,
        quarantine_until REAL,
        retry_after REAL,
        last_error TEXT,
        profile_digest TEXT,
        PRIMARY KEY(source_id, endpoint_id)) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS source_identity(
        source_id TEXT PRIMARY KEY,
        family_id TEXT,
        publisher_id TEXT,
        metadata_json TEXT)""",
    """CREATE TABLE IF NOT EXISTS source_feed(
        source_id TEXT NOT NULL,
        collection_id TEXT NOT NULL,
        mode TEXT NOT NULL,
        url_ref TEXT NOT NULL,
        public_url TEXT NOT NULL,
        source_format TEXT NOT NULL,
        adapter_kind TEXT,
        adapter_profile TEXT,
        header_refs_json TEXT NOT NULL,
        access_ref TEXT,
        access_id TEXT,
        access_revision INTEGER NOT NULL DEFAULT 1,
        etag TEXT,
        last_modified TEXT,
        body_sha256 TEXT,
        active_json TEXT NOT NULL,
        last_good_json TEXT NOT NULL,
        last_attempt_at REAL,
        last_success_at REAL,
        last_good_at REAL,
        last_validated_at REAL,
        expires_at REAL,
        next_attempt_at REAL,
        retry_after REAL,
        quarantine_until REAL,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        last_outcome TEXT,
        last_error TEXT,
        PRIMARY KEY(source_id, collection_id))""",
    """CREATE TABLE IF NOT EXISTS membership_source(
        collection_id TEXT NOT NULL,
        endpoint_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        origin TEXT NOT NULL,
        added_at REAL NOT NULL,
        last_seen_at REAL,
        PRIMARY KEY(collection_id, endpoint_id, source_id)) WITHOUT ROWID""",
)

#: Indexes over the source tables.  The first two are the ones
#: :data:`sourcedesk.REQUESTED_DDL` declares; the rest serve the reads that
#: `source_management` actually issues (`history()` and `cache_state()` are
#: `WHERE source_id=? ORDER BY id DESC LIMIT n`, and `runtime_snapshot()` counts
#: per `source_id`) -- without them those are full scans of a table that grows
#: with every fetch.
SOURCE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS membership_source_by_source ON membership_source(collection_id, source_id)",
    "CREATE INDEX IF NOT EXISTS source_feed_by_collection ON source_feed(collection_id)",
    "CREATE INDEX IF NOT EXISTS source_observation_by_source ON source_observation(source_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS source_generation_by_source ON source_generation(source_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS source_generation_by_observation ON source_generation(observation_id)",
    "CREATE INDEX IF NOT EXISTS source_state_by_source ON source_state(source_id)",
    "CREATE INDEX IF NOT EXISTS membership_source_by_endpoint ON membership_source(collection_id, endpoint_id)",
)


def _m16(conn, context):
    """The seven tables the source desk and the source views are written against.

    Purely additive: eight `CREATE ... IF NOT EXISTS` and no read of an existing
    row, so the cost does not depend on how much the file already holds and
    re-running it on a migrated database writes nothing.
    """
    for statement in SOURCE_TABLES_DDL + SOURCE_INDEXES:
        conn.execute(statement)


#: Columns `schedules` needs so that a pause and a period budget survive a
#: restart, plus the spec columns `SqliteScheduleStore.save_spec` writes only
#: when they exist (`HANDOFF/scheduler.md` §1.1; the same names are the
#: scheduler's own `REQUESTED_COLUMNS`).
#:
#: `paused` and `counters_json` are the two the contract is really about: without
#: them a restart hands the whole daily limit back, so the user reads a
#: counter, trusts it, and gets more than the limit allows (F15).
SCHEDULE_RUNTIME_COLUMNS = (
    ("last_run_at", "REAL"),
    ("paused", "INTEGER NOT NULL DEFAULT 0"),
    ("pause_reason", "TEXT"),
    ("resume_at", "REAL"),
    ("dst_policy", "TEXT"),
    ("catch_up", "INTEGER NOT NULL DEFAULT 0"),
    ("max_catch_up", "INTEGER NOT NULL DEFAULT 1"),
    ("wake_gap_s", "REAL"),
    ("power_json", "TEXT"),
    ("notify_json", "TEXT"),
    ("notifications_json", "TEXT"),
    ("counters_json", "TEXT"),
    ("updated_at", "REAL"),
)
#: The same for one recorded run: why it was due and whether it was missed.
SCHEDULE_RUN_COLUMNS = (
    ("reason", "TEXT"),
    ("missed", "INTEGER NOT NULL DEFAULT 0"),
)


def _m17(conn, context):
    """`schedules` learns the pause and the budget it has been writing nowhere.

    Additive, and `_add_column` makes it a no-op per column, so a file that a
    later build already carries them is left exactly as it is.  The defaults are
    the honest ones for a row that was saved before this migration: not paused,
    no counters spent, no grid anchor -- never a paused-looking 0 that would
    hide the fact.
    """
    for column, declaration in SCHEDULE_RUNTIME_COLUMNS:
        _add_column(conn, "schedules", column, declaration)
    for column, declaration in SCHEDULE_RUN_COLUMNS:
        _add_column(conn, "schedule_run", column, declaration)


#: A scope exclusion removes an address from the user's own scope -- not from
#: the database, and not from the global denylist
#: (`source-system-design.ru.md` §12.3: "Scan/export exclude them, but the data
#: is not deleted").  The GUI keeps these in a sidecar precisely because no such
#: table existed; the three routes it serves need `scope`, `source`, address,
#: time and the exclusive flag.
#:
#: `exclusive` and `shared` are one fact under two names: the GUI stores
#: `shared` and reasons about "exclusive addresses", and a reader of either name
#: must get the truth, so a CHECK makes a contradictory row unstorable.  The
#: foreign sources branch has the same table without the last three columns and
#: the same key, so the names stay compatible.
SCOPE_EXCLUSION_DDL = (
    """CREATE TABLE IF NOT EXISTS candidate_scope_exclusion(
        proxy TEXT NOT NULL,
        reason TEXT,
        source_id TEXT,
        scope_digest TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        expires_at REAL,
        as_seen TEXT,
        exclusive INTEGER NOT NULL DEFAULT 1,
        shared INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(proxy, scope_digest),
        CHECK(exclusive <> shared)) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS candidate_scope_exclusion_by_source"
    " ON candidate_scope_exclusion(source_id)",
    "CREATE INDEX IF NOT EXISTS candidate_scope_exclusion_by_scope"
    " ON candidate_scope_exclusion(scope_digest, created_at)",
)


def _m18(conn, context):
    """`candidate_scope_exclusion`: what the GUI kept in a JSON sidecar.

    Additive, and an exclusion is a statement about the user's scope, never
    about the address: the row is created here and no row of `candidates`,
    `membership` or `membership_source` is touched, so undo is a delete and
    another source's membership is unaffected by construction.
    """
    for statement in SCOPE_EXCLUSION_DDL:
        conn.execute(statement)


def _m19(conn, context):
    """Keep schedule runtime, execution scope and editable metadata."""
    _add_column(conn, "schedules", "activated_at", "REAL")
    _add_column(conn, "schedules", "skipped", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "schedules", "catch_up_grace_s", "REAL NOT NULL DEFAULT 0")
    _add_column(conn, "schedules", "name", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "schedules", "revision", "INTEGER NOT NULL DEFAULT 1")
    _add_column(conn, "schedules", "action", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "schedules", "collection_id", "TEXT")


MIGRATIONS = (
    Migration(0, "application_id_and_journal", _m0),
    Migration(1, "endpoints", _m1),
    Migration(2, "collections_and_membership", _m2),
    Migration(3, "accesses", _m3),
    Migration(4, "observations", _m4),
    Migration(5, "results_columns_and_write_guard", _m5),
    Migration(6, "jobs", _m6),
    Migration(7, "pools_and_schedules", _m7),
    Migration(8, "api_keys", _m8),
    Migration(9, "profile_revisions", _m9),
    Migration(10, "audit_log", _m10),
    Migration(11, "export_artifacts", _m11),
    Migration(12, "import_batches", _m12),
    Migration(13, "results_rebuild", _m13),
    Migration(14, "indexes", _m14),
    Migration(15, "results_one_row_per_address", _m15),
    Migration(16, "source_tables", _m16),
    Migration(17, "schedule_runtime_columns", _m17),
    Migration(18, "candidate_scope_exclusion", _m18),
    Migration(19, "schedule_activation_and_catch_up", _m19),
)

#: Migrations that rewrite data they did not create: a `DROP TABLE` plus a copy, so
#: the old rows are gone for good and a mistake is not reversible from the file
#: itself.  Any update that runs one of these takes a technical backup with a
#: manifest *first* -- whatever `user_version` the file happens to carry, not only a
#: legacy file at version 0 (F24, defect "backup only for user_version == 0").
DESTRUCTIVE_MIGRATIONS = frozenset({13, 15})

assert tuple(migration.version for migration in MIGRATIONS) == tuple(range(SCHEMA_VERSION + 1))


# ---------------------------------------------------------------------------
# connections and versioning
# ---------------------------------------------------------------------------


@dataclass
class _MigrationContext:
    now: float
    app_version: str = None
    backup: object = None
    legacy_candidates: int = 0


class _Connection(sqlite3.Connection):
    """Schema metadata belongs to the connection and dies when it is closed."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._column_cache = {}
        self._column_cache_version = None


def connect(path, *, read_only=False):
    """Open a connection with this package's settings and no implicit transactions.

    ``isolation_level=None`` puts transaction control in the caller's hands, which is
    what "every migration runs in one transaction" means in practice. A data folder
    is accepted anywhere a database file is, and resolves to its ``DB_FILENAME``.
    """
    path = _source_database(path)
    if read_only:
        if not path.is_file():
            raise DbError(E_FOREIGN_DB, "database file does not exist", path=path)
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, isolation_level=None,
                               factory=_Connection)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None, factory=_Connection)
    try:
        conn.row_factory = sqlite3.Row
        # The same id function the Python API uses, registered on *every* connection
        # and not only in migration 0.  Migration 13 backfills `results.endpoint_id`
        # with one set-based statement, so it needs the function -- and a file left by
        # an intermediate build of this branch (user_version 1..14) starts above
        # migration 0 and never runs it.  With the registration here, such a file
        # migrates; with it only in `_m0`, the same file died at
        # "no such function: endpoint_id" before a single row was written.
        conn.create_function("endpoint_id", 1, endpoint_id, deterministic=True)
        # A read-only open does not touch the file, so the header is read once here:
        # a file that is not a database has to fail at the open, not at the first query.
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        # foreign_keys is a no-op inside a transaction, so it is set before any BEGIN.
        conn.execute("PRAGMA foreign_keys=ON")
        if not read_only:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        # A file that is not a database fails right here; the handle must not leak.
        conn.close()
        raise
    return conn


def read_header(conn):
    """``user_version`` and ``application_id``, straight from the file header."""
    return (int(conn.execute("PRAGMA user_version").fetchone()[0]),
            int(conn.execute("PRAGMA application_id").fetchone()[0]))


def probe(path):
    """Read the version of a database without writing anything into it."""
    path = _source_database(path)
    if not path.is_file() or path.stat().st_size == 0:
        return 0, 0
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return read_header(conn)
    except sqlite3.DatabaseError as exc:
        raise DbCorruptError(E_DB_CORRUPT, str(exc), path=path) from exc
    finally:
        conn.close()


def current_version(path):
    return probe(path)[0]


def assert_current(conn):
    """Refuse to work with a database that is not exactly at :data:`SCHEMA_VERSION`."""
    user_version, application_id = read_header(conn)
    if application_id not in (0, APPLICATION_ID):
        raise DbForeignError(E_FOREIGN_DB, "application_id belongs to another application")
    if user_version > SCHEMA_VERSION:
        raise DbVersionError(
            E_VERSION_AHEAD,
            f"database is at version {user_version}, this program knows {SCHEMA_VERSION}")
    if user_version < SCHEMA_VERSION:
        raise DbVersionError(
            E_MIGRATION_FAILED, f"database is at version {user_version}, migrate() it first")
    return user_version


def _is_legacy_file(conn):
    """True when the file holds pre-versioning tables that a migration will change."""
    return any(_table_exists(conn, name) for name in
               ("candidates", "candidate_seen", "candidate_meta", "results", "profiles"))


def migrate(path, *, backup_dir=None, app_version=None, now=None, create=True):
    """Bring a database to :data:`SCHEMA_VERSION`, backing it up first when needed.

    Order of operations, and the reason for it:

    1. read ``user_version`` / ``application_id`` and refuse before any write;
    2. a legacy file (pre-versioning tables at ``user_version = 0``) gets a technical
       backup with a manifest *before* the first migration touches it;
    3. a file that is about to run a destructive migration (see
       :data:`DESTRUCTIVE_MIGRATIONS`) gets that same backup even when it is not a
       version-0 legacy file -- an intermediate build of this branch leaves a
       ``user_version`` of 1..15, and those rebuilds drop a table the user cannot
       get back from the file alone.  Migrations 16..18 are additive and are not
       in that set, so a database that only needs them is not copied;
    4. each remaining migration runs in its own transaction together with the
       ``user_version`` write and its ``schema_migrations`` row, so an interrupted
       migration leaves the file at the previous version rather than in between.

    A file that is already current comes back unchanged: no DDL, no ``user_version``
    write, no backup. A data folder is accepted instead of a file path.
    """
    path = _source_database(path)
    existed = path.is_file() and path.stat().st_size > 0
    if not existed and not create:
        raise DbError(E_PATH_CONFLICT, "database file does not exist", path=path)
    if not existed:
        path.parent.mkdir(parents=True, exist_ok=True)

    now = _now(now)
    user_version, application_id = (0, 0)
    if existed:
        # Classify the file before any pragma touches it: a non-empty file that is not
        # a database has to be refused, never silently recreated (§3.4).
        user_version, application_id = probe(path)
    conn = connect(path)
    try:
        if application_id not in (0, APPLICATION_ID):
            raise DbForeignError(
                E_FOREIGN_DB, f"application_id {application_id} does not belong to this package")
        if user_version > SCHEMA_VERSION:
            raise DbVersionError(
                E_VERSION_AHEAD,
                f"database is at version {user_version}, this program knows {SCHEMA_VERSION}")
        if user_version == SCHEMA_VERSION:
            return MigrationReport(str(path), "current", user_version, SCHEMA_VERSION,
                                   application_id, (), None, 0)

        backup = None
        # A destructive migration is irreversible from the file itself, so the backup
        # is taken on the strength of the *migration* that is about to run, not on the
        # shape of the file.  Backing up only a version-0 legacy file left every
        # intermediate build of this branch (`user_version` 1..15) rewriting `results`
        # with nothing to check the result against: `list_backups()` showed nothing.
        pending = {item.version for item in MIGRATIONS if item.version >= user_version}
        if existed and (pending & DESTRUCTIVE_MIGRATIONS or
                        (user_version == 0 and _is_legacy_file(conn))):
            backup = create_backup(conn, backup_dir or path.parent / BACKUP_DIRNAME,
                                   reason="pre-migration", app_version=app_version, now=now)

        context = _MigrationContext(now=now, app_version=app_version, backup=backup)
        applied = []
        for migration in MIGRATIONS:
            if migration.version < user_version:
                continue
            try:
                conn.execute("BEGIN IMMEDIATE")
                migration.apply(conn, context)
                conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations"
                    "(version, applied_at, app_version, backup_path) VALUES (?,?,?,?)",
                    (migration.version, now, app_version, backup.path if backup else None))
                conn.execute(f"PRAGMA user_version={migration.version}")
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                conn.execute("ROLLBACK")
                raise DbError(E_MIGRATION_FAILED,
                              f"migration {migration.version} ({migration.name}) failed: {exc}",
                              path=path) from exc
            applied.append((migration.version, migration.name))

        status = "created" if (not existed and applied) else ("migrated" if applied else "current")
        return MigrationReport(str(path), status, user_version, SCHEMA_VERSION, APPLICATION_ID,
                               tuple(applied), backup, context.legacy_candidates)
    finally:
        conn.close()


def open_db(path, **kwargs):
    """Drop-in replacement for the unversioned ``open_db``: connection + report."""
    report = migrate(path, **kwargs)
    conn = connect(path)
    try:
        assert_current(conn)
    except DbError:
        conn.close()
        raise
    return conn, report


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------


def manifest_path(backup_file):
    return Path(str(backup_file) + MANIFEST_SUFFIX)


def _copy_consistently(conn, target):
    """Copy through a path SQLite itself guarantees to be consistent."""
    if sqlite3.sqlite_version_info >= (3, 27):
        conn.execute(f"VACUUM INTO {_sql_literal(str(target))}")
        return "VACUUM INTO"
    destination = sqlite3.connect(target, isolation_level=None)
    try:
        conn.backup(destination)
    finally:
        destination.close()
    return "backup API"


def _default_backup_name(reason, user_version, now):
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
    safe = "".join(char if char.isalnum() or char in "-_" else "-" for char in str(reason))
    return f"{DB_FILENAME}.v{user_version}.{safe}.{stamp}.bak"


def create_backup(source, backup_dir, *, reason="pre-migration", name=None, app_version=None, now=None):
    """Copy a database through a consistent SQLite path and write a manifest.

    ``source`` is a path or an already open connection. The copy is a complete,
    readable database rather than a byte copy of a WAL-mode file, which is what makes
    the pre-migration backup restorable. An existing artifact is never overwritten.
    """
    now = _now(now)
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(source, (str, Path)):
        if not Path(source).is_file():
            raise BackupError(E_BACKUP_FAILED, "source database does not exist", path=source)
        conn = sqlite3.connect(source, isolation_level=None)
        owns = True
    else:
        conn = source
        owns = False
    try:
        user_version, application_id = read_header(conn)
        target = backup_dir / (name or _default_backup_name(reason, user_version, now))
        if target.exists():
            raise BackupError(E_BACKUP_FAILED, "backup file already exists", path=target)
        if conn.in_transaction:
            # VACUUM INTO cannot run inside a transaction; the caller keeps its data.
            raise BackupError(E_BACKUP_FAILED, "source connection is inside a transaction")
        tool = _copy_consistently(conn, target)
    except sqlite3.Error as exc:
        raise BackupError(E_BACKUP_FAILED, str(exc), path=backup_dir) from exc
    finally:
        if owns:
            conn.close()

    manifest = BackupManifest(
        path=str(target), sha256=sha256_file(target), bytes=target.stat().st_size,
        created_at=now, schema_version=user_version, application_id=application_id,
        source_path=str(source) if isinstance(source, (str, Path)) else _database_file(conn),
        reason=reason, tool=tool)
    try:
        manifest.write()
    except OSError as exc:
        raise BackupError(E_BACKUP_FAILED, f"manifest could not be written: {exc}", path=target) from exc
    return manifest


def verify_backup(manifest, *, deep=True):
    """True when the file is still there, still matches the checksum, and still opens."""
    manifest = as_manifest(manifest)
    path = Path(manifest.path)
    if not path.is_file() or path.stat().st_size != manifest.bytes:
        return False
    if sha256_file(path) != manifest.sha256:
        return False
    if not deep:
        return True
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            return False
        return read_header(conn) == (manifest.schema_version, manifest.application_id)
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def as_manifest(value):
    """Accept a manifest, a backup file or a manifest file, and return the manifest."""
    if isinstance(value, BackupManifest):
        return value
    path = Path(value)
    if path.name.endswith(MANIFEST_SUFFIX):
        return BackupManifest.from_dict(read_json(path))
    return BackupManifest.from_dict(read_json(manifest_path(path)))


def list_backups(backup_dir):
    """Every readable manifest in a backup folder, newest first."""
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return []
    found = []
    for path in sorted(backup_dir.glob("*" + MANIFEST_SUFFIX)):
        try:
            found.append(BackupManifest.from_dict(read_json(path)))
        except (OSError, ValueError, KeyError):
            continue
    return sorted(found, key=lambda item: item.created_at, reverse=True)


# ---------------------------------------------------------------------------
# restore, rollback and data path migration
# ---------------------------------------------------------------------------


def _source_database(source):
    """A source is a data folder, a database file or an existing backup file."""
    source = Path(source)
    return source / DB_FILENAME if source.is_dir() else source


def restore_preview(source, target_data_path, *, reason="restore"):
    """Describe a restore into a *new* data path. Writes nothing, ever.

    The current data path is never a target: a restore that would overwrite live data
    is refused rather than confirmed afterwards (§3.4, §3.5.6).
    """
    source = _source_database(source)
    target = Path(target_data_path)
    if not source.is_file():
        raise BackupError(E_BACKUP_FAILED, "source database does not exist", path=source)
    if target.exists() and target.is_file():
        raise BackupError(E_PATH_CONFLICT, "restore target must be a data folder", path=target)
    if target.exists() and source.resolve() == (target / DB_FILENAME).resolve():
        raise BackupError(E_PATH_CONFLICT, "restore target must be a new data path", path=target)

    conflicts = tuple(sorted(item.name for item in target.iterdir())) if target.is_dir() else ()
    files = [(DB_FILENAME, source.stat().st_size, sha256_file(source))]
    companion = manifest_path(source)
    if companion.is_file():
        files.append((companion.name, companion.stat().st_size, sha256_file(companion)))
    try:
        secrets = secret_bindings(source)
    except (DbError, sqlite3.Error):
        # A file the reader cannot open is the reader's problem to report, not a
        # reason to refuse to describe the copy.
        secrets = ()
    return RestorePreview(str(source), str(target), tuple(files), secrets, conflicts,
                          sum(size for _name, size, _digest in files), reason, False)


def restore(source, target_data_path, *, apply=False, reason="restore", now=None):
    """Restore a database into a new data folder. ``apply=False`` is a preview.

    The copy goes through the SQLite backup API, so a WAL source is restored
    consistently. The database it came from is left exactly where it is.
    """
    preview = restore_preview(source, target_data_path, reason=reason)
    if not apply:
        return RestoreReport(preview, None, False, "preview only; nothing was written")
    if not preview.ok:
        raise BackupError(E_PATH_CONFLICT,
                          f"target data path is not empty: {list(preview.conflicts)}",
                          path=preview.target_path)

    target = Path(preview.target_path)
    target.mkdir(parents=True, exist_ok=True)
    source = Path(preview.source_path)
    destination = target / DB_FILENAME
    reader = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
    try:
        writer = sqlite3.connect(destination, isolation_level=None)
        try:
            reader.backup(writer)
        finally:
            writer.close()
    except sqlite3.Error as exc:
        raise BackupError(E_BACKUP_FAILED, str(exc), path=destination) from exc
    finally:
        reader.close()

    user_version, application_id = probe(destination)
    manifest = BackupManifest(
        path=str(destination), sha256=sha256_file(destination), bytes=destination.stat().st_size,
        created_at=_now(now), schema_version=user_version, application_id=application_id,
        source_path=str(source), reason=reason, tool="backup API")
    manifest.write()
    restored = RestorePreview(preview.source_path, preview.target_path,
                              ((DB_FILENAME, manifest.bytes, manifest.sha256),),
                              preview.secrets, (), manifest.bytes, reason, True)
    return RestoreReport(restored, manifest, verify_backup(manifest),
                         "secrets are references only; rebind them with rebind_secrets()")


def rollback(manifest_or_backup, target_data_path, *, apply=False, now=None):
    """Put a pre-migration backup back, into a separate data path.

    The current (migrated) database stays where it is: F24 asks for the backup to
    come back and the new database to survive, not for a lossy downgrade in place.
    """
    manifest = as_manifest(manifest_or_backup)
    if not verify_backup(manifest):
        raise BackupError(E_BACKUP_FAILED, "backup failed checksum verification", path=manifest.path)
    return restore(manifest.path, target_data_path, apply=apply, reason="rollback", now=now)


def migrate_data_path(old_data_path, new_data_path, *, apply=False, now=None):
    """Move the data folder to a new location, with a backup of the old one first.

    The old folder is never deleted here: copying and reporting is the reversible
    part, and removing the original stays a separate, explicit user action.
    """
    old = Path(old_data_path)
    source = old / DB_FILENAME
    if not source.is_file():
        raise BackupError(E_BACKUP_FAILED, "source database does not exist", path=source)
    target = Path(new_data_path)
    if source.resolve() == (target / DB_FILENAME).resolve():
        raise BackupError(E_PATH_CONFLICT, "new data path must differ from the old one", path=target)

    report = restore(source, target, apply=apply, reason="data-path-move", now=now)
    if not apply:
        return report
    backup = create_backup(source, target / BACKUP_DIRNAME, reason="pre-data-path-move", now=now)
    settings = []
    for name in ("gui-settings.json", "denylist.txt", "gui-targets.json", "gui-sources.json"):
        candidate = old / name
        if candidate.is_file() and not (target / name).exists():
            (target / name).write_bytes(candidate.read_bytes())
            settings.append(name)
    note = (f"old data path kept at {old}; backup of the old database: {backup.path}; "
            f"settings copied: {settings}")
    return RestoreReport(report.preview, backup, report.verified, note)


# ---------------------------------------------------------------------------
# retention and cleanup
# ---------------------------------------------------------------------------


def _retention_where(table, policy, now):
    """Predicate for deletable rows, or None when the policy deletes nothing at all."""
    column = RETENTION_TIME_COLUMN[table]
    clauses = []
    if policy.max_age_seconds is not None:
        clauses.append(f"{_ident(column)} IS NOT NULL"
                       f" AND {_ident(column)} <= {_sql_literal(now - policy.max_age_seconds)}")
    if table == "results" and policy.expired_only:
        # A row without valid_until is inconsistent (defect 1) and is never deleted by
        # age: retention removes old proof, it does not fix a missing TTL for a row.
        clauses.append(f"valid_until IS NOT NULL AND valid_until <= {_sql_literal(now)}")
    return " AND ".join(clauses) if clauses else None


def _retention_predicate(table, policy, now):
    """The full WHERE clause retention acts on: age, plus the `keep_newest` reserve.

    With no age criterion at all, `keep_newest` is the policy on its own: keep the
    N newest rows and delete the rest. The preview counts with this same predicate,
    so what it reports and what the delete removes cannot drift apart.
    """
    where = _retention_where(table, policy, now)
    if not policy.keep_newest:
        return where
    column = RETENTION_TIME_COLUMN[table]
    reserve = (f"rowid NOT IN (SELECT rowid FROM {_ident(table)}"
               f" ORDER BY {_ident(column)} DESC LIMIT {int(policy.keep_newest)})")
    return f"{where} AND {reserve}" if where else reserve


def _retention_sql(table, policy, now):
    where = _retention_predicate(table, policy, now)
    return (f"DELETE FROM {_ident(table)} WHERE {where}", where) if where else (None, None)


def _referrers(conn, table):
    """Names of the tables whose foreign keys point at ``table``, read from the file."""
    found = set()
    for other in tables(conn):
        if other == table:
            continue
        for row in conn.execute("PRAGMA foreign_key_list(%s)" % _ident(other)):
            if row[2] == table:
                found.add(other)
    return found


def _retention_order(conn, tables):
    """The tables in the only order SQLite accepts: a referrer before what it points at.

    `results.observation_id REFERENCES observations(id)` and the pragma
    ``foreign_keys=ON`` (:func:`connect`) mean a `DELETE FROM observations` is refused
    while a result still points at the row.  The default policy names both tables and
    listed them in the wrong order, so the stock cleanup deleted nothing and then
    raised -- while the preview had already reported both tables (F24).  The order
    comes from the live foreign keys, not from a hand-written list.
    """
    remaining = list(dict.fromkeys(tables))
    ordered = []
    while remaining:
        progressed = False
        for table in list(remaining):
            if not _referrers(conn, table) & set(remaining):
                ordered.append(table)
                remaining.remove(table)
                progressed = True
        if not progressed:          # a cycle: keep the caller's order for the rest
            ordered.extend(remaining)
            break
    return tuple(ordered)


def _retention_targets(conn, policy, now):
    """The one executable plan both the preview and the apply read.

    Returns ``(targets, blocked, total)``.  ``targets`` is in delete order -- a
    referrer before what it points at -- and ``blocked`` names, per table, the
    referrers that are still going to hold rows when its own delete comes.

    Counting the blockers *here* is what makes the preview honest.  A policy that
    names ``observations`` but not the ``results`` pointing at it cannot be run;
    SQLite refuses the delete, rolls the transaction back and removes nothing.  If
    the preview counted those rows anyway, the caller would be told about a
    deletion that :func:`apply_retention` then refuses to perform -- the exact
    disagreement between "what is reported" and "what is removed" this module
    promises never to have.  So the preview reports the block and the apply
    refuses on the same computed fact (F24).

    The blockers are read against the tables the plan has already *scheduled*,
    not against the tables that happened to delete a row: a referrer that the
    policy already emptied is not an obstacle, and a referrer with nothing to
    delete never blocked anything to begin with.
    """
    targets, blocked, total = [], {}, 0
    present = [name for name in policy.include if _table_exists(conn, name)]
    scheduled = set()
    for table in _retention_order(conn, present):
        _delete, where = _retention_sql(table, policy, now)
        column = RETENTION_TIME_COLUMN[table]
        if where is None:
            rows, oldest, newest = 0, None, None
        else:
            rows = conn.execute(
                f"SELECT count(*) FROM {_ident(table)} WHERE {where}").fetchone()[0]
            bounds = conn.execute(
                f"SELECT min({_ident(column)}), max({_ident(column)}) FROM {_ident(table)}"
                f" WHERE {where}").fetchone()
            oldest, newest = bounds[0], bounds[1]
        blockers = tuple(sorted(_referrers(conn, table) - scheduled))
        if rows and blockers:
            # A referrer the policy did not empty still holds these rows, so the
            # plan cannot remove them.  Count them as zero -- the number the preview
            # reports is the number the delete removes -- and report what is being
            # held back, and by whom, in `blocked`.
            blocked[table] = (blockers, int(rows))
            rows, oldest, newest = 0, None, None
        targets.append((table, column, int(rows), oldest, newest))
        scheduled.add(table)
        total += int(rows)
    return tuple(targets), tuple((table, blockers, rows)
                                 for table, (blockers, rows) in blocked.items()), total


def retention_preview(conn, policy=None, *, now=None):
    """Count what retention would delete, plus the data size behind it.

    No writes: the call is safe to repeat and safe on a read-only connection.
    ``blocked`` names the tables whose rows the plan cannot remove, with the
    referrers that hold them, so the caller learns it here instead of from a
    failed cleanup.
    """
    policy = policy or RetentionPolicy()
    now = _now(now)
    targets, blocked, total = _retention_targets(conn, policy, now)
    return RetentionPreview(policy, now, targets, total,
                            database_bytes(_database_file(conn)),
                            sum(table_bytes(conn, target[0]) or 0 for target in targets),
                            blocked)


def apply_retention(conn, policy=None, *, now=None, vacuum=False):
    """Delete what :func:`retention_preview` reported, one transaction per table.

    Row counts and the size before/after come back in the report, so a caller can
    show what happened instead of a promise made before the fact.  The refusal
    comes from the preview's own ``blocked`` map, so what this function removes and
    what the preview promised are the same two lists read from one computation.
    """
    policy = policy or RetentionPolicy()
    now = _now(now)
    before = database_bytes(_database_file(conn))
    preview = retention_preview(conn, policy, now=now)
    blocked = {name: blockers for name, blockers, _rows in preview.blocked}
    deleted = []
    for table, _column, rows, _oldest, _newest in preview.targets:
        # A table the plan could not free is refused before anything is deleted, and
        # the refusal comes from the preview's own map -- the same computed fact the
        # caller already saw, so a cleanup never fails on something the preview did
        # not warn about (F24).
        blockers = blocked.get(table)
        if blockers and preview.blocked_rows(table):
            raise RetentionError(
                E_MIGRATION_FAILED,
                f"cannot delete from {table}: {', '.join(blockers)} still point at it;"
                f" add {' or '.join(blockers)} to include")
        delete, where = _retention_sql(table, policy, now)
        if where is None or not rows:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(delete)
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            conn.execute("ROLLBACK")
            raise RetentionError(E_MIGRATION_FAILED, str(exc)) from exc
        deleted.append((table, rows))
    vacuumed = False
    if vacuum and deleted:
        conn.execute("VACUUM")
        vacuumed = True
    after = database_bytes(_database_file(conn))
    # DELETE may only make pages reusable; it need not shrink the file, and
    # WAL can grow during cleanup. Never report a whole table as freed space.
    return RetentionReport(tuple(deleted), max(0, before - after), before,
                           after, vacuumed, preview)


#: Never removed by a cleanup: user settings and the denylist are the user's own files
#: (§3.6) -- `maintenance.RUNTIME_FILES` deliberately does not list them.
PRESERVED_FILES = ("gui-settings.json", "denylist.txt")
#: Secret material is out of scope for a data cleanup. The vault belongs to secrets.py.
PRESERVED_SECRET_FILES = ("secrets.json", "secrets.vault", "credentials.json", "vault")


def _entry_size(path):
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    return 0


def cleanup_preview(data_path):
    """What a data-folder cleanup removes, what it keeps, and how big it is.

    The runtime lists come from ``maintenance`` so there is one definition of what a
    runtime artifact is; the preserved lists are enforced here and cannot be turned off.
    """
    from . import maintenance  # local import: maintenance owns those lists

    data = Path(data_path)
    remove, protected, total = [], [], 0
    for name in maintenance.RUNTIME_FILES:
        path = data / name
        if not (path.exists() or path.is_symlink()):
            continue
        size = _entry_size(path)
        remove.append((name, size))
        total += size
    for name in maintenance.RUNTIME_DIRS:
        path = data / name
        if not path.exists():
            continue
        size = _entry_size(path)
        remove.append((name + "/", size))
        total += size
    for name, reason in [(name, "user settings") for name in PRESERVED_FILES] + \
                        [(name, "secret material") for name in PRESERVED_SECRET_FILES]:
        path = data / name
        if path.exists():
            protected.append((name, _entry_size(path), reason))
    return CleanupPreview(str(data), tuple(remove), tuple(protected), total, False)


def cleanup(data_path, *, apply=False, keep_lock=False):
    """Remove runtime artifacts, reporting the preview either way.

    ``apply=False`` is the default: cleanup answers "what would go" until the caller
    says otherwise. Secrets and user settings are never in the removal list (§3.6).
    """
    preview = cleanup_preview(data_path)
    if not apply:
        return CleanupReport(preview, (), ())
    data = Path(preview.data_path)
    removed, failed = [], []
    for name, _size in preview.remove:
        if keep_lock and name.endswith(".lock"):
            continue
        path = data / name
        try:
            if name.endswith("/"):
                shutil.rmtree(path)
            else:
                path.unlink()
            removed.append(name)
        except OSError as exc:
            failed.append((name, str(exc)))
    applied = CleanupPreview(preview.data_path, preview.remove, preview.protected,
                             preview.total_bytes, True)
    return CleanupReport(applied, tuple(removed), tuple(failed))


# ---------------------------------------------------------------------------
# secret references (values never appear here)
# ---------------------------------------------------------------------------


def _secret_bindings(conn):
    if not _table_exists(conn, "accesses"):
        return ()
    rows = conn.execute(
        "SELECT id, endpoint_id, mode, secret_ref FROM accesses"
        " WHERE secret_ref IS NOT NULL AND secret_ref != '' ORDER BY id").fetchall()
    return tuple(SecretBinding(row["id"], row["endpoint_id"], row["mode"], row["secret_ref"])
                 for row in rows)


def secret_bindings(source):
    """Every access identity that points at a vault entry, read from the database."""
    if isinstance(source, (str, Path)):
        try:
            conn = connect(source, read_only=True)
        except sqlite3.Error as exc:
            raise DbCorruptError(E_DB_CORRUPT, str(exc), path=source) from exc
        try:
            return _secret_bindings(conn)
        finally:
            conn.close()
    return _secret_bindings(source)


def _looks_like_credential(value):
    """Userinfo, or anything that carries a secret in the reference position."""
    text = str(value)
    return "@" in text and "://" in text.split("@", 1)[0]


def _access_rows(conn):
    """(access_id, secret_ref, access_revision) for every bound access, in id order."""
    if not _table_exists(conn, "accesses"):
        return ()
    return tuple((row["id"], row["secret_ref"], row["access_revision"]) for row in conn.execute(
        "SELECT id, secret_ref, access_revision FROM accesses"
        " WHERE secret_ref IS NOT NULL AND secret_ref != '' ORDER BY id"))


def rebind_secrets(source, mapping, *, dry_run=True, now=None):
    """Point access identities at another vault reference. Data and secrets stay apart.

    ``mapping`` is ``{old_secret_ref: new_secret_ref}``; both sides are references. A
    value that looks like a credential is refused, so a secret cannot reach the
    database through this door (§5.1).

    A rebind is a rotation in meaning, and it moves the row the same way
    :meth:`secrets.Coordinator.rotate` does: ``access_revision`` goes up by one and
    ``rotated_at`` gets the moment.  Admission is keyed on the exact
    ``(access_id, access_revision)`` pair (F09), so without the bump every result
    measured with the credential the access used *before* the rebind stayed
    admissible afterwards -- the restored password inherited a successful check it
    had never earned.  A restore that pointed an access back at a different vault
    entry is exactly that case, and nothing else in this module could have caught
    it: a rebind rewrites no measurement, so nothing but the revision says the
    evidence is stale.

    The new vault entry has to carry the revision this returns
    (``report.revisions``), or ``secrets.Coordinator.resolve()`` refuses the access
    with ``E_CONFLICT`` and it stays unusable until that entry is re-staged at the
    reported revision.  That is the intended direction: an access that cannot be
    resolved is visibly broken, while an access that resolves against a superseded
    revision silently keeps trusting measurements taken with a password the user
    has replaced.  Note that ``reconcile()`` does not fix it -- it finalises a
    *staged* entry and leaves a ready one at its own revision alone; see
    docs/integration/HANDOFF/fix-keys.md.
    """
    mapping = dict(mapping)
    for old_ref, new_ref in mapping.items():
        if not isinstance(new_ref, str) or not new_ref.strip():
            raise DbError(E_PATH_CONFLICT, f"secret reference for {old_ref!r} is empty")
        if _looks_like_credential(new_ref):
            raise DbError(E_PATH_CONFLICT,
                          "refusing to store a credential value; pass a vault reference")
    conn = connect(source) if isinstance(source, (str, Path)) else source
    owned = isinstance(source, (str, Path))
    try:
        rows = _access_rows(conn)
        known = {secret_ref for _id, secret_ref, _revision in rows}
        changed = [(access_id, secret_ref, mapping[secret_ref], revision)
                   for access_id, secret_ref, revision in rows
                   if mapping.get(secret_ref) not in (None, secret_ref)]
        moment = _now(now)
        revisions = tuple((access_id, revision, revision + 1, moment)
                          for access_id, _old, _new, revision in changed)
        if not dry_run and changed:
            if not {"access_revision", "rotated_at"} <= set(columns(conn, "accesses")):
                raise DbError(E_MIGRATION_FAILED,
                              "table accesses has no access_revision/rotated_at to rotate")
            try:
                conn.execute("BEGIN IMMEDIATE")
                for access_id, old_ref, new_ref, revision in changed:
                    moved = conn.execute(
                        "UPDATE accesses SET secret_ref = ?, access_revision = ?,"
                        " rotated_at = ? WHERE id = ? AND secret_ref = ? AND access_revision = ?",
                        (new_ref, revision + 1, moment, access_id, old_ref, revision)).rowcount
                    if not moved:
                        # Somebody rotated or rebound the row between the read and the
                        # write.  Half a rotation -- a new reference at the old revision --
                        # is the one outcome that is worse than not doing it at all.
                        conn.execute("ROLLBACK")
                        raise DbError(
                            E_MIGRATION_FAILED,
                            f"access {access_id!r} was changed by someone else;"
                            f" re-read the bindings and repeat the rebind")
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                conn.execute("ROLLBACK")
                raise DbError(E_MIGRATION_FAILED, str(exc)) from exc
        return RebindReport(dry_run,
                            tuple((access_id, old_ref, new_ref) for access_id, old_ref, new_ref, _r in changed),
                            tuple(ref for ref in mapping if ref not in known), (), revisions)
    finally:
        if owned:
            conn.close()


# ---------------------------------------------------------------------------
# endpoints and collections
# ---------------------------------------------------------------------------


def upsert_endpoint(conn, canonical, **fields):
    """Insert or update one endpoint and return its id.

    Only columns that exist in `endpoints` are written, and every write names its
    columns, so a later migration cannot break this call.
    """
    identifier = endpoint_id(canonical)
    known = set(columns(conn, "endpoints")) - {"id", "canonical"} if fields else set()
    written = {name: value for name, value in fields.items() if name in known}
    conn.execute("INSERT OR IGNORE INTO endpoints(id, canonical) VALUES (?,?)",
                 (identifier, canonical))
    if written:
        names = sorted(written)
        assignments = ", ".join(f"{_ident(name)} = ?" for name in names)
        conn.execute(f"UPDATE endpoints SET {assignments} WHERE id = ?",
                     [written[name] for name in names] + [identifier])
    return identifier


def create_collection(conn, name, *, kind="private", collection_id=None, now=None):
    """Create a collection. A personal list starts empty -- never seeded from public data.

    That is the whole of defect 11 in one function: the public base and personal
    collections are different rows, and creating one has no effect on the other.
    """
    if kind not in COLLECTION_KINDS:
        raise DbError(E_PATH_CONFLICT, f"unknown collection kind: {kind!r}")
    name = str(name).strip()
    if not name:
        raise DbError(E_PATH_CONFLICT, "collection name must not be empty")
    now = _now(now)
    identifier = collection_id or "col-" + hashlib.sha256(
        f"{kind}\0{name}\0{now!r}".encode("utf-8")).hexdigest()[:12]
    conn.execute("INSERT INTO collections(id, name, kind, created_at) VALUES (?,?,?,?)",
                 (identifier, name, kind, now))
    return identifier


def rename_collection(conn, collection_id, name):
    name = str(name).strip()
    if not name:
        raise DbError(E_PATH_CONFLICT, "collection name must not be empty")
    if not conn.execute("UPDATE collections SET name = ? WHERE id = ?",
                        (name, collection_id)).rowcount:
        raise DbError(E_PATH_CONFLICT, f"unknown collection: {collection_id!r}")
    return collection_id


def archive_collection(conn, collection_id, *, now=None):
    if not conn.execute("UPDATE collections SET archived_at = ? WHERE id = ?",
                        (_now(now), collection_id)).rowcount:
        raise DbError(E_PATH_CONFLICT, f"unknown collection: {collection_id!r}")
    return collection_id


def get_collection(conn, collection_id):
    row = conn.execute(
        "SELECT id, name, kind, created_at, archived_at FROM collections WHERE id = ?",
        (collection_id,)).fetchone()
    return dict(row) if row else None


def list_collections(conn, *, include_archived=False):
    where = "" if include_archived else " WHERE archived_at IS NULL"
    return [dict(row) for row in conn.execute(
        "SELECT id, name, kind, created_at, archived_at FROM collections" + where
        + " ORDER BY kind, name")]


def add_member(conn, collection_id, endpoint_id, *, origin="manual", now=None):
    """Add an endpoint to a collection. The endpoint itself is never removed here."""
    if origin not in COLLECTION_ORIGINS:
        raise DbError(E_PATH_CONFLICT, f"unknown membership origin: {origin!r}")
    conn.execute(
        "INSERT OR IGNORE INTO membership(collection_id, endpoint_id, added_at, origin)"
        " VALUES (?,?,?,?)", (collection_id, endpoint_id, _now(now), origin))
    return endpoint_id


def remove_member(conn, collection_id, endpoint_id):
    """Remove one membership. The address stays in every other list it belongs to (F02)."""
    return bool(conn.execute("DELETE FROM membership WHERE collection_id = ? AND endpoint_id = ?",
                             (collection_id, endpoint_id)).rowcount)


def collection_members(conn, collection_id):
    """Members of exactly one collection, with the origin that put them there."""
    rows = conn.execute(
        "SELECT e.id AS id, e.canonical AS canonical, m.origin AS origin, m.added_at AS added_at"
        " FROM membership m JOIN endpoints e ON e.id = m.endpoint_id"
        " WHERE m.collection_id = ? ORDER BY e.canonical", (collection_id,)).fetchall()
    return [dict(row) for row in rows]


def endpoint_collections(conn, endpoint_id):
    return [row[0] for row in conn.execute(
        "SELECT collection_id FROM membership WHERE endpoint_id = ? ORDER BY collection_id",
        (endpoint_id,))]


# ---------------------------------------------------------------------------
# scope exclusions
# ---------------------------------------------------------------------------

#: The scope an exclusion applies to when the caller names none.  An empty digest
#: is the user's ordinary working scope, which is what the GUI sidecar held: the
#: exclusion is "not in what I am looking at now", never a global denylist
#: (source-system-design §12.3).  Naming a scope keeps two scopes of the same
#: database independent -- one row per (address, scope).
DEFAULT_SCOPE = ""


def add_scope_exclusion(conn, proxy, *, source_id=None, scope_digest=DEFAULT_SCOPE,
                        as_seen=None, reason="source_scope", shared=False,
                        expires_at=None, now=None):
    """Exclude one address from one scope, and remove nothing.

    The row is a statement about the scope, not about the address: no row of
    `candidates`, `membership` or `membership_source` is touched, so undo is a
    delete and an address another source still contributes keeps its
    membership.  Re-excluding the same address in the same scope updates the
    row instead of duplicating it, so the count a caller reports stays a count
    of addresses.
    """
    proxy = str(proxy or "").strip()
    if not proxy:
        raise DbError(E_PATH_CONFLICT, "scope exclusion needs an address")
    exclusive = 0 if shared else 1
    conn.execute(
        "INSERT INTO candidate_scope_exclusion"
        "(proxy, reason, source_id, scope_digest, created_at, expires_at, as_seen, exclusive, shared)"
        " VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(proxy, scope_digest) DO UPDATE SET"
        " reason=excluded.reason, source_id=excluded.source_id, expires_at=excluded.expires_at,"
        " as_seen=excluded.as_seen, exclusive=excluded.exclusive, shared=excluded.shared",
        (proxy, reason, source_id, str(scope_digest or ""), _now(now), expires_at, as_seen,
         exclusive, int(bool(shared))))
    return proxy


def scope_exclusions(conn, *, scope_digest=DEFAULT_SCOPE, source_id=None, now=None,
                     include_expired=True):
    """Every exclusion of one scope, as plain rows.

    `include_expired=False` drops the rows whose `expires_at` has passed: an
    exclusion that has run out is not stored away, it simply stops applying,
    which is what the column is for.  The order is stable (`created_at`, then
    the address) so two calls return the same list.
    """
    clauses, params = ["scope_digest = ?"], [str(scope_digest or "")]
    if source_id is not None:
        clauses.append("source_id = ?")
        params.append(source_id)
    if not include_expired:
        clauses.append("(expires_at IS NULL OR expires_at > ?)")
        params.append(_now(now))
    return [dict(row) for row in conn.execute(
        "SELECT proxy, reason, source_id, scope_digest, created_at, expires_at, as_seen,"
        " exclusive, shared FROM candidate_scope_exclusion WHERE "
        + " AND ".join(clauses) + " ORDER BY created_at, proxy", params)]


def excluded_addresses(conn, *, scope_digest=DEFAULT_SCOPE, now=None, include_expired=True):
    """Just the addresses one scope hides, as a set a result read can test against.

    This is the primitive the exclusion is applied with: a caller that reads
    `results` or exports it drops these canonical addresses and nothing else,
    so an exclusion cannot remove an address the user did not exclude and
    cannot touch one belonging to another source.
    """
    return frozenset(row["proxy"] for row in scope_exclusions(
        conn, scope_digest=scope_digest, now=now, include_expired=include_expired))


def clear_scope_exclusions(conn, *, source_id=None, scope_digest=DEFAULT_SCOPE):
    """Drop the exclusions of one scope, or of one source inside it.

    Returns how many rows went.  A source that excluded nothing is not an error:
    clearing an exclusion is an undo, and an undo with nothing to undo changed
    nothing.
    """
    clause = "scope_digest = ?"
    params = [str(scope_digest or "")]
    if source_id is not None:
        clause += " AND source_id = ?"
        params.append(source_id)
    return conn.execute(
        f"DELETE FROM candidate_scope_exclusion WHERE {clause}", params).rowcount


def legacy_summary(conn):
    """What the migration moved out of the pre-versioning tables, for the report."""
    def count(table, where="1"):
        if not _table_exists(conn, table):
            return 0
        return int(conn.execute(
            f"SELECT count(*) FROM {_ident(table)} WHERE {where}").fetchone()[0])
    return {
        "candidates": count("candidates"),
        "candidate_seen": count("candidate_seen"),
        "results": count("results"),
        "endpoints": count("endpoints"),
        "legacy_members": count("membership", "origin = 'legacy'"),
        "public_base_members": count("membership", f"collection_id = {_sql_literal(PUBLIC_COLLECTION_ID)}"),
    }
