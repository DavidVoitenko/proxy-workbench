"""Build the macOS desktop artifact: a self-contained .app and a .dmg around it.

    python packaging/build_macos.py --no-sign            # honest unsigned build
    python packaging/build_macos.py --sign --notarize    # only with real credentials present

Everything optional is checked before it is attempted.  If signing is asked for
and this machine has no identity, the build fails instead of producing an
unsigned artifact with a signed name; if signing is not asked for, the
provenance file records ``signed: false`` with the reason.  No credential is
created, purchased or stored here - this script only looks for one.

The build is only finished when the artifact has been started: the .app is
launched with a throwaway HOME, the page it serves is fetched over loopback,
the menu bar helper it contains has to report that it drew its status item,
and the bundle must come back byte-for-byte identical.  A build that produces
an app without the menu bar, or that writes into its own bundle, is a failed
build, not a warning.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from proxy_workbench import desktop
from release_manifest import build_manifest, write_manifest
import tray_helper

APP_NAME = 'Proxy Workbench'
SUPPORTED_ARCHES = ('arm64',)
TRAY_READY_TIMEOUT_S = 40


class BuildError(Exception):
    """The requested build cannot be produced on this machine."""


def run(argv, **kwargs):
    done = subprocess.run([str(part) for part in argv], **kwargs)
    if done.returncode != 0:
        raise BuildError(f'command failed with code {done.returncode}: {" ".join(str(p) for p in argv)}')
    return done


def app_bundle(dist, app_name=APP_NAME):
    return Path(dist) / f'{app_name}.app'


def bundle_executable(bundle, app_name=APP_NAME):
    return bundle / 'Contents' / 'MacOS' / app_name


def pyinstaller_command(explicit=None):
    """How PyInstaller is invoked.

    The interpreter that is running this script runs it, not whatever
    ``pyinstaller`` happens to be on PATH: a build in a virtual environment
    without that directory on PATH used to fail with a bare
    ``No such file or directory: 'pyinstaller'``.
    """
    if explicit:
        return [str(explicit)]
    return [sys.executable, '-m', 'PyInstaller']


def build_tray_helper(root):
    """Compile the menu bar helper before PyInstaller touches anything.

    The spec compiles it again as its first step; the second call finds the
    digest-named cache file and returns it, so the source is compiled once and
    both steps agree on the same binary.
    """
    return tray_helper.build(Path(root) / 'build' / 'tray')


def build_app(root, dist, *, arch, pyinstaller=None):
    """Run PyInstaller for one architecture and return the produced .app."""
    if arch not in SUPPORTED_ARCHES:
        raise BuildError(
            f'{arch} is not a supported target here. Build a separate artifact per architecture; '
            f'supported: {", ".join(SUPPORTED_ARCHES)}. A universal2 build needs both slices built '
            f'and merged, which this script does not pretend to do.')
    command = [*pyinstaller_command(pyinstaller), '--noconfirm', '--clean',
               '--distpath', str(dist), '--workpath', str(Path(root) / 'build' / f'pyinstaller-{arch}'),
               str(Path(root) / 'packaging' / 'proxy-workbench-macos.spec')]
    env = dict(os.environ, ARCHFLAGS=f'-arch {arch}')
    run(command, cwd=root, env=env)
    bundle = app_bundle(dist)
    if not bundle_executable(bundle).is_file():
        raise BuildError(f'PyInstaller reported success but {bundle_executable(bundle)} is missing.')
    return bundle


def sign_app(bundle, identity):
    """Sign the bundle with a hardened runtime and a secure timestamp."""
    # --deep reaches the menu bar helper as well: it is a Mach-O executable
    # inside the bundle, and a bundle whose nested code is unsigned does not
    # pass codesign --verify.
    run(['codesign', '--force', '--deep', '--options', 'runtime', '--timestamp',
         '--sign', identity, str(bundle)])
    run(['codesign', '--verify', '--strict', '--verbose=2', str(bundle)])
    info = desktop.signature_of(bundle)
    if not info.signed:
        raise BuildError(f'codesign did not produce a verifiable signature: {info.reason}')
    return info


def notarize(bundle, dmg=None, profile=None):
    """Submit the bundle to Apple and staple the ticket."""
    submit = ['xcrun', 'notarytool', 'submit', str(bundle or dmg), '--wait']
    if profile:
        submit += ['--keychain-profile', profile]
    run(submit)
    if bundle:
        run(['xcrun', 'stapler', 'staple', str(bundle)])
        run(['xcrun', 'stapler', 'validate', str(bundle)])
    return True


def make_dmg(bundle, out, *, volume_name=APP_NAME):
    """A read/write compressed disk image holding the .app and an Applications link."""
    staging = Path(out).with_suffix('')
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    target = staging / f'{bundle.name}'
    shutil.copytree(bundle, target, symlinks=True)
    link = staging / 'Applications'
    link.symlink_to('/Applications')
    run(['hdiutil', 'create', '-volname', volume_name, '-srcfolder', str(staging),
         '-ov', '-format', 'UDZO', str(out)])
    shutil.rmtree(staging, ignore_errors=True)
    return Path(out)


def make_zip(bundle, out):
    """The installable archive: the .app as it would be signed, inside a zip.

    A zip is a container, not a signed object - ``codesign`` will not report on
    it - so the manifest records the bundle's own signature separately rather
    than borrowing the container's empty answer.
    """
    out = Path(out)
    staging = Path(out).with_suffix('')
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    shutil.copytree(bundle, staging / bundle.name, symlinks=True)
    archive = shutil.make_archive(str(out.with_suffix('')), 'zip', root_dir=staging)
    shutil.rmtree(staging, ignore_errors=True)
    return Path(archive)


def make_executable(bundle, name):
    """Restore the execute bit on a collected binary, and say whether it was needed.

    PyInstaller copies collected data with its permissions, but an installed
    application that cannot execute its menu bar helper has no menu bar, so the
    bit is checked rather than assumed.
    """
    path = bundle / tray_helper.BUNDLE_DATA_DIR / tray_helper.BUNDLE_SUBFOLDER / name
    if not path.is_file():
        return None, False
    if os.access(path, os.X_OK):
        return path, False
    os.chmod(path, 0o755)
    return path, True


def verify_bundle(bundle):
    """Structural checks that do not need a signature: plist, executable, resources.

    The resource checks are the ones that catch a spec that forgot the product:
    the bundled ``proxy_workbench/ui`` directory has to exist inside the bundle,
    because that is where the interface is read from at run time, and the menu
    bar helper has to exist and be executable, because F22 is a promise the
    artifact makes to the user.
    """
    plist = bundle / 'Contents' / 'Info.plist'
    if not plist.is_file():
        raise BuildError(f'{plist} is missing: the bundle has no Info.plist.')
    info = plist.read_text(encoding='utf-8', errors='replace')
    for key in ('CFBundleIdentifier', 'CFBundleShortVersionString', 'CFBundleExecutable'):
        if key not in info:
            raise BuildError(f'Info.plist does not declare {key}.')
    if not bundle_executable(bundle).is_file():
        raise BuildError('Info.plist declares an executable that is not in the bundle.')
    if not _bundled_resource(bundle, 'index.html'):
        raise BuildError('the interface assets are not inside the bundle: ui/index.html '
                         'is missing. The spec must copy the ui directory into the app.')
    if not _bundled_resource(bundle, 'sources.json'):
        raise BuildError('sources.json is not inside the bundle; the interface would fail to start.')
    if not _bundled_resource(bundle, 'source-catalog.json'):
        # The catalog is what the Sources page reads. Without it the tab renders
        # an empty list and every built-in source looks absent, which is exactly
        # the kind of quiet breakage this check exists to refuse.
        raise BuildError('source-catalog.json is not inside the bundle; the Sources tab would '
                         'show no catalog at all. The spec must copy it into the app.')
    helper = tray_helper.in_bundle(bundle)
    if helper is None:
        raise BuildError('the menu bar helper is not inside the bundle. The spec must compile it '
                         '(packaging/tray_helper.py) and collect it; an installed application has '
                         'no swiftc and would run with no menu bar at all.')
    if not os.access(helper, os.X_OK):
        raise BuildError(f'the bundled menu bar helper {helper} is not executable.')
    return True


def _bundled_resource(bundle, name):
    """True when a packaged file exists anywhere under the bundle's collected dirs."""
    for folder in ('Contents/Frameworks', 'Contents/Resources'):
        base = bundle / folder
        if base.is_dir() and any(path.is_file() for path in base.rglob(name)):
            return True
    return False


