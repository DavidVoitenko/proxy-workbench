"""Desktop delivery: where a run writes, how an old data folder moves, and what an update may do.

The product's user interface is the existing loopback browser GUI, so this module
is a thin host, not a second front end: it resolves writable paths, prepares a
worker command, and hands the rest to ``gui.main``.  A new GUI framework is only
justified by a limitation this code can name, and no such limitation is known.

Three rules shape everything here:

* an installed application never writes into its own bundle - ``.app`` and
  ``Program Files`` are read-only for a normal user, and a frozen build that
  writes next to its executable (the old ``paths.default_data``) cannot run
  there at all;
* portable mode is a decision the user makes explicitly, never a fallback that
  happens when a writable folder happens to exist;
* an artifact that was not signed is reported as unsigned.  Nothing in this
  module creates, buys or stores a signing credential.

The lower half of the module is the platform layer of F22: the part that owns
the running instance.  A browser tab is not the application - closing it must
not stop the background work, quitting must, a second launch has to reach the
first one instead of starting a rival, the login item is the user's decision
and nobody else's, and a machine that fell asleep has to be told about it.
"""
from __future__ import annotations

import argparse
import contextlib
from contextlib import closing
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

from .i18n import tr
from .paths import PACKAGE

# A marker file next to the executable is the only portable-mode signal: an
# environment variable is a per-launch override, not a durable decision.
PORTABLE_MARKER_NAME = 'proxy-workbench-portable.json'
DATA_ENV = 'PROXY_WORKBENCH_DATA'
CACHE_ENV = 'PROXY_WORKBENCH_CACHE'
LOGS_ENV = 'PROXY_WORKBENCH_LOGS'
PORTABLE_ENV = 'PROXY_WORKBENCH_PORTABLE'
UPDATE_MANIFEST_ENV = 'PROXY_WORKBENCH_UPDATE_MANIFEST'
# Mirrors db.SCHEMA_VERSION.  The real number is read from db at run time; this
# value is only the fallback for a tree where the storage layer is absent, and
# tests/test_desktop_signing.py fails when the two disagree.
FALLBACK_SCHEMA_VERSION = 19
UPDATE_STATE_NAME = 'update-state.json'
BACKUP_PREFIX = 'pre-update-'
READ_CHUNK = 1024 * 1024
# F22 platform layer.
PREFERENCES_NAME = 'desktop-preferences.json'
JOURNAL_NAME = 'desktop-journal.jsonl'
JOURNAL_MAX_RECORDS = 400
INSTANCE_LOCK_NAME = 'desktop-instance.lock'
CONTROL_SOCKET_NAME = 'desktop-control.sock'
CONTROL_MAX_LINE = 64 * 1024
CONTROL_TIMEOUT_S = 5.0
TRAY_HELPER_NAME = 'proxy-workbench-tray'
TRAY_READY_TIMEOUT_S = 25.0
PLATFORM_POLL_S = 5.0
WAKE_GAP_S = 90.0
AUTOSTART_ID = 'com.proxy-workbench.desktop'
AUTOSTART_VALUE = 'ProxyWorkbench'
AUTOSTART_DESKTOP_NAME = 'proxy-workbench.desktop'
#: Flags this host consumes; everything else is passed on to the interface.
HOST_FLAGS = ('--background', '--no-tray')

__all__ = [
    'DesktopError', 'LayoutError', 'UpdateError',
    'Layout', 'resolve_layout', 'ensure_layout', 'is_writable_dir',
    'PORTABLE_MARKER_NAME', 'portable_marker', 'is_portable', 'write_portable_marker',
    'frozen', 'app_root', 'portable_root', 'macos_bundle', 'resource_root', 'resource_path',
    'worker_command', 'child_environment', 'child_cwd', 'describe_environment',
    'MigrationPlan', 'MigrationResult', 'legacy_data_roots', 'plan_migration', 'apply_migration',
    'UpdateManifest', 'UpdateNotice', 'Verification', 'SchemaCompatibility', 'UpdatePlan',
    'UpdateResult', 'compare_versions', 'read_manifest', 'find_update', 'data_schema_version',
    'supported_schema_version', 'schema_compatible', 'verify_artifact', 'plan_update', 'apply_update',
    'SigningStatus', 'NotarizationStatus', 'SignatureInfo', 'signing_status', 'notarization_status',
    'signature_of', 'publish_label',
    'Instance', 'ControlServer', 'control_request', 'read_instance', 'InstanceLock',
    'AutostartStatus', 'autostart_status', 'enable_autostart', 'disable_autostart',
    'PlatformObserver', 'network_fingerprint', 'mark_jobs_suspended', 'close_job_sleeps',
    'journal', 'read_journal', 'read_preferences', 'interface_holder', 'published_url',
    'open_schedule_engine', 'wake_tick', 'run_startup_migration', 'DesktopHost', 'main',
]


class DesktopError(Exception):
    """Base class for delivery problems that have a user-visible action."""


class LayoutError(DesktopError):
    """A path that has to be writable cannot be written."""


class UpdateError(DesktopError):
    """An update was refused before anything was changed."""


# --------------------------------------------------------------------------
# platform facts
# --------------------------------------------------------------------------

def frozen():
    """True inside a PyInstaller bundle, where the package lives in a temp dir."""
    return bool(getattr(sys, 'frozen', False))


def macos_bundle(executable=None):
    """The ``.app`` directory containing the executable, or None off macOS bundles.

    The path inside a bundle is ``…/App.app/Contents/MacOS/App``, so the bundle
    is two levels above the executable's directory, not one.
    """
    path = Path(executable or sys.executable).resolve()
    binaries = path.parent
    if binaries.name != 'MacOS':
        return None
    contents = binaries.parent
    return contents.parent if contents.name == 'Contents' and contents.parent.name.endswith('.app') else None


def app_root(executable=None):
    """Directory that holds the running executable - usually not writable."""
    return Path(executable or sys.executable).resolve().parent


def portable_root(executable=None):
    """The unit a user moves to a USB stick: the ``.app`` bundle, else the program folder."""
    return macos_bundle(executable) or app_root(executable)


def resource_root():
    """Read-only root of packaged resources (UI, sources.json).

    PyInstaller unpacks bundled data into ``sys._MEIPASS``; a source checkout
    reads straight from the package.  Nothing here ever writes to this path.
    """
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        return Path(meipass)
    return PACKAGE


def resource_path(*parts):
    """Absolute path of one packaged resource; missing resources raise OSError."""
    return resource_root().joinpath(*parts)


def is_writable_dir(path):
    """True when a directory can actually be created or written in, not merely owned."""
    path = Path(path)
    probe = path if path.is_dir() else path.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return os.access(probe, os.W_OK | os.X_OK)


# --------------------------------------------------------------------------
# layout
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Layout:
    """Three writable folders and the rule that produced them."""

    data: Path
    cache: Path
    logs: Path
    mode: str          # 'per-user' | 'portable' | 'checkout' | 'environment'
    root: Path         # the folder a user would move or back up
    reason: str        # human readable rule, quoted in diagnostics

    @property
    def portable(self):
        return self.mode == 'portable'

    def as_dict(self):
        return dict(data=str(self.data), cache=str(self.cache), logs=str(self.logs),
                    mode=self.mode, root=str(self.root), reason=self.reason, portable=self.portable)


def portable_marker(root):
    return Path(root) / PORTABLE_MARKER_NAME


def is_portable(root):
    return portable_marker(root).is_file()


def write_portable_marker(root, environ=None, now=None):
    """Record the explicit portable decision so later launches keep it."""
    environ = os.environ if environ is None else environ
    path = portable_marker(root)
    payload = dict(schema=1, product='proxy-workbench',
                   requested_at=time.time() if now is None else now,
                   data='data', cache='cache', logs='logs')
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, payload)
    return path


def _per_user_bases(environ, home, platform):
    """Standard per-user data, cache and log roots, in that order."""
    if platform == 'win32':
        local = environ.get('LOCALAPPDATA') or str(home / 'AppData' / 'Local')
        roaming = environ.get('APPDATA') or local
        return (Path(local) / 'proxy-workbench',
                Path(local) / 'proxy-workbench' / 'Cache',
                Path(roaming) / 'proxy-workbench' / 'Logs')
    if platform == 'darwin':
        support = home / 'Library' / 'Application Support'
        return (support / 'proxy-workbench', home / 'Library' / 'Caches' / 'proxy-workbench',
                home / 'Library' / 'Logs' / 'proxy-workbench')
    data = Path(environ.get('XDG_DATA_HOME') or home / '.local' / 'share')
    cache = Path(environ.get('XDG_CACHE_HOME') or home / '.cache')
    state = Path(environ.get('XDG_STATE_HOME') or home / '.local' / 'state')
    return data / 'proxy-workbench', cache / 'proxy-workbench', state / 'proxy-workbench' / 'logs'


def _is_checkout(package):
    parent = Path(package).parent
    return (parent / 'pyproject.toml').is_file() and (parent / 'Start.bat').is_file()


def resolve_layout(environ=None, *, frozen_=None, executable=None, platform=None,
                   home=None, package=None):
    """Resolve data, cache and logs for this launch.

    The order is fixed and is the whole contract of this function:

    1. ``PROXY_WORKBENCH_DATA`` - an explicit path, per launch;
    2. ``PROXY_WORKBENCH_PORTABLE=1`` - portable for this launch only;
    3. a marker file next to the program - a durable portable decision;
    4. an installed build (frozen, or a folder that is not writable) - per-user;
    5. a source checkout - the ``data/`` folder the project has always used;
    6. anything else - per-user.

    Rule 5 keeps ``Start.bat``/``run.sh``/pipx behaviour identical to today.
    Rule 4 is the change F23 asks for: a frozen build no longer writes next to
    its executable, because that place is read-only on macOS and in Program
    Files.
    """
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    is_frozen = frozen() if frozen_ is None else bool(frozen_)
    home = Path(home) if home is not None else Path(environ.get('HOME') or Path.home())
    package = Path(package) if package is not None else PACKAGE
    program = Path(executable) if executable is not None else Path(sys.executable)
    root = portable_root(program)

    override = environ.get(DATA_ENV)
    if override:
        data = Path(override).expanduser().resolve()
        cache = Path(environ.get(CACHE_ENV) or data / 'cache').expanduser().resolve()
        logs = Path(environ.get(LOGS_ENV) or data / 'logs').expanduser().resolve()
        return Layout(data, cache, logs, 'environment', data, tr(
            f'путь задан переменной {DATA_ENV}', f'path set by {DATA_ENV}'))

    if _portable_requested(environ, root):
        return _portable_layout(root, tr('portable mode включён явно', 'portable mode was requested explicitly'))

    checkout = _is_checkout(package)
    write_root = package.parent if checkout and not is_frozen else root
    if is_frozen or not is_writable_dir(write_root):
        data, cache, logs = _per_user_bases(environ, home, platform)
        return Layout(data, cache, logs, 'per-user', data, tr(
            'установленная сборка: папки пользователя',
            'installed build: per-user folders'))

    if checkout:
        data = package.parent / 'data'
        return Layout(data, data / 'cache', data / 'logs', 'checkout', package.parent, tr(
            'исходники проекта: data/ рядом с проектом', 'source checkout: data/ next to the project'))

    data, cache, logs = _per_user_bases(environ, home, platform)
    return Layout(data, cache, logs, 'per-user', data, tr(
        'папки пользователя', 'per-user folders'))


def _portable_requested(environ, root):
    flag = str(environ.get(PORTABLE_ENV) or '').strip().lower()
    return flag in ('1', 'true', 'yes', 'on') or is_portable(root)


def _portable_layout(root, reason):
    root = Path(root)
    return Layout(root / 'data', root / 'cache', root / 'logs', 'portable', root, reason)


def ensure_layout(layout):
    """Create the three folders, or explain which one cannot be written.

    A read-only bundle is a supported state, not a crash: the caller gets a
    LayoutError naming the folder and the override that fixes it.
    """
    for folder in (layout.data, layout.cache, layout.logs):
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LayoutError(tr(
                f'Не удалось создать папку {folder}: {exc}. Укажите writable-путь через {DATA_ENV}.',
                f'Cannot create {folder}: {exc}. Point {DATA_ENV} at a writable folder.')) from exc
        if not os.access(folder, os.W_OK | os.X_OK):
            raise LayoutError(tr(
                f'Папка {folder} доступна только для чтения. Укажите writable-путь через {DATA_ENV} '
                f'или включите portable mode.',
                f'{folder} is read-only. Point {DATA_ENV} at a writable folder or use portable mode.'))
    return layout


# --------------------------------------------------------------------------
# worker command, child environment, cwd
# --------------------------------------------------------------------------

def worker_command(*args, executable=None):
    """Command line that runs the CLI in a child process, frozen or not.

    Frozen, the executable is the program itself, so arguments must be CLI
    arguments - ``gui`` has already been consumed.  Not frozen, the module
    entry point is used so an editable checkout keeps working.
    """
    program = str(Path(executable) if executable is not None else sys.executable)
    if frozen():
        return [program, *args]
    return [program, '-u', '-m', 'proxy_workbench', *args]


def child_environment(layout, base=None):
    """Environment for a worker child.

    The language variable is deliberately absent: ``gui.CHILD_ENV`` owns it and
    the integration contract makes that binding part of the interface
    contract (CONTRACTS §5.4).
    """
    env = dict(os.environ if base is None else base)
    env[DATA_ENV] = str(layout.data)
    env[CACHE_ENV] = str(layout.cache)
    env[LOGS_ENV] = str(layout.logs)
    env['PYTHONUTF8'] = '1'
    return env


def child_cwd(layout):
    """Working directory for a worker child.

    ``gui.py`` starts workers with ``cwd=ROOT.parent``, which inside a bundle is
    the read-only unpack directory.  A writable, user-owned folder is the only
    correct answer for both cases.
    """
    return layout.data


def describe_environment(layout=None):
    """Everything a diagnostic bundle needs to explain where files went."""
    layout = layout or resolve_layout()
    return dict(layout=layout.as_dict(), frozen=frozen(), executable=str(Path(sys.executable).resolve()),
                resource_root=str(resource_root()), portable_root=str(portable_root()),
                writable=is_writable_dir(layout.data), python=sys.version.split()[0])


# --------------------------------------------------------------------------
# data folder migration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MigrationPlan:
    """What a move would do, computed without touching anything."""

    source: Path
    target: Path
    items: tuple = ()            # (relative path, bytes)
    total_bytes: int = 0
    missing: tuple = ()          # files in the source that the target already has, with a different size
    backup: Path | None = None
    reason: str = ''

    @property
    def empty(self):
        return not self.items

    def as_dict(self):
        return dict(source=str(self.source), target=str(self.target), files=len(self.items),
                    total_bytes=self.total_bytes, conflicts=[str(p) for p in self.missing],
                    backup=str(self.backup) if self.backup else None, reason=self.reason)


@dataclass(frozen=True)
class MigrationResult:
    applied: bool
    source: Path
    target: Path
    copied: tuple = ()
    skipped: tuple = ()
    backup: Path | None = None
    receipt: Path | None = None
    errors: tuple = ()
    preview: bool = False

    @property
    def ok(self):
        return self.applied and not self.errors

    def as_dict(self):
        return dict(applied=self.applied, source=str(self.source), target=str(self.target),
                    copied=[str(p) for p in self.copied], skipped=[str(p) for p in self.skipped],
                    backup=str(self.backup) if self.backup else None,
                    receipt=str(self.receipt) if self.receipt else None,
                    errors=list(self.errors), preview=self.preview, ok=self.ok)


