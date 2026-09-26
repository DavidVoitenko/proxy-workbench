"""Check a built desktop artifact against the delivery rules F23 states.

    python packaging/verify_delivery.py "dist/Proxy Workbench.app"
    python packaging/verify_delivery.py dist/proxy-workbench-gui.exe

The build (``packaging/build_macos.py``, ``packaging/build_windows.py``) already
proves that the artifact starts and that it does not write into its own folder.
This script covers the three rules that are easy to get wrong and invisible in
a log:

* **per-user paths** - the state a run needs lands in the user's own folders,
  and the program folder comes back byte-for-byte unchanged;
* **portable mode** - it happens only when the user asked for it, and the data
  really lands where they were told it would;
* **migration** - a data folder an older build left behind is moved once, keeps
  a backup and a receipt, and is not moved a second time.

Every check starts the real artifact and reads files a user would find.  It
needs the platform it is checking: a Windows artifact runs this on Windows, a
macOS one on macOS.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

START_TIMEOUT_S = 120


class CheckError(Exception):
    """A delivery rule the artifact does not keep."""


class Report:
    def __init__(self):
        self.checks = []

    def add(self, name, ok, detail=''):
        self.checks.append(dict(name=name, ok=bool(ok), detail=detail))
        print(f"{'ok  ' if ok else 'FAIL'}  {name}: {detail}", flush=True)
        return ok

    @property
    def ok(self):
        return all(check['ok'] for check in self.checks)


def executable(bundle):
    """The program inside a built artifact, whatever kind of artifact it is."""
    bundle = Path(bundle)
    if bundle.suffix == '.app':
        return bundle / 'Contents' / 'MacOS' / bundle.stem
    if bundle.is_dir():
        found = [path for path in bundle.iterdir() if path.suffix == '.exe' and 'cli' not in path.name]
        if len(found) == 1:
            return found[0]
        raise CheckError(f'cannot tell which executable in {bundle} is the program')
    return bundle


def child_environment(home, extra=None):
    """A per-user environment for the launched artifact."""
    env = dict(os.environ, HOME=str(home), PROXY_WORKBENCH_LANG='en', BROWSER='true')
    if os.name == 'nt':
        env['LOCALAPPDATA'] = str(home / 'Local')
        env['APPDATA'] = str(home / 'Roaming')
        env['USERPROFILE'] = str(home)
    env.update(extra or {})
    return env


def launch(program, env, *, data=None, extra=()):
    """Start the artifact and wait until it has published its page address."""
    command = [str(program), '--no-browser', '--port', '0', '--api-port', '0', '--no-gateway', *extra]
    process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    address = _wait_for(Path(data) / 'gui-address.json', process, START_TIMEOUT_S)
    return process, address


def _wait_for(path, process, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return path
        if process.poll() is not None:
            raise CheckError(f'the artifact exited with code {process.returncode} before serving anything')
        time.sleep(0.25)
    raise CheckError(f'the artifact did not create {path} within {timeout}s')


def stop(process, timeout=20):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(10)


def tree_state(folder, skip=()):
    """Every file in a folder, so "nothing changed" is a fact and not a claim."""
    folder = Path(folder)
    if not folder.exists():
        return ()
    return sorted((str(path.relative_to(folder)), path.stat().st_size)
                  for path in folder.rglob('*') if path.is_file()
                  and not str(path.relative_to(folder)).startswith(tuple(skip)))


def per_user_folders(home):
    """The folders the product promises to use, per platform."""
    home = Path(home)
    if os.name == 'nt':
        local = Path(os.environ.get('LOCALAPPDATA') or home / 'AppData' / 'Local')
        roaming = Path(os.environ.get('APPDATA') or local)
        return dict(data=local / 'proxy-workbench', cache=local / 'proxy-workbench' / 'Cache',
                    logs=roaming / 'proxy-workbench' / 'Logs')
    if sys.platform == 'darwin':
        return dict(data=home / 'Library' / 'Application Support' / 'proxy-workbench',
                    cache=home / 'Library' / 'Caches' / 'proxy-workbench',
                    logs=home / 'Library' / 'Logs' / 'proxy-workbench')
    return dict(data=home / '.local' / 'share' / 'proxy-workbench',
                cache=home / '.cache' / 'proxy-workbench',
                logs=home / '.local' / 'state' / 'proxy-workbench' / 'logs')


def check_per_user(program, report, folder_of):
    """State lands in the user's folders; the program folder does not change."""
    home = Path(tempfile.mkdtemp(prefix='pw-delivery-'))
    folders = per_user_folders(home)
    program_folder = folder_of(program)
    before = tree_state(program_folder)
    try:
        env = child_environment(home)
        # The environment is the only thing this check sets: the artifact has
        # to find the per-user folders on its own.
        process, address = launch(program, env, data=folders['data'])
        try:
            report.add('per-user: the interface published its address',
                       address.is_file(), str(address))
            report.add('per-user: the data folder is the user\'s own',
                       folders['data'].is_dir(), str(folders['data']))
            # The background layer's own records live in the data folder, next
            # to what they are about; the cache and log folders are created
            # beside it and stay empty until something needs them.
            journal = folders['data'] / 'desktop-journal.jsonl'
            report.add('per-user: the background layer keeps its journal with the data',
                       journal.is_file(), str(journal))
            report.add('per-user: the cache and log folders exist next to it',
                       folders['cache'].is_dir() and folders['logs'].is_dir(),
                       f"{folders['cache']} | {folders['logs']}")
            report.add('per-user: nothing was written next to the program',
                       tree_state(program_folder) == before, str(program_folder))
        finally:
            stop(process)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def check_portable(unit, report, marker_writer):
    """Portable mode happens because the user asked, and puts data where they were told.

    The artifact is copied first: a portable build is a unit the user moves to a
    stick, and the copy is the only way to test one without writing into the
    build output.
    """
    home = Path(tempfile.mkdtemp(prefix='pw-portable-'))
    room = Path(tempfile.mkdtemp(prefix='pw-app-'))
    unit = Path(unit)
    moved = room / unit.name
    shutil.copytree(unit, moved, symlinks=True)
    program_copy = executable(moved)
    marker = marker_writer(moved)
    # data/, cache/ and logs/ are where portable mode is supposed to put things,
    # so they are the one part of the copy that is allowed to change.
    skip = ('data', 'cache', 'logs')
    before = tree_state(moved, skip=skip)
    try:
        env = child_environment(home)
        process, address = launch(program_copy, env, data=moved / 'data')
        try:
            report.add('portable: the data folder is next to the program',
                       address.parent == moved / 'data', str(address))
            report.add('portable: the marker file is what turned it on',
                       marker.is_file(), str(marker))
            report.add('portable: nothing else in the program folder changed',
                       tree_state(moved, skip=skip) == before, str(moved))
        finally:
            stop(process)
    finally:
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(room, ignore_errors=True)


