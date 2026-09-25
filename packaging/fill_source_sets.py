#!/usr/bin/env python3
"""Populate the derived catalog sets from each source's own declared facts.

`quick` is curated and left alone. `extended` and `experimental` are derived, so
they cannot drift from the fields that justify them:

  extended     the source may be collected without credentials, has a supported
               adapter, and is a real proxy list.
  experimental the collectable sources whose value is still unproven — the
               low-priority tail. It is an opt-in extra, never a default: a
               source only earns a place here when nothing about it has been
               measured by this project.

Nothing is ever added to a set unless `collection_allowed` is true, so a paid,
key-gated, self-hosted or non-proxy entry can never appear there.

Usage:
    python3 packaging/fill_source_sets.py [sources.json]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SUPPORTED = {'line', 'json-records', 'fields', 'page-json', 'html-table'}
NO_ACCOUNT = {'fully_public_free', 'snapshot_unavailable', 'unknown'}
UNCLEAR_PRIORITY = {'low', 'experimental'}


def classify(source: dict) -> str | None:
    if not source.get('collection_allowed'):
        return None
    if source.get('payload_role') != 'proxy_list':
        return None
    adapter = (source.get('adapter') or {}).get('kind')
    access = (source.get('access') or {}).get('kind')
    priority = source.get('priority')
    if adapter in SUPPORTED and access in NO_ACCOUNT:
        return 'experimental' if priority in UNCLEAR_PRIORITY else 'extended'
    if adapter in SUPPORTED or access in NO_ACCOUNT:
        return 'experimental'
    return None


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else 'proxy_workbench/sources.json')
    catalog = json.loads(path.read_text(encoding='utf-8'))
    sources = catalog.get('sources') or []
    quick = {member for entry in catalog.get('sets') or [] if entry.get('id') == 'quick'
             for member in (entry.get('members') or [])}

    extended, experimental = [], []
    for source in sources:
        bucket = classify(source)
        if bucket is None or source.get('id') in quick:
            continue
        (extended if bucket == 'extended' else experimental).append(source['id'])

    for entry in catalog.get('sets') or []:
        if entry.get('id') == 'extended':
            entry['members'] = extended
        elif entry.get('id') == 'experimental':
            entry['members'] = experimental
    derived = (('quick', quick), ('extended', extended), ('experimental', experimental))
    protocol_sets = [row for row in catalog.get('sets') or [] if str(row.get('id', '')).startswith('protocol:')]
    for source in sources:
        identifier = source.get('id')
        # Keep memberships the protocol sets already define; only the three
        # curated/derived sets are recomputed here.
        kept = [row['id'] for row in protocol_sets if identifier in (row.get('members') or [])]
        source['sets'] = [name for name, ids in derived if identifier in ids] + kept

    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'quick': len(quick), 'extended': len(extended),
                      'experimental': len(experimental)}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