def legacy_data_roots(layout, *, environ=None, executable=None, package=None):
    """Folders an earlier build may have written into, newest rule first.

    A frozen build used to put ``data/`` next to the executable, and that is
    still what an unupgraded copy on disk holds.  The checkout folder is listed
    too because a user who installed the app next to their sources has both.
    """
    environ = os.environ if environ is None else environ
    package = Path(package) if package is not None else PACKAGE
    found = []
    previous = environ.get('PROXY_WORKBENCH_PREVIOUS_DATA')
    if previous:
        found.append(Path(previous).expanduser())
    found.append(portable_root(executable) / 'data')
    found.append(app_root(executable) / 'data')
    found.append(Path(package).parent / 'data')
    seen = []
    for path in found:
        if path != layout.data and path.is_dir() and path not in seen:
            seen.append(path)
    return tuple(seen)


def plan_migration(layout, *, environ=None, executable=None, package=None, source=None, now=None):
    """Preview a move of one legacy folder into the resolved layout.

    Nothing is written.  A file that already exists in the target with a
    different size is a conflict, not something to overwrite silently.
    """
    if source is not None:
        candidates = (Path(source).expanduser(),)
    else:
        candidates = legacy_data_roots(layout, environ=environ, executable=executable, package=package)
    for candidate in candidates:
        if candidate.is_dir():
            break
    else:
        return MigrationPlan(source=candidates[0] if candidates else Path('.'), target=layout.data,
                             reason=tr('старая папка не найдена', 'no legacy folder found'))

    items, conflicts, total = [], [], 0
    for path in sorted(candidate.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(candidate)
        if relative.as_posix() in ('proxies.sqlite3-wal', 'proxies.sqlite3-shm', 'proxies.sqlite3-journal'):
            continue
        size = path.stat().st_size
        existing = layout.data / relative
        if existing.is_file() and not _same_content(path, existing):
            conflicts.append(relative)
            continue
        items.append((relative, size))
        total += size
    backup = _backup_path(candidate, now=now)
    return MigrationPlan(source=candidate, target=layout.data, items=tuple(items), total_bytes=total,
                         missing=tuple(conflicts), backup=backup,
                         reason=tr('перенос старой папки в выбранное место',
                                   'moving the old folder into the chosen location'))


def apply_migration(plan, *, execute=False):
    """Copy the planned files, keeping a verified backup of the source.

    The source is never deleted: a user who migrates by accident keeps their
    old folder, and the receipt says which copy is the live one.
    """
    if plan.empty:
        errors = () if plan.source.is_dir() else (tr('старая папка не найдена', 'no legacy folder found'),)
        return MigrationResult(applied=False, source=plan.source, target=plan.target, errors=errors)
    if not execute:
        return MigrationResult(applied=False, source=plan.source, target=plan.target,
                               copied=(), skipped=plan.missing, backup=plan.backup,
                               receipt=None, preview=True)
    copied, errors = [], []
    backup = _copy_tree(plan.source, plan.backup, database=plan.source / 'proxies.sqlite3')
    if backup is None:
        return MigrationResult(applied=False, source=plan.source, target=plan.target,
                               errors=(tr('Не удалось создать резервную копию; перенос не выполнен.',
                                          'Could not create the backup; migration was not applied.'),))
    try:
        for relative, _size in plan.items:
            # The snapshot contains committed WAL data; copying the original
            # main database here would silently discard those transactions.
            source_file = backup / relative
            destination = plan.target / relative
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, destination)
            except OSError as exc:
                errors.append(f'{relative}: {exc}')
            else:
                copied.append(relative)
    except OSError as exc:
        errors.append(str(exc))
    receipt = plan.target / 'migration-receipt.json'
    _write_json(receipt, dict(schema=1, source=str(plan.source), target=str(plan.target),
                              applied_at=time.time(), backup=str(backup) if backup else None,
                              copied=[str(p) for p in copied], conflicts=[str(p) for p in plan.missing],
                              errors=errors, live_target=str(plan.target)))
    return MigrationResult(applied=bool(copied), source=plan.source, target=plan.target,
                           copied=tuple(copied), skipped=plan.missing, backup=backup,
                           receipt=receipt, errors=tuple(errors))


# --------------------------------------------------------------------------
# updates
# --------------------------------------------------------------------------

_VERSION_PART = re.compile(r'(\d+|[A-Za-z]+)')


def compare_versions(left, right):
    """-1/0/1 for two dotted versions; a pre-release tag sorts before its release.

    Each part is ``(rank, value)``: a number outranks a pre-release tag, and a
    pre-release tag outranks the end of the string.  Without that third rank
    ``3.0.0rc1`` would be cut off at the first missing part and compare as
    newer than the release it precedes.
    """
    def parts(value):
        return [(0, int(chunk)) if chunk.isdigit() else (1, chunk)
                for chunk in _VERSION_PART.findall(str(value or ''))]

    a, b = parts(left), parts(right)
    for index in range(max(len(a), len(b))):
        x = a[index] if index < len(a) else (2, '')
        y = b[index] if index < len(b) else (2, '')
        if x != y:
            return -1 if x < y else 1
    return 0


@dataclass(frozen=True)
class UpdateManifest:
    """A release description a user can be shown and a file can be checked against.

    Fields the publisher does not fill in are refused rather than guessed:
    ``sha256`` is mandatory, and ``signature``/``key_id`` are the only source
    of a signed claim.
    """

    version: str
    url: str
    sha256: str
    size: int | None = None
    channel: str = 'stable'
    released_at: float | None = None
    min_data_schema: int | None = None
    max_data_schema: int | None = None
    signature: str | None = None
    key_id: str | None = None
    notes_url: str | None = None
    extra: dict = field(default_factory=dict, repr=False, compare=False)

    REQUIRED = ('version', 'url', 'sha256')

    @classmethod
    def from_dict(cls, payload):
        if not isinstance(payload, dict):
            raise UpdateError(tr('Манифест обновления должен быть объектом JSON.',
                                 'The update manifest must be a JSON object.'))
        missing = [name for name in cls.REQUIRED if not payload.get(name)]
        if missing:
            raise UpdateError(tr(f'В манифесте обновления нет: {", ".join(missing)}.',
                                 f'The update manifest is missing: {", ".join(missing)}.'))
        known = {name for name in ('version', 'url', 'sha256', 'size', 'channel', 'released_at',
                                   'min_data_schema', 'max_data_schema', 'signature', 'key_id', 'notes_url')}
        return cls(version=str(payload['version']), url=str(payload['url']),
                   sha256=str(payload['sha256']).lower(),
                   size=int(payload['size']) if payload.get('size') is not None else None,
                   channel=str(payload.get('channel') or 'stable'),
                   released_at=float(payload['released_at']) if payload.get('released_at') is not None else None,
                   min_data_schema=_optional_int(payload, 'min_data_schema'),
                   max_data_schema=_optional_int(payload, 'max_data_schema'),
                   signature=payload.get('signature') or None, key_id=payload.get('key_id') or None,
                   notes_url=payload.get('notes_url') or None,
                   extra={k: v for k, v in payload.items() if k not in known})

    @classmethod
    def from_file(cls, path):
        path = Path(path)
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except OSError as exc:
            raise UpdateError(tr(f'Не удалось прочитать манифест обновления {path}: {exc}.',
                                 f'Cannot read the update manifest {path}: {exc}.')) from exc
        except ValueError as exc:
            raise UpdateError(tr(f'Манифест обновления {path} не является корректным JSON: {exc}.',
                                 f'The update manifest {path} is not valid JSON: {exc}.')) from exc
        return cls.from_dict(payload)

    def as_dict(self):
        return dict(version=self.version, url=self.url, sha256=self.sha256, size=self.size,
                    channel=self.channel, released_at=self.released_at,
                    min_data_schema=self.min_data_schema, max_data_schema=self.max_data_schema,
                    signature=self.signature, key_id=self.key_id, notes_url=self.notes_url, **self.extra)


def _optional_int(payload, name):
    value = payload.get(name)
    return None if value is None else int(value)


@dataclass(frozen=True)
class UpdateNotice:
    """What the user is told before anything is downloaded or replaced."""

    current_version: str
    manifest: UpdateManifest
    reason: str = ''

    @property
    def available(self):
        return compare_versions(self.manifest.version, self.current_version) > 0

    @property
    def signed_claim(self):
        return bool(self.manifest.signature)

    def as_dict(self):
        return dict(current_version=self.current_version, available=self.available,
                    version=self.manifest.version, url=self.manifest.url, sha256=self.manifest.sha256,
                    channel=self.manifest.channel, released_at=self.manifest.released_at,
                    notes_url=self.manifest.notes_url, reason=self.reason,
                    signed_claim=self.signed_claim)


def read_manifest(path_or_payload):
    """Accept a path or an already parsed payload; both end at one place."""
    if isinstance(path_or_payload, (dict, UpdateManifest)):
        return path_or_payload if isinstance(path_or_payload, UpdateManifest) else UpdateManifest.from_dict(path_or_payload)
    return UpdateManifest.from_file(path_or_payload)


def find_update(current_version, manifest, *, channel=None):
    """A notice when the manifest offers something newer, else None."""
    manifest = read_manifest(manifest)
    if channel and manifest.channel != channel:
        return None
    notice = UpdateNotice(current_version=current_version, manifest=manifest)
    if not notice.available:
        return None
    return UpdateNotice(current_version, manifest, tr('доступна новая версия', 'a newer version is available'))


def data_schema_version(data):
    """``PRAGMA user_version`` of the database in a data folder, or None."""
    path = Path(data) / 'proxies.sqlite3'
    if not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as conn:
            return int(conn.execute('PRAGMA user_version').fetchone()[0])
    except (sqlite3.Error, ValueError, OSError):
        return None


def supported_schema_version():
    """The newest schema this build can open, read from the storage layer."""
    try:
        from . import db
    except ImportError:
        return FALLBACK_SCHEMA_VERSION
    version = getattr(db, 'SCHEMA_VERSION', None)
    return FALLBACK_SCHEMA_VERSION if version is None else int(version)


@dataclass(frozen=True)
class SchemaCompatibility:
    compatible: bool
    data_version: int | None
    supported: int
    manifest: UpdateManifest
    reason: str

    def as_dict(self):
        return dict(compatible=self.compatible, data_version=self.data_version, supported=self.supported,
                    min_data_schema=self.manifest.min_data_schema,
                    max_data_schema=self.manifest.max_data_schema, reason=self.reason)


def schema_compatible(data, manifest):
    """Refuse an update whose schema contract does not cover the user's data.

    Three independent checks, because each fails differently: the manifest may
    promise to read the current data, the manifest may promise to leave it
    alone, and this build must be able to open a database of that version.
    """
    manifest = read_manifest(manifest)
    version = data_schema_version(data)
    supported = supported_schema_version()
    if version is None:
        return SchemaCompatibility(True, None, supported, manifest, tr(
            'базы ещё нет - обновление не может её сломать', 'no database yet - an update cannot break it'))
    if version > supported:
        return SchemaCompatibility(False, version, supported, manifest, tr(
            f'база версии {version} новее этой программы ({supported})',
            f'the database is version {version}, newer than this build ({supported})'))
    if manifest.min_data_schema is not None and version < manifest.min_data_schema:
        return SchemaCompatibility(False, version, supported, manifest, tr(
            f'новая версия читает базу от {manifest.min_data_schema}, у вас {version}',
            f'the new version needs schema {manifest.min_data_schema}, this database is {version}'))
    if manifest.max_data_schema is not None and version > manifest.max_data_schema:
        return SchemaCompatibility(False, version, supported, manifest, tr(
            f'новая версия не обещает работу с базой версии {version}',
            f'the new version does not claim support for schema {version}'))
    return SchemaCompatibility(True, version, supported, manifest, tr(
        'схема совместима', 'the schema is compatible'))


@dataclass(frozen=True)
class SignatureInfo:
    signed: bool
    authority: str = ''
    kind: str = 'unknown'
    reason: str = ''

    def as_dict(self):
        return dict(signed=self.signed, authority=self.authority, kind=self.kind, reason=self.reason)


@dataclass(frozen=True)
class Verification:
    ok: bool
    path: Path
    digest: str
    digest_matches: bool
    size_matches: bool
    signature: SignatureInfo
    reason: str = ''

    @property
    def signed(self):
        return self.signature.signed

    def as_dict(self):
        return dict(ok=self.ok, path=str(self.path), digest=self.digest,
                    digest_matches=self.digest_matches, size_matches=self.size_matches,
                    signature=self.signature.as_dict(), reason=self.reason)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(READ_CHUNK), b''):
            digest.update(block)
    return digest.hexdigest()


def verify_artifact(path, manifest, *, platform=None, runner=None, finder=None):
    """Check a downloaded file against the manifest before anything is replaced.

    ``ok`` requires a matching digest.  A signature is reported only when a
    real check ran: a missing tool or a missing identity means ``signed=False``
    with a reason, never ``True`` by assumption.
    """
    path = Path(path)
    manifest = read_manifest(manifest)
    platform = sys.platform if platform is None else platform
    if not path.is_file():
        return Verification(False, path, '', False, False, SignatureInfo(False, reason=tr(
            'файла нет', 'the file is missing')), tr('файла нет', 'the file is missing'))
    size = path.stat().st_size
    digest = sha256_file(path)
    digest_matches = digest.lower() == manifest.sha256.lower()
    size_matches = manifest.size is None or int(manifest.size) == size
    signature = signature_of(path, platform=platform, runner=runner, finder=finder)
    if not digest_matches:
        reason = tr('контрольная сумма не совпала', 'the checksum does not match')
    elif not size_matches:
        reason = tr('размер не совпал с манифестом', 'the size does not match the manifest')
    else:
        reason = tr('файл соответствует манифесту', 'the file matches the manifest')
    return Verification(digest_matches and size_matches, path, digest, digest_matches, size_matches,
                        signature, reason)


@dataclass(frozen=True)
class UpdatePlan:
    """An update that passed every check, plus what has to happen to apply it."""

    layout: Layout
    manifest: UpdateManifest
    artifact: Path
    verification: Verification
    compatibility: SchemaCompatibility
    target: Path
    backup: Path
    receipt: Path
    executable: bool = True

    @property
    def ready(self):
        return self.verification.ok and self.compatibility.compatible

    def as_dict(self):
        return dict(ready=self.ready, target=str(self.target), backup=str(self.backup),
                    receipt=str(self.receipt), executable=self.executable,
                    manifest=self.manifest.as_dict(), verification=self.verification.as_dict(),
                    compatibility=self.compatibility.as_dict())


@dataclass(frozen=True)
class UpdateResult:
    applied: bool
    plan: UpdatePlan
    installed: Path | None = None
    backup: Path | None = None
    rolled_back: bool = False
    receipt: Path | None = None
    errors: tuple = ()
    preview: bool = False

    @property
    def ok(self):
        return self.applied and not self.errors

    def as_dict(self):
        return dict(applied=self.applied, ok=self.ok, installed=str(self.installed) if self.installed else None,
                    backup=str(self.backup) if self.backup else None, rolled_back=self.rolled_back,
                    receipt=str(self.receipt) if self.receipt else None, errors=list(self.errors),
                    preview=self.preview)