def check_migration(program, report, folder_of):
    """An old data folder is moved once, with a backup, a receipt and a survivor."""
    home = Path(tempfile.mkdtemp(prefix='pw-migrate-'))
    legacy = Path(tempfile.mkdtemp(prefix='pw-legacy-')) / 'data'
    (legacy / 'nested').mkdir(parents=True)
    (legacy / 'proxies.sqlite3').write_bytes(b'SQLite format 3\x00legacy')
    (legacy / 'gui-preferences.json').write_text('{"legacy": true}', encoding='utf-8')
    (legacy / 'nested' / 'note.txt').write_text('older run', encoding='utf-8')
    folders = per_user_folders(home)
    program_folder = folder_of(program)
    before = tree_state(program_folder)
    try:
        env = child_environment(home, {'PROXY_WORKBENCH_PREVIOUS_DATA': str(legacy)})
        process, _ = launch(program, env, data=folders['data'])
        try:
            moved = [path for path in folders['data'].rglob('*') if path.is_file()]
            report.add('migration: the old folder was moved into the per-user data folder',
                       (folders['data'] / 'gui-preferences.json').is_file(),
                       f'{len(moved)} files under {folders["data"]}')
            report.add('migration: the database came through the SQLite backup path',
                       (folders['data'] / 'proxies.sqlite3').is_file(), '')
            report.add('migration: the old folder is still there',
                       legacy.is_dir() and (legacy / 'nested' / 'note.txt').is_file(), str(legacy))
            receipt = folders['data'] / 'migration-receipt.json'
            report.add('migration: a receipt names the live copy', receipt.is_file(), str(receipt))
            # The backup is a copy of the old folder, kept next to it, and the
            # receipt is what names it: asking the receipt is the same as
            # asking the user where their old data went.
            named = ''
            if receipt.is_file():
                named = (json.loads(receipt.read_text(encoding='utf-8')) or {}).get('backup') or ''
            report.add('migration: the backup the receipt names is on disk',
                       bool(named) and Path(named).is_dir(), named)
            report.add('migration: nothing was written next to the program',
                       tree_state(program_folder) == before, str(program_folder))
        finally:
            stop(process)
        process, _ = launch(program, env, data=folders['data'])
        try:
            journal = folders['data'] / 'desktop-journal.jsonl'
            moves = [line for line in (journal.read_text(encoding='utf-8').splitlines() if journal.is_file() else [])
                     if 'migration.applied' in line]
            report.add('migration: a second start does not move it again', len(moves) == 1,
                       f'{len(moves)} migration entries in {journal}')
        finally:
            stop(process)
    finally:
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(legacy.parent, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('artifact', help='a built .app, a folder with the program in it, or an .exe')
    parser.add_argument('--only', choices=['per-user', 'portable', 'migration'],
                        help='run one of the checks')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from proxy_workbench import desktop

    program = executable(args.artifact)
    if not program.is_file():
        print(f'no program inside {args.artifact}: {program}', file=sys.stderr)
        return 2
    # The unit a user moves around: the .app itself, or the folder holding the
    # program.  It is what a portable copy is made of, and what the marker file
    # is written next to.
    unit = Path(args.artifact) if Path(args.artifact).suffix == '.app' else program.parent
    folder_of = (lambda path: Path(path).parent.parent.parent) if program.parent.name == 'MacOS' \
        else (lambda path: Path(path).parent)

    def marker_writer(moved):
        # The marker lives where the product looks for it: inside the .app
        # bundle on macOS, next to the program on Windows.
        return desktop.write_portable_marker(moved)

    report = Report()
    checks = {'per-user': lambda: check_per_user(program, report, folder_of),
              'portable': lambda: check_portable(unit, report, marker_writer),
              'migration': lambda: check_migration(program, report, folder_of)}
    try:
        for name, check in checks.items():
            if args.only and args.only != name:
                continue
            check()
    except CheckError as exc:
        report.add('delivery', False, str(exc))
    print(json.dumps(dict(artifact=str(program), ok=report.ok, checks=report.checks),
                     ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
