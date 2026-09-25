"""Check a built or downloaded release against its manifest.

Run it before publishing and after downloading: the first catches a build that
does not match what the note says, the second catches a transfer that changed
a byte.  A manifest that claims a signature the local tooling cannot confirm is
a failure, not a warning - that is the case where an unsigned artifact would be
published as a signed one.

    python packaging/verify_release.py dist/release-manifest.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# `packaging` is also an installed distribution, so this directory is imported
# by path rather than as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from proxy_workbench import desktop
from release_manifest import read_manifest


def verify_artifact(directory, entry, *, platform=None, runner=None, finder=None):
    """Re-derive size, digest and signature for one manifest entry."""
    path = Path(directory) / entry['name']
    if not path.is_file():
        return dict(name=entry['name'], ok=False, problem=desktop.tr(
            'файла нет', 'the file is missing'))
    size = path.stat().st_size
    digest = desktop.sha256_file(path)
    signature = desktop.signature_of(path, platform=platform, runner=runner, finder=finder)
    problems = []
    if size != entry.get('bytes'):
        problems.append(desktop.tr('размер не совпал', 'the size does not match'))
    if digest != entry.get('sha256'):
        problems.append(desktop.tr('контрольная сумма не совпала', 'the checksum does not match'))
    claimed = (entry.get('signature') or {}).get('signed')
    if claimed and not signature.signed:
        problems.append(desktop.tr(
            f'манифест заявляет подпись, локальная проверка её не подтверждает: {signature.reason}',
            f'the manifest claims a signature that local verification does not confirm: {signature.reason}'))
    if not claimed and signature.signed:
        problems.append(desktop.tr(
            'файл подписан, а манифест говорит, что нет', 'the file is signed but the manifest says it is not'))
    return dict(name=entry['name'], ok=not problems, size=size, sha256=digest,
                signed=signature.signed, authority=signature.authority,
                reason=signature.reason, problem='; '.join(problems))


def verify_manifest(manifest, directory, *, platform=None, runner=None, finder=None):
    results = [verify_artifact(directory, entry, platform=platform, runner=runner, finder=finder)
               for entry in manifest.get('artifacts', [])]
    return dict(ok=all(result['ok'] for result in results), results=results,
                version=manifest.get('version'), channel=manifest.get('channel'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('manifest')
    parser.add_argument('--dir', default=None, help='where the artifacts live (default: manifest folder)')
    parser.add_argument('--platform', default=None, choices=['darwin', 'win32', 'linux'])
    args = parser.parse_args(argv)
    manifest_path = Path(args.manifest)
    report = verify_manifest(read_manifest(manifest_path), args.dir or manifest_path.parent,
                             platform=args.platform)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
