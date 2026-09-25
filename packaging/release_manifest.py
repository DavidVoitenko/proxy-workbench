"""Describe built artifacts so a release note and a download check cannot disagree.

The one rule this module exists to enforce: a ``signed`` flag comes only from
``desktop.signature_of``, which returns ``signed=True`` only after a signing
tool actually verified the file.  Anything else is recorded as unsigned with
the reason, so nobody has to guess whether an artifact carries a signature.

    python packaging/release_manifest.py --version 2.2.1 dist/Proxy\\ Workbench.app
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proxy_workbench import desktop


class ProvenanceError(Exception):
    """A declared signature does not match what the signing tool found."""


def artifact_entry(path, *, platform=None, runner=None, finder=None, kind=None):
    """One artifact with its size, digest and the signature state as verified here."""
    path = Path(path)
    if path.is_dir():
        raise ProvenanceError(f'{path} is a directory; publish a .dmg, a .zip or a plain file.')
    signature = desktop.signature_of(path, platform=platform, runner=runner, finder=finder)
    return dict(name=path.name, kind=kind or guess_kind(path), bytes=path.stat().st_size,
                sha256=desktop.sha256_file(path), signature=signature.as_dict())


def guess_kind(path):
    """Artifact kind from the extension, so a consumer can tell a setup from a zip."""
    suffix = Path(path).suffix.lower()
    if suffix == '.gz' and Path(path).name.endswith('.tar.gz'):
        return 'source-archive'
    return {'.exe': 'windows-executable', '.zip': 'windows-portable', '.msi': 'windows-installer',
            '.dmg': 'macos-dmg', '.app': 'macos-app', '.whl': 'wheel'}.get(suffix, 'other')


def build_manifest(artifacts, *, version, channel='stable', released_at=None, notes_url=None,
                   min_data_schema=None, max_data_schema=None, signed_by=None, platform=None,
                   runner=None, finder=None):
    """Build a manifest, refusing a signer claim the recorded authority contradicts."""
    entries = [artifact if isinstance(artifact, dict) and 'signature' in artifact
               else artifact_entry(artifact, platform=platform, runner=runner, finder=finder)
               for artifact in artifacts]
    for entry in entries:
        signature = entry.get('signature') or {}
        if not signature.get('signed') or not signed_by:
            continue
        authority = signature.get('authority') or ''
        if authority and signed_by not in authority:
            raise ProvenanceError(
                f"{entry['name']} is signed by {authority}, not by the declared {signed_by}.")
    return dict(schema=1, product='proxy-workbench', version=version, channel=channel,
                released_at=time.time() if released_at is None else released_at,
                notes_url=notes_url, min_data_schema=min_data_schema,
                max_data_schema=max_data_schema, artifacts=entries)


def write_manifest(path, manifest):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
    return path


def read_manifest(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('artifacts', nargs='+')
    parser.add_argument('--version', required=True)
    parser.add_argument('--channel', default='stable')
    parser.add_argument('--notes-url', default=None)
    parser.add_argument('--min-data-schema', type=int, default=None)
    parser.add_argument('--max-data-schema', type=int, default=None)
    parser.add_argument('--out', default=None, help='manifest path (default: stdout)')
    args = parser.parse_args(argv)
    manifest = build_manifest(args.artifacts, version=args.version, channel=args.channel,
                              notes_url=args.notes_url, min_data_schema=args.min_data_schema,
                              max_data_schema=args.max_data_schema)
    if args.out:
        print(write_manifest(args.out, manifest))
    else:
        print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