def _backup_path(source, now=None):
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(now if now is not None else time.time()))
    return source.parent / f'{source.name}-{BACKUP_PREFIX}{stamp}'


def _update_target(executable):
    """Where a new build is installed: replace the running program in place."""
    return Path(executable).resolve()


def plan_update(layout, manifest, artifact, *, executable=None, platform=None, runner=None, now=None):
    """Verify the artifact, check schema compatibility and reserve a data backup.

    Refusal happens here, before a single byte of user data is touched.
    """
    manifest = read_manifest(manifest)
    executable = Path(executable or sys.executable)
    verification = verify_artifact(artifact, manifest, platform=platform, runner=runner)
    compatibility = schema_compatible(layout.data, manifest)
    return UpdatePlan(layout=layout, manifest=manifest, artifact=Path(artifact), verification=verification,
                      compatibility=compatibility, target=_update_target(executable),
                      backup=_backup_path(layout.data, now=now),
                      receipt=layout.data / UPDATE_STATE_NAME)


def apply_update(plan, *, execute=False):
    """Back up the data folder, install the artifact, and roll back on any failure.

    With ``execute=False`` this is a dry run: it returns the plan result without
    touching data, the program directory, or the backup location.
    """
    if not plan.ready:
        reasons = [plan.verification.reason] if not plan.verification.ok else []
        if not plan.compatibility.compatible:
            reasons.append(plan.compatibility.reason)
        return UpdateResult(False, plan, errors=tuple(reasons))
    if not execute:
        return UpdateResult(False, plan, backup=plan.backup, receipt=plan.receipt, preview=True)
    backup = _copy_tree(plan.layout.data, plan.backup, database=plan.layout.data / 'proxies.sqlite3')
    errors = []
    staged = plan.target.with_name(plan.target.name + '.new')
    installed = None
    previous = None
    try:
        _write_json(plan.receipt.parent / (UPDATE_STATE_NAME + '.pending'),
                    dict(schema=1, started_at=time.time(), from_version=None, to_version=plan.manifest.version,
                         backup=str(backup) if backup else None, target=str(plan.target),
                         sha256=plan.verification.digest, signed=plan.verification.signed))
        shutil.copy2(plan.artifact, staged)
        if plan.target.exists():
            previous = plan.target.with_name(plan.target.name + '.previous')
            shutil.copy2(plan.target, previous)
        os.replace(staged, plan.target)
        installed = plan.target
    except OSError as exc:
        errors.append(str(exc))
    if errors:
        # Nothing was moved yet in most failure cases, so the previous program
        # may never have been copied; only restore when it exists.
        rolled = _restore_program(plan.target, previous) if previous is not None else False
        _unlink(staged)
        return UpdateResult(False, plan, backup=backup, rolled_back=rolled, errors=tuple(errors))
    _write_json(plan.receipt, dict(schema=1, applied_at=time.time(), version=plan.manifest.version,
                                   target=str(plan.target), backup=str(backup) if backup else None,
                                   sha256=plan.verification.digest, signed=plan.verification.signed,
                                   signature=plan.verification.signature.as_dict()))
    return UpdateResult(True, plan, installed=installed, backup=backup, receipt=plan.receipt)


def rollback(plan, *, execute=False):
    """Put the previous program back and restore the data folder from the backup.

    A database written by a newer build is kept aside under its own name
    instead of being deleted: F24 promises a restore, not a lossless downgrade.
    """
    if not execute:
        return UpdateResult(False, plan, backup=plan.backup, preview=True)
    previous = plan.target.with_name(plan.target.name + '.previous')
    restored = _restore_program(plan.target, previous) if previous.is_file() else False
    errors = []
    if plan.backup and Path(plan.backup).is_dir():
        current = Path(plan.layout.data)
        stamp = time.strftime('%Y%m%d-%H%M%S')
        set_aside = current.parent / f'{current.name}-from-newer-{stamp}'
        if current.exists():
            try:
                os.replace(current, set_aside)
            except OSError as exc:
                errors.append(str(exc))
                set_aside = None
        if set_aside is not None:
            try:
                shutil.copytree(plan.backup, current)
            except OSError as exc:
                errors.append(str(exc))
                if set_aside and set_aside.exists():
                    os.replace(set_aside, current)
    _unlink(plan.receipt.with_name(plan.receipt.name + '.pending'))
    if plan.receipt.exists():
        plan.receipt.unlink()
    return UpdateResult(False, plan, backup=plan.backup, rolled_back=restored,
                        errors=tuple(errors) or (tr('откат выполнен', 'rollback done'),))


# --------------------------------------------------------------------------
# signing and notarization
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SigningStatus:
    """Whether this machine can sign, and what is missing if it cannot."""

    platform: str
    tool: str
    available: bool
    identity: str = ''
    reason: str = ''
    env_names: tuple = ()

    def as_dict(self):
        return dict(platform=self.platform, tool=self.tool, available=self.available,
                    identity=self.identity, reason=self.reason, env_names=list(self.env_names))


@dataclass(frozen=True)
class NotarizationStatus:
    platform: str
    tool: str
    available: bool
    profile: str = ''
    reason: str = ''
    env_names: tuple = ()

    def as_dict(self):
        return dict(platform=self.platform, tool=self.tool, available=self.available,
                    profile=self.profile, reason=self.reason, env_names=list(self.env_names))


def _run(argv, runner=None):
    """Run a probe command; a missing tool is a fact, not an exception."""
    call = runner or (lambda command: subprocess.run(command, capture_output=True, text=True, timeout=60))
    try:
        done = call(list(argv))
    except (OSError, subprocess.SubprocessError):
        return None
    return done if getattr(done, 'returncode', 1) == 0 else None


def _which(tool, finder=None):
    return (finder or shutil.which)(tool)


def signing_status(environ=None, *, platform=None, runner=None, finder=None):
    """Report signing capability for this platform.

    This function only looks.  It never creates a certificate, never stores a
    password, and never asks an interactive tool to make one.
    """
    platform = sys.platform if platform is None else platform
    if platform == 'darwin':
        if not _which('codesign', finder):
            return SigningStatus('darwin', 'codesign', False, reason=tr(
                'codesign не найден: нужен Xcode Command Line Tools',
                'codesign is missing: install the Xcode Command Line Tools'))
        done = _run(['security', 'find-identity', '-v', '-p', 'codesigning'], runner)
        if done is None:
            return SigningStatus('darwin', 'codesign', False, reason=tr(
                'не удалось прочитать кодовые идентичности',
                'could not read the code signing identities'))
        # `security find-identity` prints one line per identity with the name in
        # double quotes:  1) ABC… "Developer ID Application: Name (TEAMID)"
        identities = [line.split('"')[1] for line in done.stdout.splitlines() if '"' in line]
        distribution = [name for name in identities if 'Developer ID Application' in name]
        if distribution:
            return SigningStatus('darwin', 'codesign', True, identity=distribution[0],
                                 reason=tr('подпись Developer ID доступна', 'a Developer ID identity is available'))
        if identities:
            return SigningStatus('darwin', 'codesign', True, identity=identities[0], reason=tr(
                'подпись доступна, но идентичности для распространения нет',
                'signing is available, but no distribution identity is present'))
        return SigningStatus('darwin', 'codesign', False, reason=tr(
            'в связке ключей нет ни одной кодовой идентичности',
            'the keychain holds no code signing identity'))
    if platform == 'win32':
        if not _which('signtool', finder):
            return SigningStatus('win32', 'signtool', False, reason=tr(
                'signtool не найден: нужен Windows SDK',
                'signtool is missing: install the Windows SDK'))
        return SigningStatus('win32', 'signtool', True, reason=tr(
            'инструмент есть; сертификат должен быть предоставлен сборке',
            'the tool exists; a certificate must be supplied to the build'))
    return SigningStatus(platform, '', False, reason=tr(
        'подпись на этой платформе не настроена', 'signing is not configured for this platform'))


def notarization_status(environ=None, *, platform=None, runner=None, finder=None):
    """Report whether notarization could run: the tool plus credentials, never new ones."""
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    if platform != 'darwin':
        return NotarizationStatus(platform, '', False, reason=tr(
            'нотаризация описана только для macOS', 'notarization is described for macOS only'))
    if not _which('xcrun', finder):
        return NotarizationStatus(platform, 'xcrun notarytool', False, reason=tr(
            'xcrun не найден', 'xcrun is missing'))
    required = ('APPLE_ID', 'APPLE_PASSWORD', 'APPLE_TEAM_ID')
    profile = environ.get('NOTARYTOOL_PROFILE') or ''
    missing = [name for name in required if not environ.get(name)]
    if missing and not profile:
        return NotarizationStatus(platform, 'xcrun notarytool', False, reason=tr(
            f'нет учётных данных нотаризации: {", ".join(missing)}',
            f'no notarization credentials: {", ".join(missing)}'), env_names=required)
    if not profile and _run(['xcrun', 'notarytool', 'history'], runner) is None:
        return NotarizationStatus(platform, 'xcrun notarytool', False, reason=tr(
            'notarytool не может работать с этими учётными данными',
            'notarytool cannot work with these credentials'), env_names=required)
    return NotarizationStatus(platform, 'xcrun notarytool', True, profile=profile,
                              reason=tr('нотаризация доступна', 'notarization is available'),
                              env_names=required)


def signature_of(path, *, platform=None, runner=None, finder=None):
    """Inspect the signature that is actually on a file.

    Only a tool that ran and succeeded produces ``signed=True``.  Two cases
    matter and both count as unsigned: no tool to check with, and an ad-hoc
    signature.  PyInstaller signs an unsigned build ad-hoc by default, and
    ``codesign --verify`` passes on it, so treating that as "signed" is exactly
    how an unsigned artifact ends up described as a signed one.
    """
    path = Path(path)
    platform = sys.platform if platform is None else platform
    if not path.exists():
        return SignatureInfo(False, reason=tr('файла нет', 'the file is missing'))
    if platform == 'darwin':
        if not _which('codesign', finder):
            return SignatureInfo(False, kind='unknown', reason=tr(
                'codesign недоступен: подпись не проверена',
                'codesign is unavailable: the signature was not verified'))
        display = _run(['codesign', '--display', '--verbose=4', str(path)], runner)
        text = _output(display)
        if display is None:
            return SignatureInfo(False, kind='unsigned', reason=tr(
                'подпись не найдена', 'no signature was found'))
        if 'Signature=adhoc' in text:
            return SignatureInfo(False, kind='adhoc', reason=tr(
                'подпись ad-hoc: издатель не подтверждён, это не подпись для распространения',
                'ad-hoc signature: the publisher is not identified, which is not a distribution signature'))
        authority = _authority(text, 'Authority')
        if not authority:
            return SignatureInfo(False, kind='unsigned', reason=tr(
                'подпись есть, но издатель в ней не назван', 'a signature is present but names no authority'))
        if _run(['codesign', '--verify', '--strict', '--verbose=2', str(path)], runner) is None:
            return SignatureInfo(False, authority=authority, kind='unverified', reason=tr(
                'подпись есть, но не проходит проверку', 'a signature is present but does not verify'))
        return SignatureInfo(True, authority=authority, kind='codesign',
                             reason=tr('подпись проверена', 'the signature verified'))
    if platform == 'win32':
        if not _which('signtool', finder):
            return SignatureInfo(False, kind='unknown', reason=tr(
                'signtool недоступен: подпись не проверена',
                'signtool is unavailable: the signature was not verified'))
        done = _run(['signtool', 'verify', '/pa', str(path)], runner)
        if done is None:
            return SignatureInfo(False, kind='unsigned', reason=tr(
                'подпись не найдена', 'no signature was found'))
        return SignatureInfo(True, kind='authenticode', reason=tr('подпись проверена', 'the signature verified'))
    return SignatureInfo(False, kind='unknown', reason=tr(
        'проверка подписи на этой платформе не реализована',
        'signature inspection is not implemented for this platform'))


def _output(done):
    """Combined stdout and stderr: codesign writes its report to stderr."""
    if done is None:
        return ''
    return f'{getattr(done, "stdout", "") or ""}\n{getattr(done, "stderr", "") or ""}'


def _authority(text, marker):
    for line in str(text).splitlines():
        if marker in line:
            return line.split('=', 1)[1].strip()
    return ''


def publish_label(status, notarized=False):
    """The one wording a release note may use for this signature state.

    ``'signed'`` requires a signature that was verified here.  Everything else
    is spelled out, so an unsigned artifact can never be described as signed.
    """
    if status.available and not notarized:
        return tr('подпись доступна, но не выполнена', 'signing is available but was not performed')
    if status.available and notarized:
        return tr('подписано и нотарифицировано', 'signed and notarized')
    return tr('не подписано', 'unsigned')


# --------------------------------------------------------------------------
# platform layer (F22): the process that owns the instance
# --------------------------------------------------------------------------
#
# Everything below is a host around the same interface the browser already uses.
# It owns no product state and repeats no decision: it reads what the interface
# publishes, asks the interface to act, and reports the two things only the
# operating system can see - that the machine slept, and that the network the
# measurements were taken on is no longer the network they describe.
#
# Five promises, each of which is checkable from outside:
#
# 1. closing the browser page leaves the background work running;
# 2. quitting from the menu bar stops the listeners and leaves no process;
# 3. a second launch reaches the first one instead of starting a rival;
# 4. the login item exists only because the user asked for it;
# 5. after a resume the scheduler is told about the sleep
#    (``scheduler.mark_wake``) and the job budget is given the slept time back
#    (``jobs.mark_awake``), so the missed slots are coalesced instead of
#    replayed one measurement at a time.

def preferences_path(layout):
    return layout.data / PREFERENCES_NAME


def _load_json(path, fallback):
    """Read JSON that a previous build wrote, tolerating damage."""
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return fallback


def read_preferences(layout):
    """Durable desktop decisions.  Absent file means "the user decided nothing"."""
    payload = _load_json(preferences_path(layout), {})
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault('schema', 1)
    autostart = payload.get('autostart')
    payload['autostart'] = autostart if isinstance(autostart, dict) else {'enabled': False}
    payload['autostart'].setdefault('enabled', False)
    payload['autostart'].setdefault('changed_at', None)
    migrations = payload.get('migrations')
    payload['migrations'] = migrations if isinstance(migrations, dict) else {}
    return payload


def save_preferences(layout, payload):
    _write_json(preferences_path(layout), payload)
    return payload


def update_preferences(layout, **changes):
    """One read-modify-write of the desktop decisions."""
    payload = read_preferences(layout)
    payload.update(changes)
    return save_preferences(layout, payload)


def journal_path(layout):
    return layout.data / JOURNAL_NAME


def journal(layout, event, **fields):
    """One durable line about something the user cannot see happen.

    A journal write must never take the background work down with it, so every
    failure here is swallowed: the record is a convenience, not a dependency.
    """
    record = dict(schema=1, at=time.time(), event=event, **fields)
    path = journal_path(layout)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
        _trim_journal(path)
    except OSError:
        pass
    return record


def _trim_journal(path):
    """Keep the file bounded without reading the whole history into memory."""
    try:
        if path.stat().st_size < 512 * 1024:
            return
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()[-JOURNAL_MAX_RECORDS:]
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        os.replace(temporary, path)
    except OSError:
        pass