def verify_launch(bundle, *, timeout=120, want_tray=True):
    """Start the built app and confirm what a user would see.

    This is the check that packaged resources, the data path, the worker
    command and the menu bar are all right: the page only appears if the
    bundled UI was found, the per-user folder only appears if the host did not
    try to write into the read-only bundle, and the menu bar only counts if the
    helper process reported the status item it drew.  Ports are chosen by the OS
    and the network stays on loopback.
    """
    home = _short_home()
    # BROWSER keeps a launch check from taking over the build machine's browser:
    # a second start of the app asks the running one to show its page, and
    # that is a real user action even when a build script triggered it.
    env = dict(os.environ, HOME=str(home), PROXY_WORKBENCH_LANG='en', BROWSER='true')
    before = _bundle_state(bundle)
    command = [str(bundle_executable(bundle)), '--no-browser', '--port', '0',
               '--api-port', '0', '--no-gateway']
    if not want_tray:
        command.append('--no-tray')
    # Leave the environment otherwise alone: a real launch is what is verified.
    process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    data = home / 'Library' / 'Application Support' / 'proxy-workbench'
    logs = home / 'Library' / 'Logs' / 'proxy-workbench'
    try:
        address = _wait_for(data / 'gui-address.json', process, timeout)
        port = json.loads(address.read_text(encoding='utf-8'))['port']
        page = _fetch(f'http://127.0.0.1:{port}/')
        if 'workbench-token' not in page:
            raise BuildError('the built app served a page without the interface token.')
        tray = None
        if want_tray:
            tray = _wait_for_tray(logs / 'tray.log', process, TRAY_READY_TIMEOUT_S)
        if _bundle_state(bundle) != before:
            raise BuildError('the built app wrote into its own bundle; an installed app must keep '
                             'state in the per-user folder.')
        second = _second_launch(bundle, env, timeout)
        return dict(address=f'http://127.0.0.1:{port}/', data=str(data), page_bytes=len(page),
                    bundle_unchanged=True, tray=tray, second_launch=second)
    finally:
        process.terminate()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(10)
        left = _wait_no_helper(15)
        shutil.rmtree(home, ignore_errors=True)
        if left:
            print(f'warning: the menu bar helper outlived the app: {left}', file=sys.stderr)


