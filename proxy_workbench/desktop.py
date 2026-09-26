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
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
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
FALLBACK_SCHEMA_VERSION = 15
UPDATE_STATE_NAME = 'update-state.json'
BACKUP_PREFIX = 'pre-update-'
READ_CHUNK = 1024 * 1024

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
    'signature_of', 'publish_label', 'main',
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
        data = Path(override).expanduser()
        cache = Path(environ.get(CACHE_ENV) or data / 'cache').expanduser()
        logs = Path(environ.get(LOGS_ENV) or data / 'logs').expanduser()
        return Layout(data, cache, logs, 'environment', data, tr(
            f'путь задан переменной {DATA_ENV}', f'path set by {DATA_ENV}'))

    if _portable_requested(environ, root):
        return _portable_layout(root, tr('portable mode включён явно', 'portable mode was requested explicitly'))

    if is_frozen or not is_writable_dir(root):
        data, cache, logs = _per_user_bases(environ, home, platform)
        return Layout(data, cache, logs, 'per-user', data, tr(
            'установленная сборка: папки пользователя',
            'installed build: per-user folders'))

    if _is_checkout(package):
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
    try:
        for relative, _size in plan.items:
            source_file = plan.source / relative
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
# thin host
# --------------------------------------------------------------------------

def main(argv=None):
    """Start the existing browser GUI on resolved paths.

    Every argument this host does not understand is passed on unchanged, so
    the browser and headless paths keep working exactly as before.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ('--print-paths', '--portable', '--update-notice'):
        return _host_command(argv)
    layout = resolve_layout()
    try:
        ensure_layout(layout)
    except LayoutError as exc:
        print(exc, file=sys.stderr, flush=True)
        return 2
    os.environ.update({DATA_ENV: str(layout.data), CACHE_ENV: str(layout.cache), LOGS_ENV: str(layout.logs)})
    from . import gui
    return gui.main(['--data', str(layout.data), *argv])


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
        with closing(sqlite3.connect(f'file:{source}?mode=ro', uri=True)) as reader, \
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