def read_journal(layout, limit=20):
    path = journal_path(layout)
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return []
    found = []
    for line in lines[-int(limit):]:
        try:
            found.append(json.loads(line))
        except ValueError:
            continue
    return found


# -- the running instance ---------------------------------------------------

@dataclass(frozen=True)
class Instance:
    """Everything a second launch needs to reach the application that is up.

    The control token is in here, in a record only the owner of the data folder
    can read, and it has to be: the second launch is a separate process, and the
    control socket is the only way it can ask the first one to show the page.
    """

    pid: int
    control: str
    token: str
    url: str = ''
    started_at: float = 0.0
    tray_pid: int = 0
    version: str = ''
    platform: str = ''

    def as_dict(self):
        return dict(pid=self.pid, control=self.control, token=self.token, url=self.url,
                    started_at=self.started_at, tray_pid=self.tray_pid,
                    version=self.version, platform=self.platform)

    @classmethod
    def from_payload(cls, payload):
        if not isinstance(payload, dict) or not payload.get('control'):
            return None
        try:
            return cls(pid=int(payload.get('pid') or 0), control=str(payload['control']),
                       token=str(payload.get('token') or ''), url=str(payload.get('url') or ''),
                       started_at=float(payload.get('started_at') or 0.0),
                       tray_pid=int(payload.get('tray_pid') or 0),
                       version=str(payload.get('version') or ''),
                       platform=str(payload.get('platform') or ''))
        except (TypeError, ValueError):
            return None


def instance_lock_path(layout):
    return layout.data / INSTANCE_LOCK_NAME


def read_instance(layout):
    """The instance record of a running application, or None."""
    return Instance.from_payload(_load_json(instance_lock_path(layout), None))


class InstanceLock:
    """Exclusive claim on one data folder, released by the kernel on exit.

    The lock is what makes a second launch a second *launch* instead of a second
    application: it is held for the whole life of the process, so a crash cannot
    leave a claim behind and a clean exit cannot lose it.
    """

    def __init__(self, layout):
        self.path = instance_lock_path(layout)
        self.handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Not "a+b": an append handle would put every published record at
            # the end of the file and the record a second launch reads would be
            # two JSON objects glued together.
            # O_CREAT without O_TRUNC also avoids two simultaneous first
            # launches truncating each other's published record.
            handle = os.fdopen(os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_BINARY', 0), 0o600), 'r+b')
        except OSError:
            return False
        try:
            if os.name == 'nt':
                import msvcrt
                # Windows byte locks forbid other processes from *reading*
                # those bytes. Lock beyond the JSON record so second launches
                # can still read the control address while the owner is alive.
                handle.seek(0x7FFFFFFF)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def publish(self, instance):
        """Write the record a second launch reads, inside the lock we hold."""
        if self.handle is None:
            return None
        payload = json.dumps(instance.as_dict(), ensure_ascii=False).encode('utf-8')
        try:
            self.handle.seek(0)
            self.handle.write(payload)
            self.handle.truncate()
            self.handle.flush()
            os.fsync(self.handle.fileno())
        except OSError:
            return None
        return instance

    def release(self):
        """Empty the record first: a stale address is worse than no address."""
        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            handle.truncate()
            handle.flush()
        except OSError:
            pass
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0x7FFFFFFF)
                with contextlib.suppress(OSError):
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            with contextlib.suppress(OSError):
                handle.close()


class ControlServer:
    """The one local socket the tray helper and a second launch both speak to.

    One line of JSON in, exactly one line of JSON out, for every line received -
    an event line is answered like a command so a client can keep a single
    request/reply pairing.  A handler that raises answers with an error instead
    of taking the channel (and with it the background work) down.

    The socket is created inside the data folder with owner-only permissions, so
    only the user who owns the installation can drive the running instance.  On
    Windows, where a named pipe needs privileges this product does not ask for,
    it is a loopback listener guarded by a token instead.
    """

    def __init__(self, layout, handler, *, platform=None, token=None):
        self.layout = layout
        self.handler = handler
        self.platform = sys.platform if platform is None else platform
        self.token = token or secrets.token_hex(16)
        self.address = ''
        self.socket_path = layout.data / CONTROL_SOCKET_NAME
        self._socket_dir = None
        self._socket = None
        self._thread = None
        self._stop = threading.Event()

    def start(self) -> str:
        """Bind and start serving.  Returns the address to publish, or ''."""
        try:
            if self.platform == 'win32':
                self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self._socket.bind(('127.0.0.1', 0))
                self.address = f'tcp://127.0.0.1:{self._socket.getsockname()[1]}'
            else:
                # macOS permits only 104 bytes for a UNIX socket address.
                # A normal Unicode home/data path can exceed that limit.
                if len(os.fsencode(self.socket_path)) > 100:
                    self._socket_dir = Path(tempfile.mkdtemp(
                        prefix='pw-control-', dir='/tmp' if Path('/tmp').is_dir() else None))
                    self.socket_path = self._socket_dir / 'control.sock'
                self.socket_path.parent.mkdir(parents=True, exist_ok=True)
                with contextlib.suppress(OSError):
                    self.socket_path.unlink()
                self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._socket.bind(str(self.socket_path))
                _restrict(self.socket_path)
                self.address = f'unix://{self.socket_path}'
        except OSError:
            self.stop()
            return ''
        self._socket.listen(8)
        self._socket.settimeout(1.0)
        self._thread = threading.Thread(target=self._serve, name='desktop-control', daemon=True)
        self._thread.start()
        return self.address

    def _serve(self):
        listener = self._socket
        while not self._stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._session, args=(connection,), daemon=True).start()

    def _session(self, connection):
        buffer = b''
        with connection, contextlib.suppress(OSError):
            connection.settimeout(CONTROL_TIMEOUT_S)
            while not self._stop.is_set():
                chunk = connection.recv(4096)
                if not chunk:
                    return
                buffer += chunk
                if len(buffer) > CONTROL_MAX_LINE:
                    return
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    reply = self.answer(line)
                    connection.sendall(json.dumps(reply, ensure_ascii=False, default=str).encode('utf-8') + b'\n')

    def answer(self, line: bytes) -> dict:
        """One request line to one answer, whatever the line was."""
        try:
            request = json.loads(line.decode('utf-8'))
        except (ValueError, UnicodeError):
            return dict(ok=False, error='the request is not valid JSON')
        if not isinstance(request, dict):
            return dict(ok=False, error='the request is not a JSON object')
        if secrets.compare_digest(str(request.get('token') or ''), self.token) is False:
            return dict(ok=False, error='the control token does not match')
        try:
            reply = self.handler(request)
        except Exception as exc:  # a broken control action must not stop the background work
            return dict(ok=False, error=f'{type(exc).__name__}: {exc}')
        return reply if isinstance(reply, dict) else dict(ok=True)

    def stop(self):
        self._stop.set()
        connection, self._socket = self._socket, None
        if connection is not None:
            with contextlib.suppress(OSError):
                connection.close()
        with contextlib.suppress(OSError, ValueError):
            if self._thread is not None:
                self._thread.join(timeout=2.0)
        if self.platform != 'win32':
            with contextlib.suppress(OSError):
                self.socket_path.unlink()
            if self._socket_dir is not None:
                with contextlib.suppress(OSError):
                    self._socket_dir.rmdir()


def _restrict(path):
    """Owner-only permissions for a socket that grants control of the process."""
    with contextlib.suppress(OSError, AttributeError):
        os.chmod(path, 0o600)


def _parse_control_address(address):
    """``unix:///path`` or ``tcp://127.0.0.1:port`` into connect() arguments."""
    text = str(address or '')
    if text.startswith('unix://'):
        return 'unix', text[len('unix://'):], 0
    if text.startswith('tcp://'):
        host, _, port = text[len('tcp://'):].rpartition(':')
        return 'tcp', (host or '127.0.0.1', int(port or 0)), 0
    raise ValueError(f'unknown control address: {address!r}')


def control_request(address, token, request, *, timeout=CONTROL_TIMEOUT_S):
    """Send one command to a running instance and return its answer.

    Used by the second launch and by anything else that has to reach the
    process that is already up.  A failure is a value, not an exception: the
    caller has to be able to say "the instance that answered is not there
    anymore" without a traceback.
    """
    try:
        kind, target, _ = _parse_control_address(address)
    except (TypeError, ValueError):
        return dict(ok=False, error='the running instance published no usable address')
    connection = None
    try:
        if kind == 'unix':
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(timeout)
            connection.connect(target)
        else:
            connection = socket.create_connection(target, timeout=timeout)
        connection.settimeout(timeout)
        connection.sendall(json.dumps({**request, 'token': token}).encode('utf-8') + b'\n')
        buffer = b''
        while b'\n' not in buffer and len(buffer) <= CONTROL_MAX_LINE:
            chunk = connection.recv(4096)
            if not chunk:
                break
            buffer += chunk
        line, _, _ = buffer.partition(b'\n')
        if not line:
            return dict(ok=False, error='the running instance closed the connection')
        answer = json.loads(line.decode('utf-8'))
        return answer if isinstance(answer, dict) else dict(ok=False, error='the answer is not an object')
    except (OSError, ValueError, UnicodeError) as exc:
        return dict(ok=False, error=f'{type(exc).__name__}: {exc}')
    finally:
        if connection is not None:
            with contextlib.suppress(OSError):
                connection.close()


# -- login item -------------------------------------------------------------

@dataclass(frozen=True)
class AutostartStatus:
    """Whether the application starts when the user logs in, and why not."""

    platform: str
    supported: bool
    enabled: bool
    path: Path | None = None
    command: tuple = ()
    reason: str = ''
    changed_at: float | None = None

    def as_dict(self):
        return dict(platform=self.platform, supported=self.supported, enabled=self.enabled,
                    path=str(self.path) if self.path else None, command=list(self.command),
                    reason=self.reason, changed_at=self.changed_at)


def _home(environ=None):
    environ = os.environ if environ is None else environ
    return Path(environ.get('HOME') or Path.home())


def autostart_path(*, platform=None, environ=None):
    """Where this platform keeps a per-user login item for this application."""
    platform = sys.platform if platform is None else platform
    home = _home(environ)
    if platform == 'darwin':
        return home / 'Library' / 'LaunchAgents' / f'{AUTOSTART_ID}.plist'
    if platform == 'win32':
        return None
    config = Path((os.environ if environ is None else environ).get('XDG_CONFIG_HOME')
                  or home / '.config') / 'autostart'
    return config / AUTOSTART_DESKTOP_NAME


def autostart_command(*, platform=None, executable=None):
    """The command a login item runs: no browser, the menu bar is the interface."""
    platform = sys.platform if platform is None else platform
    program = str(Path(executable or sys.executable).resolve())
    if frozen():
        return (program, '--background')
    return (program, '-m', 'proxy_workbench.desktop', '--background')


def _windows_run_value(*, command=None):
    try:
        import winreg
    except ImportError:
        return False, None
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                         r'Software\Microsoft\Windows\CurrentVersion\Run')
    try:
        value, _ = winreg.QueryValueEx(key, AUTOSTART_VALUE)
        return True, str(value)
    except FileNotFoundError:
        return False, None
    finally:
        key.Close()


def autostart_status(layout=None, *, platform=None, environ=None, executable=None):
    """Read the login item.  Never writes, never guesses an intention.

    The preference is the user's decision; the file is the fact.  When they
    disagree the file wins and the reason says so, because a login item that
    exists is the thing a user can actually see.
    """
    platform = sys.platform if platform is None else platform
    command = autostart_command(platform=platform, executable=executable)
    if platform == 'win32':
        exists, value = _windows_run_value()
        wanted = ' '.join(f'"{part}"' if ' ' in part else part for part in command)
        return AutostartStatus('win32', True, exists and value == wanted, None, command,
                               tr('запуск при входе: ключ Run в профиле пользователя',
                                  'start at login: the Run key of the current user')
                               if exists else tr('запуск при входе выключен',
                                                 'start at login is off'))
    path = autostart_path(platform=platform, environ=environ)
    if platform != 'darwin' and platform.startswith('linux'):
        return AutostartStatus(platform, True, path.is_file() if path else False, path, command,
                               tr('запуск при входе включён', 'start at login is on')
                               if path and path.is_file() else tr('запуск при входе выключен',
                                                                  'start at login is off'))
    if platform != 'darwin':
        return AutostartStatus(platform, False, False, path, command, tr(
            'запуск при входе на этой платформе не настроен',
            'start at login is not configured for this platform'))
    if path is None or not path.is_file():
        return AutostartStatus('darwin', True, False, path, command, tr(
            'запуск при входе выключен', 'start at login is off'))
    return AutostartStatus('darwin', True, True, path, command, tr(
        'запуск при входе включён', 'start at login is on'))


def _launch_agent_plist(command, *, environment=None, working_directory=None,
                        stdout=None, stderr=None):
    payload = {
        'Label': AUTOSTART_ID,
        'ProgramArguments': list(command),
        'RunAtLoad': True,
        # A login item is not a service: if the user quits the application, it
        # stays quit until the next login.
        'KeepAlive': False,
        'ProcessType': 'Background',
    }
    if environment:
        payload['EnvironmentVariables'] = {str(k): str(v) for k, v in environment.items()}
    if working_directory:
        payload['WorkingDirectory'] = str(working_directory)
    if stdout:
        payload['StandardOutPath'] = str(stdout)
    if stderr:
        payload['StandardErrorPath'] = str(stderr)
    return plistlib.dumps(payload, sort_keys=True).decode('utf-8')


def _login_environment(layout, platform):
    """What a login item needs to find this installation from a cold start."""
    environ = {}
    if platform == 'darwin' and not frozen():
        environ['PYTHONPATH'] = str(Path(PACKAGE).parent)
        environ['PYTHONUTF8'] = '1'
        environ['PYTHONUNBUFFERED'] = '1'
    return environ


def enable_autostart(layout, *, platform=None, environ=None, executable=None, now=None):
    """Write the login item.  Only ever called from an explicit user action."""
    platform = sys.platform if platform is None else platform
    command = autostart_command(platform=platform, executable=executable)
    if platform == 'win32':
        try:
            import winreg
        except ImportError as exc:
            raise DesktopError(tr('На этой платформе нельзя записать автозапуск.',
                                  'This platform cannot write a start-at-login entry.')) from exc
        line = ' '.join(f'"{part}"' if ' ' in part else part for part in command)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                r'Software\Microsoft\Windows\CurrentVersion\Run',
                                0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, AUTOSTART_VALUE, 0, winreg.REG_SZ, line)
    elif platform.startswith('linux'):
        path = autostart_path(platform=platform, environ=environ)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = ' '.join(f'"{part}"' if ' ' in part else part for part in command)
        _write_text(path, '\n'.join([
            '[Desktop Entry]', 'Type=Application', 'Name=Proxy Workbench',
            f'Exec={line}', 'Terminal=false', 'NoDisplay=true',
            'X-GNOME-Autostart-enabled=true', '',
        ]))
    elif platform == 'darwin':
        path = autostart_path(platform=platform, environ=environ)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_text(path, _launch_agent_plist(
            command,
            environment=_login_environment(layout, platform),
            working_directory=(Path(PACKAGE).parent if not frozen() else macos_bundle()),
            stdout=layout.logs / 'autostart.log', stderr=layout.logs / 'autostart.log'))
    else:
        raise DesktopError(tr('Запуск при входе на этой платформе не поддерживается.',
                              'Start at login is not supported on this platform.'))
    stamp = time.time() if now is None else now
    update_preferences(layout, autostart=dict(enabled=True, changed_at=stamp,
                                              platform=platform, command=list(command)))
    journal(layout, 'autostart.enabled', platform=platform, command=list(command))
    return autostart_status(layout, platform=platform, environ=environ, executable=executable)


