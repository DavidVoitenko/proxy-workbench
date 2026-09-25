"""Build the macOS desktop artifact: a self-contained .app and a .dmg around it.

    python packaging/build_macos.py --no-sign            # honest unsigned build
    python packaging/build_macos.py --sign --notarize    # only with real credentials present

Everything optional is checked before it is attempted.  If signing is asked for
and this machine has no identity, the build fails instead of producing an
unsigned artifact with a signed name; if signing is not asked for, the
provenance file records ``signed: false`` with the reason.  No credential is
created, purchased or stored here - this script only looks for one.
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

APP_NAME = 'Proxy Workbench'
SUPPORTED_ARCHES = ('arm64',)
PYINSTALLER = shutil.which('pyinstaller') or 'pyinstaller'


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


def build_app(root, dist, *, arch, pyinstaller=None):
    """Run PyInstaller for one architecture and return the produced .app."""
    if arch not in SUPPORTED_ARCHES:
        raise BuildError(
            f'{arch} is not a supported target here. Build a separate artifact per architecture; '
            f'supported: {", ".join(SUPPORTED_ARCHES)}. A universal2 build needs both slices built '
            f'and merged, which this script does not pretend to do.')
    command = [pyinstaller or PYINSTALLER, '--noconfirm', '--clean',
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


def verify_bundle(bundle):
    """Structural checks that do not need a signature: plist, executable, resources.

    The resource check is the one that catches a spec that forgot the UI: the
    bundled ``proxy_workbench/ui`` directory has to exist inside the bundle,
    because that is where the interface is read from at run time.
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
    return True


def _bundled_resource(bundle, name):
    """True when a packaged file exists anywhere under the bundle's collected dirs."""
    for folder in ('Contents/Frameworks', 'Contents/Resources'):
        base = bundle / folder
        if base.is_dir() and any(path.is_file() for path in base.rglob(name)):
            return True
    return False


def verify_launch(bundle, *, timeout=90):
    """Start the built app and confirm it serves its own interface.

    This is the check that packaged resources, the data path and the worker
    command are all right: the page only appears if the bundled UI was found,
    and the per-user folder only appears if the host did not try to write into
    the read-only bundle.  Ports are chosen by the OS and the network stays on
    loopback.
    """
    import tempfile
    home = Path(tempfile.mkdtemp(prefix='pw-launch-'))
    env = dict(os.environ, HOME=str(home), PROXY_WORKBENCH_LANG='en')
    before = _bundle_state(bundle)
    # Leave the environment otherwise alone: a real launch is what is verified.
    process = subprocess.Popen([str(bundle_executable(bundle)), '--no-browser', '--port', '0',
                                '--api-port', '0', '--no-gateway'],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    data = home / 'Library' / 'Application Support' / 'proxy-workbench'
    try:
        address = _wait_for(data / 'gui-address.json', process, timeout)
        port = json.loads(address.read_text(encoding='utf-8'))['port']
        page = _fetch(f'http://127.0.0.1:{port}/')
        if 'workbench-token' not in page:
            raise BuildError('the built app served a page without the interface token.')
        if _bundle_state(bundle) != before:
            raise BuildError('the built app wrote into its own bundle; an installed app must keep '
                             'state in the per-user folder.')
        return dict(address=f'http://127.0.0.1:{port}/', data=str(data), page_bytes=len(page),
                    bundle_unchanged=True)
    finally:
        process.terminate()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(10)
        shutil.rmtree(home, ignore_errors=True)


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

    try:
        bundle = build_app(root, dist, arch=args.arch, pyinstaller=args.pyinstaller)
        verify_bundle(bundle)
        launch = None if args.no_verify else verify_launch(bundle)
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
    manifest['bundle'] = dict(name=bundle.name, signature=bundle_signature.as_dict(),
                              notarized=bool(args.notarize and notarization.available),
                              notarization=notarization.as_dict())
    path = write_manifest(dist / f'proxy-workbench-{PRODUCT_VERSION}-macos-{args.arch}.manifest.json', manifest)
    print(f'manifest: {path}')
    print(f"  {bundle.name}: signed={bundle_signature.signed} ({bundle_signature.reason})"
          f"{', notarized' if manifest['bundle']['notarized'] else ', not notarized'}")
    for entry in manifest['artifacts']:
        print(f"  {entry['name']}: container signed={entry['signature']['signed']} "
              f"({entry['signature']['reason']})")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
