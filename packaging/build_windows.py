"""Build the Windows desktop artifacts: a windowed GUI exe, a console CLI exe, an installer, a zip.

    python packaging/build_windows.py --dist dist --installer

The GUI executable has no console (``console=False``), so the CLI is a second
binary: a windowed process has nowhere to print.  The installer is per-user
(``PrivilegesRequired=lowest``) and installs into ``%LOCALAPPDATA%``, because
the app writes only to per-user folders and must not need an administrator.

Like the macOS build this one does not stop at "PyInstaller exited with 0": the
GUI executable is started with a throwaway ``%LOCALAPPDATA%``, the page it
serves is fetched over loopback, its PE subsystem is read to confirm that it
really is a windowed binary and the CLI really is a console one, a second start
has to reach the first, and the folder the executable sits in must come back
unchanged.  A windowless machine cannot run any of that - it is a Windows
build, and it says so instead of pretending.

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
import struct
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
PORTABLE_README_NAME = 'portable-README.txt'
#: Where Inno Setup usually lives on a Windows machine or a GitHub runner.
ISCC_HINTS = (r'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
              r'C:\Program Files\Inno Setup 6\ISCC.exe')
#: PE subsystem values: what a program is, not what it prints.
PE_SUBSYSTEM = {2: 'windows-gui', 3: 'windows-console'}
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


class NotThisPlatform(BuildError):
    """A Windows build cannot be produced on a machine that is not Windows."""


def run(argv, **kwargs):
    done = subprocess.run([str(part) for part in argv], **kwargs)
    if done.returncode != 0:
        raise BuildError(f'command failed with code {done.returncode}: {" ".join(str(p) for p in argv)}')
    return done


def require_windows():
    if sys.platform != 'win32':
        raise NotThisPlatform(
            'a Windows build needs Windows: PyInstaller builds for the machine it runs on, '
            'and a .exe produced elsewhere would not be a Windows program. '
            'Run this script on a Windows machine, or on the Windows job in '
            '.github/workflows/windows.yml.')


def pyinstaller_command(explicit=None):
    """How PyInstaller is invoked: the interpreter running this script, not PATH."""
    if explicit:
        return [str(explicit)]
    return [sys.executable, '-m', 'PyInstaller']


def build_executable(root, dist, spec, name, pyinstaller=None):
    """Build one binary, with its own work directory.

    A shared work path lets the second analysis read the first one's cached
    state, which is how two builds end up sharing one collected tree.
    """
    run([*pyinstaller_command(pyinstaller), '--noconfirm', '--clean',
         '--distpath', str(dist), '--workpath', str(root / 'build' / 'pyinstaller' / Path(spec).stem),
         str(root / 'packaging' / spec)], cwd=root)
    built = Path(dist) / name
    if not built.is_file():
        raise BuildError(f'PyInstaller reported success but {built} is missing.')
    return built


def pe_subsystem(path):
    """The PE subsystem of an .exe, read from the file itself.

    ``console=False`` in a spec is a claim about this byte.  Reading it back
    out of the produced executable is the only check that cannot be satisfied
    by a spec that was edited and not built.  Returns the name from
    :data:`PE_SUBSYSTEM`, or None when the file is not a PE image.
    """
    with open(path, 'rb') as handle:
        header = handle.read(0x100)
        if len(header) < 0x40 or header[:2] != b'MZ':
            return None
        offset = struct.unpack_from('<I', header, 0x3C)[0]
        handle.seek(offset)
        rest = handle.read(0x100)
    if len(rest) < 0x50 or rest[:4] != b'PE\0\0':
        return None
    optional = 0x18                       # COFF header is 20 bytes after the signature
    magic = struct.unpack_from('<H', rest, optional)[0]
    if magic not in (0x10B, 0x20B):
        return None
    return PE_SUBSYSTEM.get(struct.unpack_from('<H', rest, optional + 68)[0])


def check_subsystems(gui, cli):
    """Both binaries have to be the kind of program F23 asks for."""
    found = {GUI_EXE: pe_subsystem(gui), CLI_EXE: pe_subsystem(cli)}
    problems = []
    if found[GUI_EXE] != 'windows-gui':
        problems.append(f'{GUI_EXE} is {found[GUI_EXE] or "not a PE image"}, expected a windowed program')
    if found[CLI_EXE] != 'windows-console':
        problems.append(f'{CLI_EXE} is {found[CLI_EXE] or "not a PE image"}, expected a console program')
    if problems:
        raise BuildError('; '.join(problems))
    return found


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
    (staging / PORTABLE_README_NAME).write_text(PORTABLE_README, encoding='utf-8')
    archive = shutil.make_archive(str(out.with_suffix('')), 'zip', root_dir=staging)
    shutil.rmtree(staging, ignore_errors=True)
    return Path(archive)


def write_portable_readme(dist):
    """The same note next to the executables, where the installer looks for it."""
    path = Path(dist) / PORTABLE_README_NAME
    path.write_text(PORTABLE_README, encoding='utf-8')
    return path


def iscc_path(compiler=None):
    """Inno Setup, wherever this machine keeps it."""
    if compiler:
        return compiler
    found = shutil.which('iscc') or shutil.which('ISCC')
    if found:
        return found
    for hint in ISCC_HINTS:
        if Path(hint).is_file():
            return hint
    return None


def build_installer(root, dist, version, *, compiler=None):
    """Run Inno Setup if it is installed; otherwise say plainly that it was skipped."""
    compiler = iscc_path(compiler)
    if not compiler:
        return None, ('Inno Setup (iscc) was not found on PATH; the per-user installer was not built. '
                      'Install Inno Setup 6 and run this script again.')
    run([compiler, f'/DProductVersion={version}', f'/DOutDir={dist}', f'/DSourceDir={dist}',
         str(root / 'packaging' / 'windows-installer.iss')])
    setup = Path(dist) / f'proxy-workbench-{version}-windows-x64-setup.exe'
    if not setup.is_file():
        raise BuildError(f'iscc reported success but {setup} is missing.')
    return setup, None


def verify_launch(gui, *, timeout=120):
    """Start the built GUI executable and confirm what a user would see.

    Same facts the macOS build checks, minus the menu bar: the page is served
    on a loopback port, the state lands in a per-user folder rather than next
    to the executable, a second start reaches the first, and the program folder
    is byte-for-byte unchanged afterwards.
    """
    import tempfile
    home = Path(tempfile.mkdtemp(prefix='pw-win-'))
    env = dict(os.environ, LOCALAPPDATA=str(home / 'Local'), APPDATA=str(home / 'Roaming'),
               PROXY_WORKBENCH_LANG='en')
    before = _folder_state(gui.parent)
    process = subprocess.Popen([str(gui), '--no-browser', '--port', '0', '--api-port', '0', '--no-gateway'],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    data = home / 'Local' / 'proxy-workbench'
    try:
        import httpx
        address = _wait_for(data / 'gui-address.json', process, timeout)
        port = json.loads(address.read_text(encoding='utf-8'))['port']
        page = httpx.get(f'http://127.0.0.1:{port}/', timeout=15, trust_env=False).text
        if 'workbench-token' not in page:
            raise BuildError('the built GUI executable served a page without the interface token.')
        if _folder_state(gui.parent) != before:
            raise BuildError('the built GUI executable wrote into its own program folder.')
        second = _second_launch(gui, env, timeout)
        return dict(data=str(data), port=port, page_bytes=len(page), program_folder_unchanged=True,
                    second_launch=second)
    finally:
        _stop(process)
        shutil.rmtree(home, ignore_errors=True)


def _folder_state(folder):
    return sorted((str(path.relative_to(folder)), path.stat().st_size)
                  for path in Path(folder).rglob('*') if path.is_file())


def _second_launch(gui, env, timeout):
    """A second start must reach the first, not become a rival process."""
    before = _instances(gui)
    second = subprocess.run([str(gui), '--no-browser', '--port', '0'], env=env,
                            capture_output=True, text=True, timeout=timeout)
    after = _instances(gui)
    said = [line.strip() for line in ((second.stdout or '') + (second.stderr or '')).splitlines() if line.strip()]
    return dict(exit_code=second.returncode, said=said[-1:] if said else [],
                instances_before=before, instances_after=after, one_instance=after <= before)


def _instances(gui):
    """How many copies of this executable are running, by process image name."""
    image = str(gui).rsplit('\\', 1)[-1]
    try:
        done = subprocess.run(['tasklist', '/FI', f'IMAGENAME eq {image}', '/NH'],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return -1
    return sum(1 for line in (done.stdout or '').splitlines() if image.lower() in line.lower())


def _stop(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(15)


def _wait_for(path, process, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return path
        if process.poll() is not None:
            raise BuildError(f'the built application exited with code {process.returncode} '
                             f'before serving anything. It writes to a windowless process with no '
                             f'console; run it from a terminal to see the message.')
        time.sleep(0.25)
    raise BuildError(f'the built application did not create {path} within {timeout}s.')


def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--dist', default=str(root / 'dist'))
    parser.add_argument('--installer', action='store_true', help='also build the per-user installer')
    parser.add_argument('--sign', action='store_true', help='sign with the certificate named by the environment')
    parser.add_argument('--channel', default='stable')
    parser.add_argument('--notes-url', default=None)
    parser.add_argument('--pyinstaller', default=None, help='path to the pyinstaller entry point')
    parser.add_argument('--no-verify', action='store_true', help='skip starting the built application')
    args = parser.parse_args(argv)
    dist = Path(args.dist)

    from proxy_workbench.branding import PRODUCT_VERSION
    signing = desktop.signing_status(platform='win32')
    thumbprint = os.environ.get(THUMBPRINT_ENV, '')
    print(json.dumps(dict(version=PRODUCT_VERSION, platform=sys.platform, signing=signing.as_dict(),
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
        require_windows()
        gui = build_executable(root, dist, 'proxy-workbench-windows-gui.spec', GUI_EXE, args.pyinstaller)
        cli = build_executable(root, dist, 'proxy-workbench-windows-cli.spec', CLI_EXE, args.pyinstaller)
        subsystems = check_subsystems(gui, cli)
        launch = None if args.no_verify else verify_launch(gui)
        signatures = {}
        if args.sign:
            for target in (gui, cli):
                signatures[Path(target).name] = sign(thumbprint, target).as_dict()
        write_portable_readme(dist)
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
    manifest['subsystems'] = subsystems
    manifest['verified'] = dict(launched=bool(launch), detail=launch)
    manifest['installer'] = dict(built=bool(installer), per_user=True,
                                 note=skipped or 'Inno Setup per-user installer')
    if signatures:
        manifest['signing'] = dict(per_executable=signatures, thumbprint_configured=True)
    path = write_manifest(dist / f'proxy-workbench-{PRODUCT_VERSION}-windows-x64.manifest.json', manifest)
    print(f'manifest: {path}')
    if skipped:
        print(f'installer skipped: {skipped}', file=sys.stderr)
    if launch:
        print(f'  second launch: {json.dumps(launch["second_launch"], ensure_ascii=False)}')
    for entry in manifest['artifacts']:
        print(f"  {entry['name']}: signed={entry['signature']['signed']} "
              f"({entry['signature']['reason']})")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