def disable_autostart(layout, *, platform=None, environ=None, now=None):
    """Remove the login item.  The user may always take this back."""
    platform = sys.platform if platform is None else platform
    if platform == 'win32':
        try:
            import winreg
        except ImportError:
            pass
        else:
            with contextlib.suppress(OSError):
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                    r'Software\Microsoft\Windows\CurrentVersion\Run',
                                    0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, AUTOSTART_VALUE)
    else:
        path = autostart_path(platform=platform, environ=environ)
        if path is not None:
            with contextlib.suppress(OSError):
                path.unlink()
    stamp = time.time() if now is None else now
    update_preferences(layout, autostart=dict(enabled=False, changed_at=stamp,
                                              platform=platform, command=[]))
    journal(layout, 'autostart.disabled', platform=platform)
    return autostart_status(layout, platform=platform, environ=environ)


def set_autostart(layout, enabled, **kwargs):
    """One entry point so a menu click and a future API cannot disagree."""
    if enabled:
        return enable_autostart(layout, **kwargs)
    return disable_autostart(layout, **kwargs)


def _write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False)
    try:
        with handle:
            handle.write(text)
        os.replace(handle.name, path)
    except BaseException:
        _unlink(handle.name)
        raise


# -- sleep, wake and the network -------------------------------------------

def network_fingerprint():
    """A cheap identity of the current network path, with no dependencies.

    It is the addresses this machine holds, which change when the user moves
    between a home network, a phone hotspot and a VPN.  It is deliberately not
    a route table: parsing one is a portability bug waiting to happen, and the
    native observer reports the real path where one exists.
    """
    found = set()
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None):
            address = info[4][0]
            if not address.startswith('127.') and address != '::1':
                found.add(address)
    except (OSError, UnicodeError):
        pass
    return tuple(sorted(found))


class PlatformObserver:
    """Sleep, wake and network change, from the OS where it can tell us.

    On macOS the menu bar helper forwards the real ``NSWorkspace`` notifications
    and the real network path, because only the system knows when the lid
    closed.  Underneath that - and on the platforms with no such helper - a poll
    compares the wall clock with the monotonic clock: a machine that slept
    comes back with a wall clock that jumped forward and a monotonic clock that
    did not, and that difference is the slept time.  Windows counts sleep inside
    its monotonic clock, so there the poll sees nothing; that is stated rather
    than papered over.
    """

    def __init__(self, on_sleep, on_wake, on_network, *, gap_s=WAKE_GAP_S, poll_s=PLATFORM_POLL_S,
                 platform=None):
        self.on_sleep = on_sleep
        self.on_wake = on_wake
        self.on_network = on_network
        self.gap_s = float(gap_s)
        self.poll_s = float(poll_s)
        self.platform = sys.platform if platform is None else platform
        self._stop = threading.Event()
        self._thread = None
        self._origin_wall = time.time()
        self._origin_mono = time.monotonic()
        self._network = network_fingerprint()
        self.clock_gaps_supported = self.platform != 'win32'

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name='desktop-platform', daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=self.poll_s + 1.0)

    def _loop(self):
        while not self._stop.wait(self.poll_s):
            try:
                self._poll()
            except Exception:  # a background observer may never take the work down
                pass

    def _poll(self):
        wall, mono = time.time(), time.monotonic()
        drift = (wall - self._origin_wall) - (mono - self._origin_mono)
        if abs(drift) >= self.gap_s:
            self._origin_wall, self._origin_mono = wall, mono
            # A backwards jump is a changed clock, not a sleep: nothing slept.
            self.on_wake(slept_s=max(0.0, drift), source='clock')
        current = network_fingerprint()
        if current != self._network:
            previous, self._network = self._network, current
            self.on_network(previous=previous, current=current, source='address')

    def native_event(self, name, body=None):
        """A report from the menu bar helper, which sees the real system events."""
        body = body or {}
        if name == 'wake':
            self.on_wake(slept_s=float(body.get('slept_s') or 0.0), source='system')
        elif name == 'sleep':
            self.on_sleep(source='system')
        elif name == 'network':
            self.on_network(status=str(body.get('status') or ''), source='system')
        return name


# -- the sleep the job layer and the scheduler have to hear about -----------

def _data_connection(layout, timeout=10.0):
    """A short-lived connection to the application's own database."""
    from . import db
    connection, _ = db.open_db(str(layout.data))
    with contextlib.suppress(sqlite3.Error, AttributeError, TypeError):
        connection.execute(f'PRAGMA busy_timeout={int(timeout * 1000)}')
    return connection


def mark_jobs_suspended(layout):
    """Record every live job as sleeping before the machine goes to sleep.

    A sleeping machine sends no heartbeat, and the heartbeat timeout is minutes
    long.  Without this marker the first recovery after a resume would read the
    silence as a crash and requeue work that is still running.
    """
    from . import jobs
    marked, errors = [], []
    connection = None
    try:
        connection = _data_connection(layout)
        store = jobs.JobStore(connection)
        for job in store.jobs():
            if not job.active:
                continue
            with contextlib.suppress(jobs.JobError):
                store.mark_suspended(job.id)
                marked.append(job.id)
    except (OSError, sqlite3.Error, jobs.JobError) as exc:
        errors.append(f'{type(exc).__name__}: {exc}')
    finally:
        if connection is not None:
            with contextlib.suppress(sqlite3.Error):
                connection.close()
    return dict(jobs=marked, errors=errors)


def close_job_sleeps(layout):
    """Give the slept time back to every job that was marked suspended."""
    from . import jobs
    closed, errors = [], []
    connection = None
    try:
        connection = _data_connection(layout)
        store = jobs.JobStore(connection)
        for job in store.jobs():
            if not job.active:
                continue
            record = store.checkpoint(job.id, 'suspend')
            state = (record or {}).get('state') or {}
            if not state.get('suspended_at') or state.get('woke_at'):
                continue
            try:
                saved = store.mark_awake(job.id)
            except jobs.JobError as exc:
                errors.append(f'{job.id}: {exc}')
            else:
                closed.append(dict(job=job.id, slept_s=float(saved['state'].get('slept_s') or 0.0)))
    except (OSError, sqlite3.Error, jobs.JobError) as exc:
        errors.append(f'{type(exc).__name__}: {exc}')
    finally:
        if connection is not None:
            with contextlib.suppress(sqlite3.Error):
                connection.close()
    return dict(jobs=closed, errors=errors)


def open_schedule_engine(layout):
    """A ``Scheduler`` over the application's own schedule tables.

    The connection is returned as well, because the caller has to close it: a
    schedule engine is a view over one connection, not a global.
    """
    from . import scheduler
    connection = _data_connection(layout)
    return connection, scheduler.Scheduler(scheduler.SqliteScheduleStore(connection))


def wake_tick(engine):
    """Consume the wake in one tick, now, while the machine is awake.

    ``mark_wake`` is only a flag; the tick is what acts on it.  Ticking straight
    after the mark is the whole point: a schedule that missed six intervals
    while the machine slept comes back with one coalesced run instead of six
    replays, and the overdue slots are decided once rather than rediscovered on
    every later tick.  Nothing is executed here - the job layer owns that - so
    the report is the evidence, not a claim that work was started.
    """
    at = engine.mark_wake()
    report = engine.tick()
    reasons = {}
    for decision in report.decisions:
        reasons[decision.reason] = reasons.get(decision.reason, 0) + 1
    return dict(at=at, woke=bool(report.woke), schedules=len(report.decisions),
                run_requests=len(report.run_requests), reasons=reasons,
                decisions=[decision.reason for decision in report.decisions])


# -- the menu bar helper ----------------------------------------------------

#: The macOS menu bar item, in full, as a string in this module.
#:
#: It lives here rather than in a separate source file because the platform
#: layer is this module, and because a login item, a frozen bundle and a source
#: checkout all have to be able to produce the same helper.  It is compiled once
#: per machine with the Xcode command line tools; when they are missing the
#: application says so and runs without a menu bar instead of pretending.
TRAY_HELPER_SOURCE = r'''
// Proxy Workbench menu bar helper.
//
// The helper owns nothing but the menu bar item.  Every label, every enabled
// flag and every piece of state comes from the application over the control
// socket, so this file holds no product knowledge and no second source of
// truth.  It exits when the socket closes, which is what keeps a menu bar icon
// from outliving the application as an orphan process.
import Cocoa
import Network
import Darwin

private let maxLine = 1 << 18

func logLine(_ text: String) {
    FileHandle.standardError.write(("proxy-workbench-tray: " + text + "\n").data(using: .utf8)!)
}

func json(_ object: Any) -> Data {
    guard let data = try? JSONSerialization.data(withJSONObject: object) else { return Data("{}".utf8) }
    return data
}

func jsonObject(_ data: Data) -> NSDictionary? {
    guard let parsed = try? JSONSerialization.jsonObject(with: data, options: []),
          let object = parsed as? NSDictionary else { return nil }
    return object
}

// MARK: - Control channel

final class Channel {
    private let fd: Int32
    private let lock = NSLock()
    private var writes: [Data] = []
    private var waiting: [(NSDictionary) -> Void]? = nil
    private var closed = false
    var onClose: ((String) -> Void)?

    init(path: String) throws {
        let handle = socket(AF_UNIX, SOCK_STREAM, 0)
        guard handle >= 0 else { throw NSError(domain: "tray", code: 1, userInfo: [NSLocalizedDescriptionKey: "socket() failed"]) }
        fd = handle
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let capacity = MemoryLayout.size(ofValue: address.sun_path)
        let bytes = Array(path.utf8)
        guard bytes.count < capacity - 1 else {
            Darwin.close(fd)
            throw NSError(domain: "tray", code: 3, userInfo: [NSLocalizedDescriptionKey: "the control socket path is too long for this platform"])
        }
        withUnsafeMutablePointer(to: &address.sun_path) { raw in
            raw.withMemoryRebound(to: CChar.self, capacity: capacity) { target in
                memcpy(target, bytes, bytes.count)
                target[bytes.count] = 0
            }
        }
        let result = withUnsafePointer(to: &address) { pointer -> Int32 in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockaddrPointer in
                Darwin.connect(fd, sockaddrPointer, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard result == 0 else {
            Darwin.close(fd)
            throw NSError(domain: "tray", code: 2, userInfo: [NSLocalizedDescriptionKey: "connect() to the control socket failed"])
        }
        var timeout = timeval(tv_sec: 2, tv_usec: 0)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
    }

    /// One background thread owns the descriptor: writes and reads are strictly
    /// paired in order, so a reply can never be handed to the wrong request.
    func start() {
        let thread = Thread { [weak self] in self?.loop() }
        thread.name = "tray.channel"
        thread.start()
    }

    /// Every line takes one slot in the reply queue, which is why the
    /// application answers every line exactly once.  An event has no completion:
    /// its answer refreshes the menu and is dropped.
    private func enqueue(_ payload: [String: Any], completion: ((NSDictionary) -> Void)?) {
        lock.lock()
        if closed {
            lock.unlock()
            return
        }
        writes.append(json(payload))
        if waiting == nil { waiting = [] }
        waiting?.append(completion ?? { _ in })
        lock.unlock()
    }

    func request(_ payload: [String: Any], completion: @escaping (NSDictionary) -> Void) {
        enqueue(payload, completion: completion)
    }

    func emit(_ payload: [String: Any]) {
        enqueue(payload, completion: nil)
    }

    private func loop() {
        var buffer = Data()
        while true {
            let pending: ([Data], [(NSDictionary) -> Void]?) = lock.withLock { (writes, waiting) }
            for line in pending.0 {
                if !writeAll(line + Data([0x0a])) {
                    return finish("the control socket was closed")
                }
            }
            if !pending.0.isEmpty {
                lock.withLock { writes.removeFirst(pending.0.count) }
            }
            var chunk = [UInt8](repeating: 0, count: 8192)
            let count = chunk.withUnsafeMutableBytes { read(fd, $0.baseAddress, 8192) }
            if count == 0 {
                return finish(errno == 0 || errno == EAGAIN ? "the control socket was closed" : "read failed: \(errno)")
            }
            if count < 0 {
                if errno == EAGAIN || errno == EWOULDBLOCK { continue }
                return finish("read failed: \(errno)")
            }
            buffer.append(contentsOf: chunk[0..<count])
            if buffer.count > maxLine { return finish("the control channel sent too much data") }
            while let index = buffer.firstIndex(of: 0x0a) {
                let line = buffer.subdata(in: buffer.startIndex..<index)
                buffer.removeSubrange(buffer.startIndex...index)
                guard let object = jsonObject(line) else { continue }
                if (object["event"] as? String) != nil {
                    logLine("ignored an unsolicited line")
                    continue
                }
                let completion = lock.withLock { (waiting?.isEmpty ?? true) ? nil : waiting?.removeFirst() }
                if let completion = completion {
                    DispatchQueue.main.async { completion(object) }
                }
            }
        }
    }

    private func writeAll(_ data: Data) -> Bool {
        var sent = 0
        return data.withUnsafeBytes { raw -> Bool in
            guard let base = raw.baseAddress else { return true }
            while sent < data.count {
                let written = write(fd, base.advanced(by: sent), data.count - sent)
                if written <= 0 {
                    if errno == EAGAIN || errno == EWOULDBLOCK { usleep(2000); continue }
                    return false
                }
                sent += written
            }
            return true
        }
    }

    private func finish(_ reason: String) {
        lock.lock()
        if closed {
            lock.unlock()
            return
        }
        closed = true
        lock.unlock()
        Darwin.close(fd)
        DispatchQueue.main.async { [weak self] in self?.onClose?(reason) }
    }
}

extension NSLock {
    func withLock<T>(_ body: () -> T) -> T {
        lock()
        defer { unlock() }
        return body()
    }
}

// MARK: - Menu bar item

@objc final class Tray: NSObject, NSApplicationDelegate {
    private let channel: Channel
    private let statusItem: NSStatusItem
    private let monitor = NWPathMonitor()
    private let queue = DispatchQueue(label: "tray.path")
    private let token: String
    private var lastNetwork = ""
    private var pollTimer: Timer?
    private var stop = false
    private var rendered = ""
    private var answers = 0

    init(channel: Channel, token: String) {
        self.channel = channel
        self.token = token
        self.statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        if let image = NSImage(systemSymbolName: "network", accessibilityDescription: "Proxy Workbench") {
            image.isTemplate = true
            statusItem.button?.image = image
        }
        statusItem.button?.title = "PW"
        let menu = NSMenu()
        menu.autoenablesItems = false
        statusItem.menu = menu
        rebuild([])
        channel.onClose = { [weak self] reason in
            guard let self = self else { return }
            logLine("exiting: " + reason)
            self.stop = true
            NSApp.terminate(nil)
        }
        channel.start()
        channel.request(["action": "status", "token": token]) { [weak self] body in
            self?.apply(body)
            self?.schedule()
        }
        observePower()
        observeNetwork()
    }

    /// Reported once, from the second answer rather than from startup: AppKit
    /// lays the item out on the run loop, and a frame measured before that is
    /// all zeros - which would prove nothing.  Every value here is a plain JSON
    /// type, because a CGRect inside a dictionary is an Objective-C exception
    /// raised inside JSONSerialization, not a Swift error, and `try?` does not
    /// catch it: the helper would die one line after drawing its item.
    private func reportReady() {
        let box = statusItem.button?.window?.frame ?? .zero
        logLine("ready " + (String(data: json([
            "status_item": true,
            "button": statusItem.button != nil,
            "window": statusItem.button?.window != nil,
            "visible": statusItem.button?.window?.isVisible ?? false,
            "frame": [box.origin.x, box.origin.y, box.size.width, box.size.height],
            "menu_items": statusItem.menu?.numberOfItems ?? 0,
            "pid": ProcessInfo.processInfo.processIdentifier,
        ]), encoding: .utf8) ?? "{}"))
    }

    private func schedule() {
        pollTimer?.invalidate()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            guard let self = self, !self.stop else { return }
            self.channel.request(["action": "status", "token": self.token]) { body in self.apply(body) }
        }
    }

    private func observePower() {
        let center = NSWorkspace.shared.notificationCenter
        center.addObserver(forName: NSWorkspace.screensDidSleepNotification, object: nil, queue: .main) { [weak self] _ in
            self?.send("sleep")
        }
        center.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            self?.send("wake")
        }
    }

    private func observeNetwork() {
        monitor.pathUpdateHandler = { [weak self] path in
            let state = path.status == .satisfied ? "satisfied"
                : (path.status == .requiresConnection ? "connecting" : "offline")
            let value = "\(state):\(path.isExpensive):\(path.isConstrained)"
            guard let self = self, value != self.lastNetwork else { return }
            self.lastNetwork = value
            self.send("network", ["status": state, "expensive": path.isExpensive ? 1 : 0])
        }
        monitor.start(queue: queue)
    }

    private func send(_ name: String, _ extra: [String: Any] = [:]) {
        var payload: [String: Any] = ["action": name, "token": token]
        payload.merge(extra) { _, new in new }
        channel.request(payload) { [weak self] body in self?.apply(body) }
    }

    private func apply(_ body: NSDictionary) {
        guard (body["ok"] as? Bool) == true else { return }
        if let title = body["badge"] as? String {
            statusItem.button?.title = title
        }
        rebuild((body["menu"] as? [NSDictionary]) ?? [])
        answers += 1
        if answers == 2 { reportReady() }
        if (body["bye"] as? Bool) == true {
            stop = true
            NSApp.terminate(nil)
        }
    }

    private func rebuild(_ items: [NSDictionary]) {
        let menu = statusItem.menu ?? NSMenu()
        menu.removeAllItems()
        for item in items {
            if (item["separator"] as? Bool) == true {
                menu.addItem(.separator())
                continue
            }
            let entry = NSMenuItem(title: item["label"] as? String ?? "", action: nil, keyEquivalent: "")
            entry.isEnabled = (item["enabled"] as? Bool) ?? true
            entry.state = (item["checked"] as? Bool) == true ? .on : .off
            if let action = item["action"] as? String, let selector = self.selector(for: action) {
                entry.action = selector
                entry.target = self
                if action == "quit" { entry.keyEquivalent = "q" }
            }
            menu.addItem(entry)
        }
        // Logged only when it changes: this is how the application can see that
        // the menu really was built, without a screenshot of the menu bar.
        let signature = "\(menu.numberOfItems):\(menu.items.first?.title ?? "")"
        if signature != rendered {
            rendered = signature
            logLine("rendered " + String(data: json([
                "items": menu.numberOfItems,
                "first": menu.items.first?.title ?? "",
                "enabled": menu.items.filter { $0.isEnabled }.count,
                "actions": menu.items.compactMap { $0.action.map { NSStringFromSelector($0) } },
            ]), encoding: .utf8)!)
        }
    }

    private func selector(for action: String) -> Selector? {
        switch action {
        case "activate": return #selector(activate)
        case "pause": return #selector(pause)
        case "resume": return #selector(resume)
        case "autostart": return #selector(toggleAutostart)
        case "quit": return #selector(quit)
        default: return nil
        }
    }

    @objc private func activate() { send("activate") }
    @objc private func pause() { send("pause") }
    @objc private func resume() { send("resume") }
    @objc private func toggleAutostart() { send("autostart") }
    @objc private func quit() { send("quit") }
}

func argument(_ name: String) -> String {
    let items = CommandLine.arguments
    guard let index = items.firstIndex(of: name), index + 1 < items.count else { return "" }
    return items[index + 1]
}

let socketPath = argument("--socket")
guard !socketPath.isEmpty else {
    FileHandle.standardError.write(Data("proxy-workbench-tray: --socket is required\n".utf8))
    exit(2)
}

do {
    let tray = Tray(channel: try Channel(path: socketPath), token: argument("--token"))
    let app = NSApplication.shared
    app.delegate = tray
    app.run()
} catch {
    FileHandle.standardError.write(Data("proxy-workbench-tray: \(error.localizedDescription)\n".utf8))
    exit(2)
}
'''


