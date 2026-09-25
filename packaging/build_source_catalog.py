#!/usr/bin/env python3
"""Build the bundled source catalog from the research catalog.

The research catalog carries the evidence; this script decides the two facts the
application must never guess, and writes them explicitly:

  payload_role         what the URL actually serves. Anything that is not a
                       proxy list (commercial pages, self-hosted setups, client
                       configuration, Tor and MTProto material, documentation)
                       gets a role that keeps it out of collection.
  collection_allowed   whether the collector may fetch it at all: no, when the
                       source is not a proxy list, is rejected, or needs an
                       account, a key, payment or your own server.

Everything is written through the application's own validator, so a catalog that
does not satisfy the app's schema fails here rather than at runtime.

Usage:
    python3 packaging/build_source_catalog.py [research.json] [sources.json]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

RESEARCH = '/Users/main/Desktop/111/proxy-sources-research-2026-09-25/catalog_corrected.json'
GATED_ACCESS = {'paid', 'temporary_trial', 'permanent_free_quota', 'free_with_api_key'}
SUBSCRIPTION_CATEGORIES = {'subscription-format', 'subscription_format'}
DOCUMENTATION_CATEGORIES = {'documentation', 'docs', 'project-homepage'}


def access_kind(source: dict) -> str:
    access = source.get('access')
    if isinstance(access, dict):
        return str(access.get('kind', 'unknown'))
    return str(access or 'unknown')


def account_required(source: dict) -> bool:
    access = source.get('access')
    if isinstance(access, dict) and access.get('account_required'):
        return True
    return bool(source.get('account_required'))


def derive(source: dict) -> tuple[str, bool]:
    """Return (payload_role, collection_allowed) from the recorded evidence."""
    relation = str(source.get('relation_to_current') or '')
    category = str(source.get('category') or '')
    kind = access_kind(source)
    adapter = source.get('adapter')
    adapter_name = adapter if isinstance(adapter, str) else (adapter or {}).get('kind')

    if relation == 'not_proxy_source':
        role = 'subscription_config' if category in SUBSCRIPTION_CATEGORIES else 'documentation'
        return role, False
    # Checked before the account gate: a self-hosted setup also wants an account
    # of its own, but it is not a commercial provider.
    if kind == 'own_infrastructure':
        return 'self_hosted', False
    if kind in GATED_ACCESS or account_required(source):
        return 'commercial_page', False
    if category in DOCUMENTATION_CATEGORIES and not source.get('data_urls'):
        return 'documentation', False
    if relation == 'rejected' or str(source.get('priority')) == 'rejected':
        return 'proxy_list', False
    # A source the collector has no adapter for is not collectable: offering it
    # would put "Add to the set" in front of a page nothing can read.
    if str(source.get('id')) in NO_ADAPTER or str(source.get('id')) in UNCONFIRMED_LAYOUT \
            or adapter_name in (None, 'unsupported', 'unknown'):
        return 'proxy_list', False
    return 'proxy_list', True


QUICK = ('cur-02', 'cur-04', 'cur-46', 'cur-51', 'cur-55', 'new-024', 'new-009', 'new-045')
PROTOCOLS = ('http', 'https', 'socks4', 'socks5')

# The research recorded these as pages whose addresses the parser cannot read:
# the table is rendered client-side, or the rows live behind a script the
# application must not execute. They stay in the catalog with that stated.
NO_ADAPTER = ('new-011', 'new-012', 'new-050', 'new-052', 'new-057', 'new-065')
# Table sources whose column layout could not be reproduced from the saved
# sample, so the catalog states that instead of shipping a guessed plan.
UNCONFIRMED_LAYOUT = ('new-051', 'new-064')

# Adapter configuration confirmed against each source's own saved sample, with
# the first record the research observed. Reproduced by the honesty tests.
VERIFIED_ADAPTER_CONFIG = {
    'new-016': {'row_fragment': True, 'columns': {'ip': 0, 'port': 1, 'protocol': 5}},
    'new-066': {'row_fragment': True, 'columns': {'ip': 0, 'port': 1, 'protocol': 2}},
    'new-031': {'legacy_kind': 'auto', 'line_address': 'first-token'},
    'new-023': {'default_protocol': 'http'},
    'new-055': {'host_fields': ['query'], 'port_fields': ['port'], 'default_protocol': 'http',
                'metadata': {'country': 'countryCode'}},
}
# The snapshot itself was not available when the research ran.
SNAPSHOT_UNAVAILABLE = ('new-053',)
# Sources that existed in the old flat sources.json, and therefore may carry a
# legacy spec a user could still have in their settings.
LEGACY_PREFIX = 'cur-'


def _sets_for(sources: list[dict]) -> list[dict]:
    """Declare the shipped sets, so none of them can offer an uncollectable source."""
    known = {source['id'] for source in sources}
    revision = 2026092501
    collectable = [source for source in sources if source['payload_role'] == 'proxy_list'
                   and source['collection_allowed']]
    quick = [member for member in QUICK if member in known]
    quick_sources = {source['id'] for source in collectable}
    quick = [member for member in quick if member in quick_sources]
    sets = [
        {'id': 'quick', 'name': 'Базовый быстрый', 'kind': 'system', 'members': quick,
         'catalog_revision': revision},
        {'id': 'extended', 'name': 'Расширенный', 'kind': 'system', 'members': [],
         'catalog_revision': revision},
        {'id': 'experimental', 'name': 'Экспериментальные', 'kind': 'system', 'members': [],
         'catalog_revision': revision},
    ]
    for protocol in PROTOCOLS:
        sets.append({'id': f'protocol:{protocol}', 'name': protocol.upper(), 'kind': 'protocol',
                     'members': [source['id'] for source in collectable if protocol in source['protocols']],
                     'catalog_revision': revision})
    sets.append({'id': 'custom', 'name': 'Пользовательские', 'kind': 'custom', 'members': [],
                 'catalog_revision': revision})
    return sets


def main() -> int:
    from proxy_workbench import source_catalog

    research = Path(sys.argv[1] if len(sys.argv) > 1 else RESEARCH)
    out = Path(sys.argv[2] if len(sys.argv) > 2 else source_catalog.bundled_path())
    raw = json.loads(research.read_text(encoding='utf-8'))

    sources = []
    columns_file = Path(__file__).with_name('table-columns.json')
    derived_columns = json.loads(columns_file.read_text(encoding='utf-8')) if columns_file.is_file() else {}
    for entry in raw.get('sources') or []:
        source_id = entry.get('id')
        role, allowed = derive(entry)
        source = {**entry, 'payload_role': role, 'collection_allowed': allowed}
        if source_id and not source_id.startswith(LEGACY_PREFIX):
            # Never in anybody's old settings: there is no legacy spec to migrate.
            source['legacy_specs'] = []
        if source_id in NO_ADAPTER:
            source['adapter'] = 'unsupported'
            source['unknowns'] = [*entry.get('unknowns', []),
                                  'Разметка таблицы читается браузером или скриптом: приложение её не выполняет, '
                                  'поэтому адаптер не назначен.']
        elif source_id in UNCONFIRMED_LAYOUT:
            source['adapter'] = 'unsupported'
            source['unknowns'] = [*entry.get('unknowns', []),
                                  'Колонки таблицы не удалось подтвердить по сохранённому образцу: адаптер не назначен, '
                                  'чтобы источник не выглядел читаемым без основания.']
        elif source_id in derived_columns:
            source['adapter_config'] = derived_columns[source_id]
        elif source_id in VERIFIED_ADAPTER_CONFIG:
            source['adapter_config'] = VERIFIED_ADAPTER_CONFIG[source_id]
        if source_id in SNAPSHOT_UNAVAILABLE:
            current = source.get('access') if isinstance(source.get('access'), dict) else {'kind': 'unknown'}
            source['access'] = {**current, 'kind': 'snapshot_unavailable'}
        sources.append(source)

    staged = Path('/tmp/source-catalog-staged.json')
    # The sets are declared here rather than left to the loader's defaults, so
    # that protocol sets list only sources the collector may actually fetch.
    sets = _sets_for(sources)
    staged.write_text(json.dumps({**raw, 'sources': sources, 'sets': sets}, ensure_ascii=False, indent=2) + '\n',
                      encoding='utf-8')
    catalog = source_catalog.load_catalog(staged, allow_research=True, allow_unsafe=True)
    out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    tally = {}
    for source in catalog['sources']:
        key = f"{source['payload_role']}/{'collect' if source['collection_allowed'] else 'no-collect'}"
        tally[key] = tally.get(key, 0) + 1
    print(json.dumps({'written': str(out), 'sources': len(catalog['sources']), 'tally': tally},
                     ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