def _short_home():
    """A throwaway HOME short enough for the control socket to fit in.

    The host puts its control socket inside the data folder, and a unix socket
    path is capped at 104 bytes.  macOS's per-user temporary directory is
    already about 60 characters long, so a ``tempfile`` directory under it
    pushes the socket past the limit - the control server then fails to bind,
    the menu bar has nothing to talk to, and the check would report a broken
    product instead of an impossible test environment.  ``/tmp`` is short and
    is the folder the socket limit is designed around.
    """
    home = Path('/tmp') / f'pw-launch-{os.getpid()}'
    if home.exists():
        shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True)
    return home


def _second_launch(bundle, env, timeout):
    """A second start of the artifact must reach the first one, not rival it.

    The count of running instances of the bundle executable is read from the
    process table before and after: two processes serving one data folder would
    be the defect F22 forbids.  The count is taken by the full executable path,
    because the command line of a running app carries its arguments and a
    match on the bare file name would find nothing.
    """
    before = _instances(bundle)
    second = subprocess.run([str(bundle_executable(bundle)), '--no-browser', '--port', '0'],
                            env=env, capture_output=True, text=True, timeout=timeout)
    after = _instances(bundle)
    said = [line.strip() for line in (second.stdout or '').splitlines() if line.strip()]
    return dict(exit_code=second.returncode, said=said[-1:] if said else [],
                instances_before=before, instances_after=after,
                one_instance=after <= before)