def tray_helper_source_digest():
    return hashlib.sha256(TRAY_HELPER_SOURCE.encode('utf-8')).hexdigest()[:16]


def tray_helper_target(layout, digest=None):
    return layout.cache / 'tray' / f'{TRAY_HELPER_NAME}-{digest or tray_helper_source_digest()}'


def ensure_tray_helper(layout, *, platform=None, finder=None, runner=None):
    """The menu bar helper for this machine: packaged, cached or compiled once.

    Returns ``(path, reason)``.  A machine without the Xcode command line tools
    gets ``(None, reason)`` - the application then runs without a menu bar and
    says why, instead of claiming a tray it does not have.
    """
    platform = sys.platform if platform is None else platform
    if platform != 'darwin':
        return None, tr(f'меню-бар не собран для {platform}', f'no menu bar helper for {platform}')
    if frozen():
        packaged = resource_path('tray', TRAY_HELPER_NAME)
        if packaged.is_file() and os.access(packaged, os.X_OK):
            return packaged, tr('меню-бар взят из сборки', 'the menu bar helper came from the bundle')
        return None, tr('в сборке нет helper меню-бара; см. docs/integration/HANDOFF/fix-desktop.md',
                        'the bundle has no menu bar helper; see docs/integration/HANDOFF/fix-desktop.md')
    target = tray_helper_target(layout)
    if target.is_file() and os.access(target, os.X_OK):
        return target, tr('меню-бар уже собран', 'the menu bar helper is already built')
    compiler = (finder or shutil.which)('swiftc')
    if not compiler:
        return None, tr('не найден swiftc: нужен Xcode Command Line Tools',
                        'swiftc is missing: install the Xcode Command Line Tools')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='proxy-workbench-tray-') as folder:
        source = Path(folder) / 'tray.swift'
        source.write_text(TRAY_HELPER_SOURCE, encoding='utf-8')
        staged = Path(folder) / TRAY_HELPER_NAME
        call = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=300))
        try:
            done = call([str(compiler), '-O', '-o', str(staged), str(source)])
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f'swiftc could not be run: {exc}'
        if getattr(done, 'returncode', 1) != 0 or not staged.is_file():
            detail = (getattr(done, 'stderr', '') or '').strip().splitlines()[-1:] or ['unknown error']
            return None, f'swiftc failed: {detail[0]}'
        try:
            shutil.copy2(staged, target)
            os.chmod(target, 0o755)
        except OSError as exc:
            return None, f'the helper could not be cached: {exc}'
    return target, tr('меню-бар собран на этой машине', 'the menu bar helper was built on this machine')


class Tray:
    """The menu bar item, and the helper process that owns it.

    The helper is a child, so it is a candidate for an orphan; three things stop
    it: it exits by itself when the control socket closes, this object terminates
    it on an orderly quit, and a helper left over from a dead instance is reaped
    at the next start.
    """

    def __init__(self, layout, address, token, *, on_event=None, on_spawn=None):
        self.layout = layout
        self.address = address
        self.token = token
        self.on_event = on_event
        self.on_spawn = on_spawn
        self.process = None
        self.path = None
        self.reason = ''
        self.ready = {}
        self.log_path = layout.logs / 'tray.log'
        self.started_at = None
        self._stopped = False
        self._lock = threading.Lock()

    def start(self, *, wait_s=TRAY_READY_TIMEOUT_S):
        """Put the menu bar up without making anything wait for it.

        Compiling the helper and waiting for it to draw its item both happen in
        the background: the interface has to come up in the second the user asked
        for it, and a menu that appears a moment later is not noticed, while a
        window that appears twenty seconds later is.
        """
        thread = threading.Thread(target=self._bring_up, args=(wait_s,),
                                  name='desktop-tray', daemon=True)
        thread.start()
        return self.status()

    def _bring_up(self, wait_s):
        path, reason = ensure_tray_helper(self.layout)
        with self._lock:
            if self._stopped:
                return
            self.reason = reason
        if path is None:
            return
        if not self.socket_path:
            with self._lock:
                self.reason = tr('нет локального сокета для меню-бара на этой платформе',
                                 'this platform has no local socket for the menu bar')
            return
        self.path = path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open('ab') as log:
            log.write(f'--- {time.strftime("%Y-%m-%d %H:%M:%S")} pid={os.getpid()} {path}\n'.encode('utf-8'))
            try:
                process = subprocess.Popen(
                    [str(path), '--socket', str(self.socket_path), '--token', self.token],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    cwd=str(self.layout.data), start_new_session=True)
            except OSError as exc:
                with self._lock:
                    self.reason = f'the menu bar helper could not be started: {exc}'
                return
        with self._lock:
            # A quit that arrived while the helper was still compiling must not
            # leave a fresh menu bar item behind with nobody left to close it.
            if self._stopped:
                _terminate(process)
                return
            self.process = process
        self.started_at = time.time()
        if self.on_spawn is not None:
            with contextlib.suppress(Exception):
                self.on_spawn(process.pid)
        self._await_ready(wait_s)

    @property
    def socket_path(self):
        try:
            kind, target, _ = _parse_control_address(self.address)
        except (TypeError, ValueError):
            return ''
        return target if kind == 'unix' else ''

    def _await_ready(self, wait_s):
        """Wait for the helper to report what it created, so "the tray is up" is a fact."""
        deadline = time.monotonic() + float(wait_s)
        offset = 0
        report = None
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                report = dict(error=f'the menu bar helper exited with {self.process.returncode}')
                break
            try:
                with self.log_path.open('rb') as handle:
                    handle.seek(offset)
                    chunk = handle.read()
                    offset = handle.tell()
            except OSError:
                chunk = b''
            for line in chunk.decode('utf-8', 'replace').splitlines():
                marker = line.find('ready ')
                if marker < 0:
                    continue
                try:
                    report = json.loads(line[marker + len('ready '):])
                except ValueError:
                    report = dict(error='the menu bar helper reported unreadable state')
                break
            if report is not None:
                break
            time.sleep(0.2)
        if report is None:
            report = dict(error=tr('меню-бар не ответил за отведённое время',
                                   'the menu bar helper did not answer in time'))
        with self._lock:
            self.ready = report
            if not report.get('status_item'):
                # Not a reason to kill a working background: say what it said.
                self.reason = report.get('error') or tr('меню-бар не подтвердил элемент',
                                                        'the menu bar item was not confirmed')
        journal(self.layout, 'tray.ready', **{k: v for k, v in report.items() if k != 'frame'})

    def reap(self, previous=None):
        """Stop a menu bar item left behind by an instance that is gone.

        The helper exits by itself when the control socket closes, so this is
        the second line of defence, and it only ever signals a pid that the dead
        instance recorded as its own helper.
        """
        pid = int(getattr(previous, 'tray_pid', 0) or 0)
        if not pid or (self.process is not None and pid == self.process.pid):
            return False
        if not stop_process(pid):
            return False
        journal(self.layout, 'tray.reaped', pid=pid)
        return True

    def event(self, name, body=None):
        """Forward a system event the helper reported to the observer."""
        if self.on_event is not None:
            self.on_event(name, body or {})
        return name

    def stop(self, timeout=5.0):
        with self._lock:
            self._stopped = True
            process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return False
        return _terminate(process, timeout=timeout)

    def status(self):
        with self._lock:
            reason, ready = self.reason, dict(self.ready)
        alive = self.process is not None and self.process.poll() is None
        return dict(supported=self.path is not None, running=bool(alive),
                    pid=(self.process.pid if alive and self.process else 0),
                    path=str(self.path) if self.path else None,
                    reason=reason, ready=ready, started_at=self.started_at)


def _terminate(process, timeout=5.0):
    """End one of our children, escalating only if it refuses to go."""
    if process is None or process.poll() is not None:
        return False
    with contextlib.suppress(OSError):
        process.terminate()
    try:
        process.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(OSError):
        process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        process.wait(timeout=2.0)
    return True


def stop_process(pid, timeout=5.0):
    """Terminate a process we started, escalating only if it refuses to go."""
    if not pid or pid <= 1:
        return False
    try:
        done = subprocess.run(['ps', '-p', str(pid), '-o', 'command='],
                              capture_output=True, text=True, timeout=timeout)
        command = (done.stdout or '').strip()
    except (OSError, subprocess.SubprocessError):
        command = ''
    if command and TRAY_HELPER_NAME not in command and 'proxy_workbench' not in command:
        # The pid was reused by something that is none of our business.
        return False
    for number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, number)
        except OSError:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            time.sleep(0.1)
    return False


# --------------------------------------------------------------------------
# the data folder an earlier build left behind
# --------------------------------------------------------------------------

def _fingerprint(folder):
    """A cheap identity of a folder's contents, so "nothing changed" is a fact."""
    count = 0
    size = 0
    newest = 0.0
    try:
        for path in Path(folder).rglob('*'):
            try:
                info = path.stat()
            except OSError:
                continue
            count += 1
            size += info.st_size
            newest = max(newest, info.st_mtime)
    except OSError:
        return None
    return dict(files=count, bytes=size, newest=newest)


def run_startup_migration(layout, *, environ=None, executable=None, package=None, now=None):
    """Move a data folder an earlier build left behind - once, and say so.

    The old folder is never deleted and a file that already differs in the new
    one is never overwritten, so the worst case is a folder the user still has
    and a receipt that names which copy is live.  It repeats only when the old
    folder actually changed, which is why the record is kept next to the data.
    """
    environ = os.environ if environ is None else environ
    # A custom data folder is a separate workspace, not permission to import
    # the current checkout or another installation into it.
    previous = environ.get('PROXY_WORKBENCH_PREVIOUS_DATA')
    if previous:
        roots = (Path(previous).expanduser(),)
    elif layout.mode == 'per-user':
        roots = legacy_data_roots(layout, environ=environ, executable=executable, package=package)
    else:
        roots = ()
    if not roots:
        return dict(applied=False, reason=tr('старой папки нет', 'there is no legacy folder'))
    preferences = read_preferences(layout)
    record = preferences['migrations']
    stamp = time.time() if now is None else now
    performed = []
    for root in roots:
        fingerprint = _fingerprint(root)
        if fingerprint is None:
            continue
        known = record.get(str(root))
        if isinstance(known, dict) and known.get('fingerprint') == fingerprint:
            continue
        plan = plan_migration(layout, source=root, now=now)
        if plan.empty:
            record[str(root)] = dict(fingerprint=fingerprint, at=stamp, files=0, applied=False)
            continue
        result = apply_migration(plan, execute=True)
        performed.append(dict(source=str(result.source), files=len(result.copied),
                              bytes=plan.total_bytes, conflicts=len(result.skipped),
                              errors=list(result.errors), backup=str(result.backup) if result.backup else None))
        record[str(root)] = dict(fingerprint=fingerprint, at=stamp, files=len(result.copied),
                                 applied=result.applied, conflicts=len(result.skipped),
                                 errors=list(result.errors))
    if not performed:
        return dict(applied=False, reason=tr('старые папки не менялись', 'the legacy folders did not change'))
    update_preferences(layout, migrations=record)
    journal(layout, 'migration.applied', moved=performed)
    return dict(applied=True, moved=performed,
                reason=tr('данные перенесены из старой папки', 'the data was moved from the old folder'))


