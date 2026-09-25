"""Build the Windows desktop artifacts: a windowed GUI exe, a console CLI exe, an installer, a zip.

    python packaging/build_windows.py --dist dist --installer

The GUI executable has no console (``console=False``), so the CLI is a second
binary: a windowed process has nowhere to print.  The installer is per-user
(``PrivilegesRequired=lowest``) and installs into ``%LOCALAPPDATA%``, because
the app writes only to per-user folders and must not need an administrator.

Signing uses a certificate that is already in the Windows certificate store and
is named by thumbprint.  No password is ever passed on the command line, and
this script never creates or imports a certificate.
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

GUI_EXE = 'proxy-workbench-gui.exe'
CLI_EXE = 'proxy-workbench-cli.exe'
THUMBPRINT_ENV = 'PROXY_WORKBENCH_SIGN_THUMBPRINT'
TIMESTAMP_URL = 'http://timestamp.digicert.com'
PORTABLE_README = '''Proxy Workbench - portable build
=================================

This folder is the portable variant. Nothing is installed.

  proxy-workbench-gui.exe   opens the interface in your browser
  proxy-workbench-cli.exe   the command line tool

Data location:
  by default a per-user folder
    %LOCALAPPDATA%\\proxy-workbench
  portable mode (data next to this file) is opt-in:
    set PROXY_WORKBENCH_PORTABLE=1

The files in this folder may be read-only. The application never writes here
unless you ask for portable mode.
'''


class BuildError(Exception):
    """The requested build cannot be produced on this machine."""


def run(argv, **kwargs):
    done = subprocess.run([str(part) for part in argv], **kwargs)
    if done.returncode != 0:
        raise BuildError(f'command failed with code {done.returncode}: {" ".join(str(p) for p in argv)}')
    return done


def build_executable(root, dist, spec, name):
    run(['pyinstaller', '--noconfirm', '--clean', '--distpath', str(dist),
         '--workpath', str(root / 'build' / 'pyinstaller'), str(root / 'packaging' / spec)], cwd=root)
    built = Path(dist) / name
    if not built.is_file():
        raise BuildError(f'PyInstaller reported success but {built} is missing.')
    return built


def sign(thumbprint, target):
    """Sign from the certificate store; the password never appears in argv."""
    run(['signtool', 'sign', '/sha1', thumbprint, '/fd', 'SHA256', '/tr', TIMESTAMP_URL,
         '/td', 'SHA256', '/v', str(target)])
    info = desktop.signature_of(target, platform='win32')
    if not info.signed:
        raise BuildError(f'signtool did not produce a verifiable signature: {info.reason}')
    return info


def make_portable_zip(dist, version, *, executables, out=None):
    """Zip the GUI and CLI binaries plus the note that says where data goes."""
    out = Path(out or dist / f'proxy-workbench-{version}-windows-x64-portable.zip')
    staging = Path(out).with_suffix('')
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for source in executables:
        shutil.copy2(source, staging / Path(source).name)
    (staging / 'portable-README.txt').write_text(PORTABLE_README, encoding='utf-8')
    archive = shutil.make_archive(str(out.with_suffix('')), 'zip', root_dir=staging)
    shutil.rmtree(staging, ignore_errors=True)
    return Path(archive)


def build_installer(root, dist, version, *, compiler=None):
    """Run Inno Setup if it is installed; otherwise say plainly that it was skipped."""
    compiler = compiler or shutil.which('iscc') or shutil.which('ISCC')
    if not compiler:
        return None, ('Inno Setup (iscc) was not found on PATH; the per-user installer was not built. '
                      'Install Inno Setup 6 and run this script again.')
    run([compiler, f'/DProductVersion={version}', f'/DOutDir={dist}',
         str(root / 'packaging' / 'windows-installer.iss')])
    setup = Path(dist) / f'proxy-workbench-{version}-windows-x64-setup.exe'
    if not setup.is_file():
        raise BuildError(f'iscc reported success but {setup} is missing.')
    return setup, None


def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--dist', default=str(root / 'dist'))
    parser.add_argument('--installer', action='store_true', help='also build the per-user installer')
    parser.add_argument('--sign', action='store_true', help='sign with the certificate named by the environment')
    parser.add_argument('--channel', default='stable')
    parser.add_argument('--notes-url', default=None)
    args = parser.parse_args(argv)
    dist = Path(args.dist)

    from proxy_workbench.branding import PRODUCT_VERSION
    signing = desktop.signing_status(platform='win32')
    thumbprint = os.environ.get(THUMBPRINT_ENV, '')
    print(json.dumps(dict(version=PRODUCT_VERSION, signing=signing.as_dict(),
                          thumbprint_configured=bool(thumbprint)), ensure_ascii=False, indent=2), flush=True)
    if args.sign and (not signing.available or not thumbprint):
        missing = []
        if not signing.available:
            missing.append(signing.reason)
        if not thumbprint:
            missing.append(f'{THUMBPRINT_ENV} is not set')
        print('refusing to build a signed release: ' + '; '.join(missing), file=sys.stderr)
        return 3

    try:
        gui = build_executable(root, dist, 'proxy-workbench-windows-gui.spec', GUI_EXE)
        cli = build_executable(root, dist, 'proxy-workbench-windows-cli.spec', CLI_EXE)
        signatures = {}
        if args.sign:
            for target in (gui, cli):
                signatures[Path(target).name] = sign(thumbprint, target).as_dict()
        portable = make_portable_zip(dist, PRODUCT_VERSION, executables=[gui, cli])
        installer, skipped = (None, None)
        if args.installer:
            installer, skipped = build_installer(root, dist, PRODUCT_VERSION)
    except BuildError as exc:
        print(f'build failed: {exc}', file=sys.stderr)
        return 2

    artifacts = [gui, cli, portable]
    if installer:
        artifacts.append(installer)
    manifest = build_manifest(artifacts, version=PRODUCT_VERSION, channel=args.channel,
                              notes_url=args.notes_url, platform='win32')
    manifest['built_at'] = time.time()
    manifest['installer'] = dict(built=bool(installer), per_user=True,
                                 note=skipped or 'Inno Setup per-user installer')
    if signatures:
        manifest['signing'] = dict(per_executable=signatures, thumbprint_configured=True)
    path = write_manifest(dist / f'proxy-workbench-{PRODUCT_VERSION}-windows-x64.manifest.json', manifest)
    print(f'manifest: {path}')
    if skipped:
        print(f'installer skipped: {skipped}', file=sys.stderr)
    for entry in manifest['artifacts']:
        print(f"  {entry['name']}: signed={entry['signature']['signed']} "
              f"({entry['signature']['reason']})")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