def _instances(bundle):
    needle = str(bundle_executable(bundle))
    try:
        done = subprocess.run(['ps', '-A', '-o', 'command='], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return -1
    return sum(1 for line in (done.stdout or '').splitlines() if needle in line)


def _helpers():
    """Every menu bar helper process on this machine, whoever started it."""
    try:
        done = subprocess.run(['ps', '-A', '-o', 'command='], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in (done.stdout or '').splitlines()
            if desktop.TRAY_HELPER_NAME in line]


def _wait_no_helper(timeout):
    """Wait for the helper to go away by itself, and return it if it does not.

    The helper is a separate process, so an app that dies without shutting it
    down leaves it running with nobody to talk to.  F22's acceptance is "no
    orphan processes", and the process table is the only place that is a fact.
    """
    deadline = time.monotonic() + timeout
    left = _helpers()
    while left and time.monotonic() < deadline:
        time.sleep(0.25)
        left = _helpers()
    return left


def _bundle_state(bundle):
    """Names and sizes of everything in the bundle, to prove a launch changed none of it."""
    return sorted((str(path.relative_to(bundle)), path.stat().st_size)
                  for path in bundle.rglob('*') if path.is_file())


def _wait_for(path, process, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return path
        if process.poll() is not None:
            raise BuildError(f'the built app exited with code {process.returncode} before serving anything.')
        time.sleep(0.25)
    raise BuildError(f'the built app did not create {path} within {timeout}s.')


def _wait_for_tray(path, process, timeout):
    """Wait for the helper to report the status item it drew.

    The report comes from the helper itself into its own log, so this is the
    menu bar talking about itself, not the build guessing from a file name.
    """
    deadline = time.monotonic() + timeout
    report = None
    while time.monotonic() < deadline:
        report = _tray_report(path)
        if report is not None:
            break
        if process.poll() is not None:
            raise BuildError('the built app exited before the menu bar helper reported anything.')
        time.sleep(0.25)
    if report is None:
        raise BuildError(f'the menu bar helper of the built app did not report within {timeout}s '
                         f'(nothing in {path}).')
    if not report.get('status_item'):
        raise BuildError(f'the menu bar helper did not create a status item: {report}')
    if not _alive(report.get('pid')):
        raise BuildError(f'the menu bar helper reported pid {report.get("pid")} and then it was gone.')
    return report


def _tray_report(path):
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None
    for line in text.splitlines():
        marker = line.find('ready ')
        if marker < 0:
            continue
        try:
            return json.loads(line[marker + len('ready '):])
        except ValueError:
            continue
    return None


def _alive(pid):
    try:
        os.kill(int(pid), 0)
    except (TypeError, ValueError, ProcessLookupError, PermissionError) as exc:
        return isinstance(exc, PermissionError)
    except OSError:
        return False
    return True


def _fetch(url):
    import httpx
    try:
        return httpx.get(url, timeout=15, trust_env=False).text
    except httpx.HTTPError as exc:
        raise BuildError(f'the built app did not answer on {url}: {exc}') from exc


def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--arch', default='arm64', choices=list(SUPPORTED_ARCHES))
    parser.add_argument('--dist', default=str(root / 'dist'))
    parser.add_argument('--sign', action='store_true', help='sign with the identity found in the keychain')
    parser.add_argument('--notarize', action='store_true', help='submit to Apple notary service')
    parser.add_argument('--no-dmg', action='store_true')
    parser.add_argument('--channel', default='stable')
    parser.add_argument('--notes-url', default=None)
    parser.add_argument('--pyinstaller', default=None, help='path to the pyinstaller entry point')
    parser.add_argument('--no-verify', action='store_true', help='skip starting the built app')
    parser.add_argument('--no-tray', action='store_true',
                        help='do not require a menu bar (a build machine without a window session)')
    args = parser.parse_args(argv)
    dist = Path(args.dist)

    from proxy_workbench.branding import PRODUCT_VERSION
    signing = desktop.signing_status()
    notarization = desktop.notarization_status()
    report = dict(version=PRODUCT_VERSION, arch=args.arch, signing=signing.as_dict(),
                  notarization=notarization.as_dict())
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if args.sign and not signing.available:
        print(f'refusing to build a signed release: {signing.reason}', file=sys.stderr)
        return 3
    if args.notarize and not notarization.available:
        print(f'refusing to build a notarized release: {notarization.reason}', file=sys.stderr)
        return 3

    tray_helper_path, tray_reason = (None, 'not built')
    if sys.platform == 'darwin':
        tray_helper_path, tray_reason = build_tray_helper(root)
    print(json.dumps(dict(menu_bar_helper=str(tray_helper_path or ''), reason=tray_reason),
                     ensure_ascii=False), flush=True)

    try:
        bundle = build_app(root, dist, arch=args.arch, pyinstaller=args.pyinstaller)
        helper, fixed = make_executable(bundle, desktop.TRAY_HELPER_NAME)
        verify_bundle(bundle)
        launch = None if args.no_verify else verify_launch(bundle, want_tray=not args.no_tray)
        if args.sign:
            sign_app(bundle, signing.identity)
        if args.notarize:
            notarize(bundle, profile=notarization.profile or None)
        bundle_signature = desktop.signature_of(bundle)
        artifacts = [make_zip(bundle, dist / f'proxy-workbench-{PRODUCT_VERSION}-macos-{args.arch}.zip')]
        if not args.no_dmg:
            artifacts.append(make_dmg(bundle, dist / f'proxy-workbench-{PRODUCT_VERSION}-macos-{args.arch}.dmg'))
    except BuildError as exc:
        print(f'build failed: {exc}', file=sys.stderr)
        return 2

    manifest = build_manifest(artifacts, version=PRODUCT_VERSION, channel=args.channel,
                              notes_url=args.notes_url, platform='darwin')
    manifest['arch'] = args.arch
    manifest['built_at'] = time.time()
    manifest['verified'] = dict(bundle_structure=True, launched=bool(launch), detail=launch)
    manifest['menu_bar'] = dict(
        helper=str(helper) if helper else None, built_on_this_machine=bool(tray_helper_path),
        reason=tray_reason, execute_bit_restored=bool(fixed),
        checked=bool(launch) and not args.no_tray,
        confirmed=bool(launch and launch.get('tray')),
        report=(launch or {}).get('tray'))
    if not manifest['menu_bar']['checked']:
        # Say which of the two it is: a menu bar nobody looked at is not a
        # menu bar that was not there.
        manifest['menu_bar']['reason'] = desktop.tr(
            'сборка выполнена с --no-tray: меню-бар при запуске не проверялся',
            'the build ran with --no-tray: the menu bar was not checked at launch')
    manifest['bundle'] = dict(name=bundle.name, signature=bundle_signature.as_dict(),
                              notarized=bool(args.notarize and notarization.available),
                              notarization=notarization.as_dict())
    path = write_manifest(dist / f'proxy-workbench-{PRODUCT_VERSION}-macos-{args.arch}.manifest.json', manifest)
    print(f'manifest: {path}')
    print(f"  {bundle.name}: signed={bundle_signature.signed} ({bundle_signature.reason})"
          f"{', notarized' if manifest['bundle']['notarized'] else ', not notarized'}")
    print(f"  menu bar: {'confirmed at launch' if manifest['menu_bar']['confirmed'] else 'NOT confirmed'}"
          f" ({manifest['menu_bar']['reason']})")
    if launch:
        print(f"  second launch: {json.dumps(launch['second_launch'], ensure_ascii=False)}")
    for entry in manifest['artifacts']:
        print(f"  {entry['name']}: container signed={entry['signature']['signed']} "
              f"({entry['signature']['reason']})")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