def interface_holder(layout):
    """True when something else already serves this data folder.

    ``gui.App`` holds ``gui-instance.lock`` for its whole life, so a held lock
    means the interface is up - including when it was started the old way,
    straight from ``gui.py``, which knows nothing about this host and writes no
    instance record of its own.  A free lock is a fact, not a guess, which is
    why this is a lock and not a port probe.

    A folder that cannot be locked at all answers False: the interface will
    report the real problem itself, in a better sentence than this one.
    """
    path = layout.data / 'gui-instance.lock'
    try:
        handle = path.open('r+b') if path.exists() else path.open('w+b')
    except OSError:
        return False
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b'\0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return True
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            with contextlib.suppress(OSError):
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        with contextlib.suppress(OSError):
            handle.close()
    return False


def published_url(layout):
    """The address a running interface recorded, or an empty string."""
    address = _load_json(layout.data / 'gui-address.json', {})
    port = address.get('port') if isinstance(address, dict) else None
    return f'http://127.0.0.1:{port}/' if type(port) is int and 1 <= port <= 65535 else ''


# --------------------------------------------------------------------------
# the host: one process, one instance, one lifecycle
# --------------------------------------------------------------------------

class DesktopHost:
    """Everything that has to be true for the product to work as a desktop app.

    It holds no product state.  It reads what the interface publishes, asks the
    interface to act, and is answerable for the things only this process can
    know: that the data folder an earlier build left behind was moved, that the
    machine slept, and that a second launch must not become a second copy.
    """

    def __init__(self, layout, argv=(), *, platform=None, tray=True):
        self.layout = layout
        self.argv = [item for item in argv if item not in HOST_FLAGS]
        self.background = '--background' in argv
        self.tray_requested = bool(tray) and '--no-tray' not in argv
        self.platform = sys.platform if platform is None else platform
        self.started_at = time.time()
        self.token = secrets.token_hex(16)
        self.lock = InstanceLock(layout)
        self.server = None
        self.app = None
        self.instance = None
        self.previous = None
        self.control = None
        self.tray = None
        self.observer = None
        self.url = ''
        self.notice = None
        self.platform_state = dict(last_wake=None, last_sleep=None, slept_s=0.0,
                                   network=list(network_fingerprint()), network_changed_at=None,
                                   network_state='')
        self._quitting = False
        self._network_reports = 0
        self._wake_lock = threading.Lock()

    # -- the instance ------------------------------------------------------

    def claim(self) -> bool:
        """Become the application, or report that one is already running."""
        if not self.lock.acquire():
            return False
        # Read before publishing: whatever is in the file now belongs to a
        # process that is gone, and may have left a menu bar item behind.
        self.previous = read_instance(self.layout)
        self.control = ControlServer(self.layout, self.handle, platform=self.platform, token=self.token)
        address = self.control.start()
        self.instance = self.lock.publish(Instance(
            pid=os.getpid(), control=address, token=self.token, url='',
            started_at=self.started_at, version=_product_version(), platform=self.platform))
        journal(self.layout, 'instance.claimed', pid=os.getpid(), control=address)
        return True

    def running_instance(self):
        return read_instance(self.layout)

    def forward(self) -> int:
        """A second launch reaches the first one and leaves immediately.

        It never becomes a second copy: the lock is held by a live process, and
        a process that exits right after forwarding cannot become an orphan.
        When the control channel is not answering, the page address the first
        instance published is still the honest answer - better than starting a
        rival that would fight over the same database.
        """
        running = self.running_instance()
        if running is not None:
            answer = control_request(running.control, running.token,
                                     dict(action='activate', source='second-launch', argv=self.argv,
                                          open_browser=not self.background and '--no-browser' not in self.argv))
            if answer.get('ok'):
                url = running.url or str(answer.get('url') or '')
                print(tr(f'Приложение уже запущено: {url}',
                         f'The application is already running: {url}'), flush=True)
                journal(self.layout, 'instance.forwarded', pid=running.pid, url=url)
                return 0
        url = published_url(self.layout)
        if not self.background and '--no-browser' not in self.argv:
            self.open_interface(url or None)
        print(tr(f'Приложение уже запущено{": " + url if url else ""}; повторный запуск ничего не изменил.',
                 f'The application is already running{": " + url if url else ""}; this launch changed nothing.'),
              flush=True)
        journal(self.layout, 'instance.forwarded', pid=(running.pid if running else 0), url=url,
                control=False)
        return 0

    def defer_to_interface(self) -> int:
        """Stand down for an instance that was started before this host existed.

        An older build holding the same data folder is a working application,
        not a rival to be replaced: the launch opens its page and ends, without
        a menu bar item that would appear and vanish again.
        """
        if not interface_holder(self.layout):
            return -1
        url = published_url(self.layout)
        if not self.background and '--no-browser' not in self.argv:
            self.open_interface(url or None)
        print(tr(f'Приложение уже запущено{": " + url if url else ""} (эта копия закрыта, данные те же).',
                 f'The application is already running{": " + url if url else ""} '
                 f'(this copy closed itself, same data).'), flush=True)
        journal(self.layout, 'instance.deferred', url=url)
        return 0

    def release(self):
        if self.tray is not None:
            self.tray.stop()
        if self.observer is not None:
            self.observer.stop()
        if self.control is not None:
            self.control.stop()
        self.lock.release()
        journal(self.layout, 'instance.released', pid=os.getpid())

    # -- the interface -----------------------------------------------------

    @contextlib.contextmanager
    def gui_handle(self):
        """Run the interface and keep the handle its own ``main`` would hide.

        ``gui.main`` blocks and owns the shutdown of its listeners, which is
        exactly the behaviour that has to survive.  What it does not return is
        the server, and without the server this process cannot show a status,
        stop a run or shut down in bounded time.  One narrow substitution gets
        the handle without touching a line of the interface, and it is undone
        before the interface returns.
        """
        from . import gui
        original = gui.make_server

        def capture(data, port=0, **options):
            server = original(data, port, **options)
            self.adopt(server)
            return server

        gui.make_server = capture
        try:
            yield
        finally:
            gui.make_server = original

    def adopt(self, server):
        self.server = server
        self.app = getattr(server, 'app', None)
        url = f'http://127.0.0.1:{server.server_port}/'
        self.url = url
        if self.instance is not None:
            self.instance = self.lock.publish(Instance(
                pid=self.instance.pid, control=self.instance.control, token=self.token, url=url,
                started_at=self.started_at, tray_pid=(self.tray.process.pid if self.tray and self.tray.process else 0),
                version=self.instance.version, platform=self.platform))
        journal(self.layout, 'instance.address', url=url)

    # -- status ------------------------------------------------------------

    def describe_run(self):
        """What the interface itself says is happening, read defensively.

        Every read here can fail - the folder may be mid-write, the database may
        be locked by a check, the secret store may be locked by the OS - and a
        background status line may never be the reason the background work
        stops.  So each source is isolated and a failure becomes a message.
        """
        state = {}
        if self.app is not None:
            try:
                state = self.app.state() or {}
            except Exception as exc:
                state = dict(error=f'{type(exc).__name__}: {exc}')
        progress = dict(state.get('progress') or {})
        job = dict(state.get('job') or {})
        export = dict(state.get('export') or {})
        if not progress and self.app is None:
            progress = _load_json(self.layout.data / 'gui-progress.json', {})
            job = _load_json(self.layout.data / 'gui-job.json', {})
        running = bool(state.get('running')) or bool(progress.get('pid'))
        phase = str(progress.get('phase') or ('running' if running else 'idle'))
        if job.get('stopping'):
            phase = 'stopping'
        if not running and job.get('exit_code') not in (0, 130, None):
            phase = 'error'
        return dict(running=running, phase=phase, job=job, progress=progress, export=export,
                    gateway=state.get('gateway'), error=state.get('error'))

    def describe_vault(self):
        """Can the secret store be read right now, and what does the user do?

        A locked keychain must never take the background down, so this only
        reports.  ``action`` is the text the secret layer itself carries: the
        fix is a user action, not a retry.
        """
        from . import secrets as secretstore
        try:
            vault = secretstore.open_vault('auto')
        except secretstore.SecretError as exc:
            return dict(state='unavailable', reason=exc.describe(), action=exc.action or '')
        except Exception as exc:
            return dict(state='unavailable', reason=f'{type(exc).__name__}: {exc}', action='')
        name = getattr(vault, 'name', 'unknown')
        if name == 'os-vault':
            try:
                refs = vault.refs()
            except secretstore.SecretError as exc:
                return dict(state='locked', reason=exc.describe(), action=exc.action or '')
            except Exception as exc:
                return dict(state='unavailable', reason=f'{type(exc).__name__}: {exc}', action='')
            return dict(state='ready', reason=tr('системное хранилище доступно',
                                                'the system secret store is available'),
                        references=len(refs))
        return dict(state='session', reason=tr('секреты только на время сеанса',
                                               'secrets live only for this session'),
                    references=len(getattr(vault, 'refs', lambda: [])() or []))

    def status(self):
        run = self.describe_run()
        export = run['export'] or {}
        available = int(export.get('available') or 0)
        total = int(export.get('rows_total') or 0)
        gateway = run.get('gateway') or {}
        headline = _headline(run, available)
        autostart = autostart_status(self.layout, platform=self.platform)
        result = dict(
            ok=True,
            badge=str(available) if available else '—',
            headline=headline,
            url=self.url,
            pid=os.getpid(),
            started_at=self.started_at,
            run=dict(running=run['running'], phase=run['phase'], job=run['job'],
                     progress=run['progress'], error=run.get('error')),
            pool=dict(available=available, total=total,
                      in_use=int(gateway.get('in_use') or 0),
                      expired=int(export.get('expired_count') or 0),
                      stale=bool(export.get('stale')), published=bool(export.get('published')),
                      state=export.get('state') or ('missing' if not export else 'ok')),
            gateway=dict(running=bool(gateway), address=gateway.get('address') or '',
                         in_use=int(gateway.get('in_use') or 0),
                         mobile_ready=bool(gateway.get('mobile_ready'))),
            vault=self.describe_vault(),
            platform=dict(self.platform_state),
            autostart=autostart.as_dict(),
            tray=(self.tray.status() if self.tray is not None
                  else dict(supported=False, running=False, reason=tr('меню-бар не запрошен',
                                                                      'the menu bar was not requested'))),
            migration=self.notice,
            layout=self.layout.as_dict(),
        )
        result['menu'] = tray_menu(result)
        return result

    # -- actions the menu bar and a second launch can ask for ---------------

    def handle(self, request) -> dict:
        """One control request in, one answer out.  Never raises."""
        event = str(request.get('event') or '')
        if event:
            # A line the menu bar helper sent because the system told it
            # something: a lid closed, a machine woke, a network path changed.
            self.tray_event(event, request)
            return self.status()
        action = str(request.get('action') or 'status')
        if action in ('status', 'hello'):
            return self.status()
        if action == 'activate':
            if request.get('open_browser', True):
                self.open_interface()
            return dict(self.status(), bye=False)
        if action in ('pause', 'stop'):
            return dict(self.status(), **self.pause_run())
        if action in ('resume', 'start'):
            return dict(self.status(), **self.resume_run())
        if action == 'autostart':
            wanted = request.get('enabled')
            if wanted is None:
                wanted = not autostart_status(self.layout, platform=self.platform).enabled
            return dict(self.status(), **self.set_autostart(bool(wanted)))
        if action == 'quit':
            self.quit(str(request.get('reason') or 'menu'))
            return dict(self.status(), bye=True)
        if action == 'state':
            return self.status()
        return dict(ok=False, error=f'unknown action: {action}')

    def open_interface(self, url=None):
        """Show the page.  This is not what starts or stops anything."""
        import webbrowser
        target = url or self.url
        if not target:
            return dict(opened=False, reason=tr('интерфейс ещё не запущен',
                                                'the interface is not up yet'))
        try:
            webbrowser.open(target)
        except Exception as exc:
            return dict(opened=False, reason=f'{type(exc).__name__}: {exc}')
        return dict(opened=True, url=target)

    def pause_run(self):
        """Stop the check that is running; the application stays up."""
        if self.app is None:
            return dict(paused=False, reason=tr('интерфейс ещё не запущен',
                                                'the interface is not up yet'))
        try:
            answer = self.app.stop()
        except Exception as exc:
            return dict(paused=False, reason=f'{type(exc).__name__}: {exc}')
        journal(self.layout, 'run.paused', stopping=bool(answer.get('stopping')))
        return dict(paused=bool(answer.get('stopping')), reason='')

    def resume_run(self):
        """Start a check with the settings the user last chose."""
        if self.app is None:
            return dict(resumed=False, reason=tr('интерфейс ещё не запущен',
                                                 'the interface is not up yet'))
        try:
            job = self.app.start({'action': 'run'})
        except Exception as exc:
            return dict(resumed=False, reason=f'{type(exc).__name__}: {exc}')
        journal(self.layout, 'run.resumed', job=job.get('id') if isinstance(job, dict) else None)
        return dict(resumed=True, reason='')

    def set_autostart(self, enabled):
        try:
            status = set_autostart(self.layout, enabled, platform=self.platform)
        except DesktopError as exc:
            return dict(autostart=None, reason=str(exc))
        except OSError as exc:
            return dict(autostart=None, reason=f'{type(exc).__name__}: {exc}')
        return dict(autostart=status.as_dict(), reason=status.reason)

    def quit(self, reason='menu'):
        """Say who is still connected, stop the listeners, let the interface return.

        The bounded shutdown of the API, the rotating proxy and the worker stays
        where it already is - inside the interface's own ``finally``.  All this
        has to do is count the clients that are about to lose their connection
        and ask the server to stop serving.
        """
        if self._quitting:
            return False
        self._quitting = True
        clients = self.connected_clients()
        journal(self.layout, 'instance.quit', reason=reason, url=self.url, **clients)
        print(tr(f'Выход. Подключено клиентов: {clients["clients"]}. '
                 f'Останавливаю слушатели…',
                 f'Quit. Connected clients: {clients["clients"]}. Stopping the listeners…'),
              flush=True)
        if self.tray is not None:
            self.tray.stop()
        server = self.server
        if server is None:
            return True
        threading.Thread(target=_shutdown, args=(server,), name='desktop-shutdown', daemon=True).start()
        return True

    def connected_clients(self):
        """Who is talking to the application right now, before it goes away.

        A user who quits with a phone on the rotating proxy deserves to be told
        that the phone is about to be cut off.  Both numbers come from state the
        product already keeps, so nothing here can fail for a new reason.
        """
        gateway, in_use = None, 0
        try:
            run = self.describe_run()
            gateway = run.get('gateway') or None
            in_use = int((gateway or {}).get('in_use') or 0)
        except Exception as exc:
            gateway = {'error': f'{type(exc).__name__}: {exc}'}
        return dict(clients=in_use, gateway_running=bool(gateway),
                    gateway_address=(gateway or {}).get('address') or '',
                    workers=1 if (self.app is not None and self.app.running()) else 0)

    # -- sleep, wake, network ---------------------------------------------

    def start_platform(self):
        """Watch the machine and put the menu bar up."""
        self.observer = PlatformObserver(self.on_sleep, self.on_wake, self.on_network,
                                         platform=self.platform)
        self.observer.start()
        if self.tray_requested:
            self.tray = Tray(self.layout, self.control.address if self.control else '',
                             self.token, on_event=self.tray_event, on_spawn=self.tray_spawned)
            self.tray.reap(self.previous)
            self.tray.start()
        return self

    def tray_spawned(self, pid):
        """Publish the helper's pid, so a later launch can reap it if we die."""
        if self.instance is None:
            return
        self.instance = self.lock.publish(Instance(
            pid=self.instance.pid, control=self.instance.control, token=self.token,
            url=self.url, started_at=self.started_at, tray_pid=int(pid or 0),
            version=self.instance.version, platform=self.platform))
        journal(self.layout, 'tray.spawned', pid=pid)
    def tray_event(self, name, body):
        if self.observer is not None:
            self.observer.native_event(name, body)

    def on_sleep(self, source='clock'):
        with self._wake_lock:
            self.platform_state['last_sleep'] = time.time()
            marked = mark_jobs_suspended(self.layout)
        journal(self.layout, 'platform.sleep', source=source, jobs=len(marked['jobs']),
                errors=marked['errors'])
        return marked

    def on_wake(self, slept_s=0.0, source='clock'):
        """The resume, told to the two layers that have to hear about it."""
        with self._wake_lock:
            self.platform_state['last_wake'] = time.time()
            self.platform_state['slept_s'] = round(
                float(self.platform_state.get('slept_s') or 0.0) + max(0.0, float(slept_s or 0.0)), 3)
            jobs_report = close_job_sleeps(self.layout)
            tick_report = self.tick_after_wake()
        record = dict(source=source, slept_s=float(slept_s or 0.0), jobs=jobs_report,
                      schedules=tick_report, network_changed_at=self.platform_state.get('network_changed_at'))
        journal(self.layout, 'platform.wake', **record)
        return record

    def tick_after_wake(self):
        """Coalesce missed intervals and submit them through the same runner."""
        try:
            return self.schedule_tick(woke=True)
        except Exception as exc:
            return dict(error=f'{type(exc).__name__}: {exc}')

    def schedule_tick(self, *, woke=False):
        """Wake and headless timers share one durable dispatcher."""
        from .schedule_runtime import runtime_for
        return runtime_for(self.layout.data).tick(woke=woke)

    def on_network(self, previous=None, current=None, status='', source='address'):
        """A new network path: measurements from the old one are not this one.

        Nothing is invalidated and nothing is deleted.  A result is a fact about
        the network that produced it, so the change is recorded next to the data
        and shown in the menu; rewriting the history here would destroy the very
        evidence that says which network a row came from.

        The first report a platform observer makes is its current state, not a
        change, so it is recorded and does not raise a "the network changed"
        alarm on every start.
        """
        initial = source == 'system' and self._network_reports == 0
        self._network_reports += 1
        changed = current is not None and list(current) != list(self.platform_state.get('network') or [])
        if current is not None:
            self.platform_state['network'] = list(current)
        if status:
            self.platform_state['network_state'] = str(status)
        if not initial and (changed or (status and source == 'system')):
            self.platform_state['network_changed_at'] = time.time()
        journal(self.layout, 'platform.network', source=source, status=status, initial=initial,
                previous=list(previous or ()), current=list(current or ()))
        return self.platform_state

    # -- startup -----------------------------------------------------------

    def announce_migration(self):
        """Move an old data folder once, and tell the user it happened."""
        try:
            report = run_startup_migration(self.layout, package=None)
        except Exception as exc:
            report = dict(applied=False, reason=f'{type(exc).__name__}: {exc}')
        if not report.get('applied'):
            return report
        self.notice = dict(at=time.time(), reason=report['reason'], moved=report['moved'])
        for item in report['moved']:
            print(tr(f'Данные перенесены из {item["source"]}: файлов {item["files"]}, '
                     f'резервная копия {item["backup"] or "не создана"}.',
                     f'Data moved from {item["source"]}: {item["files"]} files, '
                     f'backup {item["backup"] or "not created"}.'), flush=True)
        if report['moved'][0]['conflicts']:
            print(tr('Файлы, которые уже отличались в новой папке, оставлены как есть.',
                     'Files that already differed in the new folder were left alone.'), flush=True)
        return report


def _shutdown(server):
    """Ask an HTTP server to stop, from a thread that is not serving it."""
    with contextlib.suppress(Exception):
        server.shutdown()


def _product_version():
    try:
        from .branding import PRODUCT_VERSION
        return PRODUCT_VERSION
    except ImportError:
        return ''


def _headline(run, available):
    """The one line the menu bar shows: what is happening, in this order."""
    if run.get('error'):
        return tr('фон: ошибка чтения состояния', 'background: the state could not be read')
    phase = run['phase']
    progress = run['progress'] or {}
    done, total = progress.get('done'), progress.get('total')
    if run['running']:
        if phase == 'stopping':
            return tr('останавливается…', 'stopping…')
        numbers = f' {done}/{total}' if isinstance(done, int) and isinstance(total, int) and total else ''
        labels = {'scanning': tr('проверка прокси', 'checking proxies'),
                  'collecting': tr('сбор источников', 'collecting sources'),
                  'exporting': tr('экспорт', 'exporting'),
                  'starting': tr('запуск', 'starting'),
                  'waiting': tr('ожидание', 'waiting')}
        return labels.get(phase, phase) + numbers
    if phase == 'error':
        return tr('последняя проверка завершилась ошибкой', 'the last check ended with an error')
    if available:
        return tr(f'готов: {available} прокси', f'ready: {available} proxies')
    return tr('готов, проверок ещё не было', 'ready, no check has run yet')


def tray_menu(status):
    """The menu, built here so the helper has no product knowledge at all."""
    run, pool = status['run'], status['pool']
    autostart = status['autostart'] or {}
    platform = status['platform'] or {}
    vault = status['vault'] or {}
    notice = status.get('migration')
    lines = [dict(label=status['headline'], enabled=False)]
    parts = [tr(f'в пуле: {pool["available"]}', f'in the pool: {pool["available"]}')]
    if pool['in_use']:
        parts.append(tr(f'занято {pool["in_use"]}', f'{pool["in_use"]} in use'))
    if pool['expired']:
        parts.append(tr(f'просрочено {pool["expired"]}', f'{pool["expired"]} expired'))
    lines.append(dict(label=' · '.join(parts), enabled=False))
    extra = []
    if platform.get('last_wake'):
        extra.append(tr(f'сон {platform.get("slept_s") or 0:.0f} с',
                        f'slept {platform.get("slept_s") or 0:.0f} s'))
    if platform.get('network_changed_at'):
        extra.append(tr('сеть сменилась', 'the network changed'))
    if vault.get('state') in ('locked', 'unavailable'):
        extra.append(tr('хранилище секретов недоступно', 'the secret store is unavailable'))
    if extra:
        lines.append(dict(label=' · '.join(extra), enabled=False))
    if notice and notice.get('reason'):
        lines.append(dict(label=notice['reason'], enabled=False))
    menu = list(lines) + [dict(separator=True),
                          dict(label=tr('Открыть интерфейс', 'Open the interface'),
                               action='activate', enabled=bool(status.get('url'))),
                          dict(label=tr('Пауза проверки', 'Pause the check'), action='pause',
                               enabled=bool(run.get('running'))),
                          dict(label=tr('Запустить проверку', 'Start the check'), action='resume',
                               enabled=not run.get('running') and run.get('phase') != 'error'),
                          dict(label=tr('Запускать при входе', 'Start at login'), action='autostart',
                               checked=bool(autostart.get('enabled')), enabled=bool(autostart.get('supported'))),
                          dict(separator=True),
                          dict(label=tr('Выход', 'Quit'), action='quit')]
    return menu


def main(argv=None):
    """Start the browser GUI on resolved paths, with this process owning it.

    The interface is still the product's user interface and it is still a page
    in a browser; what this host adds is the part a page cannot be: closing the
    page leaves the background work alone, quitting stops it, a second launch
    reaches the one that is already running, and the login item exists only
    because the user turned it on.

    Every argument this host does not understand is passed on unchanged, so the
    browser and headless paths keep working exactly as before.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    # Resolve --data before taking the instance lock. Otherwise two folders
    # share one host, while its interface and journal point at different data.
    data_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    data_parser.add_argument('--data')
    options, host_argv = data_parser.parse_known_args(argv)
    if options.data is not None:
        os.environ[DATA_ENV] = str(Path(options.data).expanduser().resolve())
    argv = host_argv
    if argv and argv[0] in ('--print-paths', '--portable', '--update-notice', '--autostart',
                            '--autostart-status', '--status', '--migrate-preview'):
        return _host_command(argv)
    if any(token in ('-h', '--help', '--version') for token in argv):
        # `--help` and `--version` ask a question and change nothing, so they
        # must be answered whether or not the application is already running.
        # `__main__` routes `--data ... --help` here, because `--data` is one of
        # this host's own flags; without this a running instance swallowed the
        # question and answered "already running", which is a true sentence
        # about the wrong thing.  The parser prints and exits, so the lock is
        # never taken.
        from . import proxytool
        return proxytool.main(argv)
    layout = resolve_layout()
    try:
        ensure_layout(layout)
    except LayoutError as exc:
        print(exc, file=sys.stderr, flush=True)
        return 2
    os.environ.update({DATA_ENV: str(layout.data), CACHE_ENV: str(layout.cache), LOGS_ENV: str(layout.logs)})
    host = DesktopHost(layout, argv)
    if not host.claim():
        return host.forward()
    try:
        deferred = host.defer_to_interface()
        if deferred >= 0:
            return deferred
        host.announce_migration()
        host.start_platform()
        passed = list(host.argv)
        if host.background and '--no-browser' not in passed:
            passed.insert(0, '--no-browser')
        with host.gui_handle():
            from . import gui
            return gui.main(['--data', str(layout.data), *passed])
    finally:
        host.release()


def _host_command(argv):
    from .branding import PRODUCT_VERSION
    command = argv[0]
    layout = resolve_layout()
    if command == '--print-paths':
        print(json.dumps(describe_environment(layout), ensure_ascii=False, indent=2), flush=True)
        return 0
    if command == '--portable':
        try:
            marker = write_portable_marker(portable_root())
        except OSError as exc:
            print(tr(f'Не удалось включить portable mode: {exc}', f'Cannot enable portable mode: {exc}'),
                  file=sys.stderr, flush=True)
            return 2
        print(tr(f'Portable mode включён: {marker}', f'Portable mode enabled: {marker}'), flush=True)
        return 0
    if command == '--autostart-status':
        print(json.dumps(autostart_status(layout).as_dict(), ensure_ascii=False, indent=2), flush=True)
        return 0
    if command == '--autostart':
        wanted = 'off' not in argv[1:] and 'false' not in argv[1:]
        try:
            status = set_autostart(layout, wanted)
        except DesktopError as exc:
            print(str(exc), file=sys.stderr, flush=True)
            return 2
        print(json.dumps(status.as_dict(), ensure_ascii=False, indent=2), flush=True)
        return 0
    if command == '--migrate-preview':
        sources = legacy_data_roots(layout)
        print(json.dumps(dict(sources=[str(path) for path in sources],
                              plans=[plan_migration(layout, source=path).as_dict() for path in sources]),
                         ensure_ascii=False, indent=2), flush=True)
        return 0
    if command == '--status':
        running = read_instance(layout)
        if running is None:
            print(json.dumps(dict(running=False, data=str(layout.data)), ensure_ascii=False, indent=2),
                  flush=True)
            return 1
        answer = control_request(running.control, running.token, dict(action='status'))
        answer.pop('layout', None)
        print(json.dumps(answer, ensure_ascii=False, indent=2, default=str), flush=True)
        return 0 if answer.get('ok') else 2
    location = os.environ.get(UPDATE_MANIFEST_ENV)
    if not location:
        print(tr(f'Укажите файл манифеста обновления или переменную {UPDATE_MANIFEST_ENV}.',
                 f'Pass an update manifest file or set {UPDATE_MANIFEST_ENV}.'), file=sys.stderr, flush=True)
        return 2
    try:
        manifest = read_manifest(location)
        notice = find_update(PRODUCT_VERSION, manifest)
        compatibility = schema_compatible(layout.data, manifest)
    except UpdateError as exc:
        print(exc, file=sys.stderr, flush=True)
        return 2
    if notice is None:
        print(json.dumps(dict(current_version=PRODUCT_VERSION, available=False,
                              compatibility=compatibility.as_dict()), ensure_ascii=False, indent=2), flush=True)
        return 0
    print(json.dumps(dict(notice=notice.as_dict(), compatibility=compatibility.as_dict()),
                     ensure_ascii=False, indent=2), flush=True)
    return 0


# --------------------------------------------------------------------------
# shared file helpers
# --------------------------------------------------------------------------

def _same_content(left, right):
    """True when two files are byte-identical.

    Size alone is not enough: two settings files of the same length with
    different content are a conflict, and overwriting one of them would lose
    whichever the user wrote last.
    """
    left, right = Path(left), Path(right)
    if left.stat().st_size != right.stat().st_size:
        return False
    return sha256_file(left) == sha256_file(right)


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False)
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(handle.name, path)
    except BaseException:
        _unlink(handle.name)
        raise


def _unlink(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


def _copy_tree(source, destination, database=None):
    """Copy a data folder, taking the database through the SQLite backup API.

    Copying a live WAL database file by file can produce a torn copy, so the
    main database is snapshotted by SQLite and the side files are then ignored.
    Returns the backup path, or None when nothing could be written.
    """
    source, destination = Path(source), Path(destination)
    if not source.is_dir():
        return None
    try:
        destination.mkdir(parents=True, exist_ok=True)
        for path in sorted(source.rglob('*')):
            if not path.is_file() or path.is_symlink():
                continue
            if database is not None and str(path) in {str(database) + ending
                                                      for ending in ('-wal', '-shm', '-journal')}:
                continue
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            if database is not None and path == Path(database):
                _snapshot_database(path, target)
            else:
                shutil.copy2(path, target)
    except OSError:
        return None
    return destination


def _snapshot_database(source, target):
    try:
        with closing(sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)) as reader, \
                closing(sqlite3.connect(target)) as writer:
            reader.backup(writer)
    except sqlite3.Error:
        shutil.copy2(source, target)


def _restore_program(target, previous):
    if not Path(previous).is_file():
        return False
    try:
        os.replace(Path(previous), Path(target))
    except OSError:
        return False
    return True


# Last, because ``python -m proxy_workbench.desktop`` runs this file as
# ``__main__`` and every helper above has to exist before ``main`` is called.
if __name__ == '__main__':
    raise SystemExit(main())
