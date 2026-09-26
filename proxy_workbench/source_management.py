"""Source catalog view shared by the GUI, the CLI and the read-only API.

The module turns the catalog, the user's selection and the runtime tables into
one bounded view.  It performs no network I/O and writes nothing:
``proxytool.collect`` stays the only network and persistence boundary, and
``gui.App`` stays the only writer of ``gui-settings.json``.

Wording rule enforced here by construction: a row is never called working,
alive or quality.  A source keeps five separate research statuses, the three
runtime fetch states, and the age of the data that is actually on disk.
"""
from __future__ import annotations

from copy import deepcopy
import sqlite3
import time

from . import source_catalog

MAX_QUERY = 200
MAX_ROWS = 500
MAX_DETAIL_HISTORY = 20
# Beyond this many generations the exclusive-contribution set is too expensive
# to compute on every catalog refresh; the UI shows the accepted count instead.
MAX_CONTRIBUTION_SOURCES = 200

ACCESS_GROUPS = (
    ("public_free", ("public", "fully_public_free")),
    ("permanent_free_quota", ("permanent_free_quota",)),
    ("free_with_key", ("free_with_api_key",)),
    ("trial", ("temporary_trial",)),
    ("paid", ("paid",)),
    ("own_infrastructure", ("own_infrastructure",)),
    ("snapshot_unavailable", ("snapshot_unavailable",)),
    ("unknown", ("unknown",)),
)
ACCESS_GROUP_OF = {kind: group for group, kinds in ACCESS_GROUPS for kind in kinds}
PROVIDER_GROUPS = ("public_free", "permanent_free_quota", "free_with_key", "trial", "paid")

ADAPTER_KINDS = ("line", "json-records", "fields", "page-json", "html-table")
# A format the application cannot read is described by why, never by calling the
# source dead, broken or low quality.
NOT_PROXY_REASONS = {
    "subscription_config": "Формат конфигурации клиента или подписки, а не список адресов прокси.",
    "self_hosted": "Собственная инфраструктура, а не публичный список.",
    "commercial_page": "Коммерческая страница, а не файл со списком.",
    "documentation": "Документация, а не файл со списком.",
    "snapshot_unavailable": "Снимок в исследовании получить не удалось.",
    "no_adapter": "Приложение не умеет читать этот формат: нет зарегистрированного адаптера.",
    "unknown_payload": "Назначение данных не установлено.",
}
ACCESS_REASONS = {
    "paid": "Платный доступ.",
    "temporary_trial": "Временный trial, а не постоянный бесплатный тариф.",
    "free_with_api_key": "Нужен API-ключ; приложение не хранит ключи.",
    "own_infrastructure": "Собственная инфраструктура.",
    "snapshot_unavailable": "Снимок в исследовании получить не удалось.",
    # Not an access rule and not a format rule: the catalog record itself
    # refuses collection.  The reason is in the record, and it is shown next to
    # this line rather than replaced by a guess.
    "catalog_not_enabled": "Каталог не разрешает сбор этого источника.",
}
FETCH_STATES = ("not_run", "not_attempted", "http_2xx_nonempty", "empty_body", "not_modified",
                "http_429", "http_error", "timeout", "blocked_destination", "redirect_blocked", "canceled")
CACHE_STATES = ("none", "last_good", "stale_last_good", "not_modified", "cache_evicted", "partial")
FILTER_STATES = ("selected", "unselected", "disabled", "custom", "not_proxy_source", "provider",
                 "needs_access", "rights_unresolved", "never_checked", "has_data", "last_good",
                 "stale", "failed", "quarantined", "collectable", "retired")
SORT_KEYS = ("name", "id", "category", "access", "last_attempt", "accepted", "exclusive")


def access_group(kind):
    return ACCESS_GROUP_OF.get(kind, "unknown")


# The bundled catalog classifies these by category.  A subscription-format entry
# is deliberately absent: its endpoint may be a client config, a Tor export or a
# multi-format repository, and "no registered adapter" is the one statement
# that stays true for all of them.
CATEGORY_REASONS = {
    "self-hosted": "self_hosted",
    "snapshot-unavailable": "snapshot_unavailable",
    "commercial": "commercial_page",
}


def not_proxy_reason(source):
    """Why this record is not a list of proxy addresses for this application.

    An access requirement is not a format problem, so a provider that needs an
    account, a key or a paid plan is explained by its access reason instead.
    """
    role = source.get("payload_role", "unknown")
    adapter_kind = (source.get("adapter") or {}).get("kind")
    if role == "proxy_list" and adapter_kind in source_catalog.ADAPTERS - {"unsupported"}:
        return None
    # A record that is meant to be a list but has no registered adapter says so
    # as a format problem, which is a different reason than its access terms.
    if role == "proxy_list" and adapter_kind not in source_catalog.ADAPTERS - {"unsupported"}:
        return "no_adapter"
    if role in NOT_PROXY_REASONS:
        return role
    category_reason = CATEGORY_REASONS.get(source.get("category"))
    if category_reason:
        return category_reason
    if not source.get("collection_allowed", True) or source.get("account_required"):
        return None
    if adapter_kind not in source_catalog.ADAPTERS - {"unsupported"}:
        return "no_adapter"
    return "unknown_payload"


def access_reason(source):
    access = source.get("access") or {}
    if access.get("account_required"):
        return "account_required"
    kind = access.get("kind")
    return ACCESS_REASONS.get(kind)


def _checked_at(source):
    rights = source.get("rights") or {}
    evidence = source.get("evidence") or {}
    return (rights.get("checked_at") or source.get("checked_at")
            or (evidence.get("url_reachable") or {}).get("checked_at"))


def _host(url):
    from urllib.parse import urlsplit
    try:
        return (urlsplit(url).hostname or '').lower()
    except (TypeError, ValueError):
        return ''


def terms_url(source):
    rights = source.get("rights") or {}
    return source.get("terms_url") or rights.get("terms_url") or source.get("documentation_url") or ''


def runtime_snapshot(db, now=None, source_ids=None):
    """Read-only aggregate of the runtime tables, keyed by stable source ID.

    Everything here is an observation of what the app already stored.  A source
    with no rows is reported as ``never_checked`` rather than as a failure.
    ``source_ids`` narrows every read to one detail view.
    """
    now = time.time() if now is None else now
    result = {}
    if db is None:
        return result
    wanted = [str(value) for value in source_ids] if source_ids else None
    if wanted is not None and not wanted:
        return result
    marks = '' if wanted is None else ', '.join('?' * len(wanted))

    def where(column):
        if wanted is None:
            return '', ()
        return f' WHERE {column} IN ({marks})', tuple(wanted)

    try:
        clause, params = where('source_id')
        for row in db.execute('SELECT source_id,endpoint_id,final_url,etag,last_modified,last_attempt_at,'
                              'last_success_at,last_body_at,last_304_at,current_generation,last_good_generation,'
                              'consecutive_failures,backoff_until,quarantine_until,retry_after,last_error'
                              ' FROM source_state' + clause, params):
            item = result.setdefault(row[0], _empty_runtime(now))
            previous = item['endpoints'].get(row[1])
            if previous is None or (row[5] or 0) > (previous.get('last_attempt_at') or 0):
                item['endpoints'][row[1]] = dict(final_url=row[2], etag=row[3], last_modified=row[4],
                                                 last_attempt_at=row[5], last_success_at=row[6], last_body_at=row[7],
                                                 last_304_at=row[8], current_generation=row[9],
                                                 last_good_generation=row[10], consecutive_failures=row[11] or 0,
                                                 backoff_until=row[12], quarantine_until=row[13],
                                                 retry_after=row[14], last_error=row[15])
    except sqlite3.Error:
        return result
    try:
        clause, params = where('source_id')
        latest = dict(db.execute('SELECT source_id,MAX(id) FROM source_observation' + clause
                                 + ' GROUP BY source_id', params))
        for source_id, observation_id in latest.items():
            row = db.execute('SELECT * FROM source_observation WHERE id=?', (observation_id,)).fetchone()
            item = result.setdefault(source_id, _empty_runtime(now))
            if row is None:
                continue
            names = [column[0] for column in db.execute('SELECT * FROM source_observation WHERE id=?',
                                                        (observation_id,)).description]
            observation = dict(zip(names, row))
            item.update(http_state=observation.get('http_state'), parse_state=observation.get('parse_state'),
                        cache_state=observation.get('cache_state'), outcome=observation.get('outcome'),
                        status=observation.get('status'), attempts=observation.get('attempts') or 0,
                        pages=observation.get('pages') or 0, bytes=observation.get('bytes') or 0,
                        received=observation.get('received') or 0, recognized=observation.get('recognized') or 0,
                        accepted=observation.get('accepted') or 0, rejected=observation.get('rejected') or 0,
                        duplicate=observation.get('duplicate') or 0, blocked=observation.get('blocked') or 0,
                        new_endpoints=observation.get('new_endpoints') or 0,
                        partial=bool(observation.get('partial')), error=observation.get('error'),
                        retryable=bool(observation.get('retryable')), retry_after=observation.get('retry_after'),
                        fallback_used=bool(observation.get('fallback_used')),
                        observed_at=observation.get('ended_at') or observation.get('started_at'),
                        endpoint_id=observation.get('endpoint_id'))
    except sqlite3.Error:
        pass
    try:
        clause, params = where('source_id')
        for source_id, state, active, last_good, created_at, record_count in db.execute(
                'SELECT source_id,state,active,last_good,created_at,record_count FROM source_generation'
                + clause + ' ORDER BY id', params):
            item = result.setdefault(source_id, _empty_runtime(now))
            candidate = dict(state=state, active=bool(active), last_good=bool(last_good),
                             created_at=created_at, record_count=record_count)
            best = item.get('generation')
            if best is None or (candidate['active'], candidate['last_good'], candidate['created_at'] or 0) > \
                    (best['active'], best['last_good'], best['created_at'] or 0):
                item['generation'] = candidate
                if candidate['active'] or candidate['last_good']:
                    item['last_good_at'] = created_at
                    item['last_good_records'] = record_count
    except sqlite3.Error:
        pass
    try:
        clause, params = where('source')
        for source_id, count in db.execute('SELECT source,COUNT(*) FROM candidate_seen' + clause
                                           + ' GROUP BY source', params):
            result.setdefault(source_id, _empty_runtime(now))['membership'] = count
    except sqlite3.Error:
        pass
    try:
        newest = {}
        clause, params = where('source_id')
        for app_run_id, source_id, checked, passed in db.execute(
                'SELECT app_run_id,source_id,checked_by_app,passed_profile FROM source_scan_stat' + clause, params):
            if app_run_id >= newest.get(source_id, ('',))[0]:
                newest[source_id] = (app_run_id, checked, passed)
        for source_id, (_, checked, passed) in newest.items():
            result.setdefault(source_id, _empty_runtime(now)).update(checked_by_app=checked, passed_profile=passed)
    except sqlite3.Error:
        pass
    try:
        clause, params = where('source_id')
        for source_id, count in db.execute('SELECT source_id,COUNT(*) FROM candidate_scope_exclusion' + clause
                                           + ' GROUP BY source_id', params):
            result.setdefault(source_id, _empty_runtime(now))['scope_excluded'] = count
    except sqlite3.Error:
        pass
    for item in result.values():
        state = max(item['endpoints'].values(), key=lambda value: value.get('last_attempt_at') or 0,
                    default=None)
        if state:
            item.update(last_attempt_at=state.get('last_attempt_at'), last_success_at=state.get('last_success_at'),
                        last_body_at=state.get('last_body_at'), last_304_at=state.get('last_304_at'),
                        consecutive_failures=state.get('consecutive_failures'),
                        backoff_until=state.get('backoff_until'), quarantine_until=state.get('quarantine_until'),
                        retry_after=state.get('retry_after'), state_error=state.get('last_error'),
                        state_etag=state.get('etag'), state_last_modified=state.get('last_modified'))
        # A transport failure without a stored observation is still a reason.
        if not item.get('error') and item.get('state_error'):
            item['error'] = item['state_error']
        item.pop('endpoints', None)
    return result


def _empty_runtime(now):
    return {'http_state': 'not_run', 'parse_state': 'not_run', 'cache_state': 'none', 'outcome': None,
            'status': None, 'attempts': 0, 'pages': 0, 'bytes': 0, 'received': 0, 'recognized': 0,
            'accepted': 0, 'rejected': 0, 'duplicate': 0, 'blocked': 0, 'new_endpoints': 0, 'partial': False,
            'error': None, 'retryable': False, 'retry_after': None, 'fallback_used': False, 'observed_at': None,
            'endpoint_id': None, 'last_attempt_at': None, 'last_success_at': None, 'last_body_at': None,
            'last_304_at': None, 'consecutive_failures': 0, 'backoff_until': None, 'quarantine_until': None,
            'state_error': None, 'state_etag': None, 'state_last_modified': None,
            'generation': None, 'last_good_at': None, 'last_good_records': 0, 'membership': 0,
            'checked_by_app': None, 'passed_profile': None, 'scope_excluded': 0, 'contribution': None,
            'endpoints': {}, 'data_age_seconds': None, 'last_good_age_seconds': None, 'now': now}


def attach_contributions(runtime, db, source_ids):
    """Exclusive/leave-one-out contribution over the active generations."""
    ids = [item for item in source_ids if item in runtime]
    if db is None or not ids or len(ids) > MAX_CONTRIBUTION_SOURCES:
        return runtime
    from . import proxytool
    # proxytool grows independently of this module; an older engine that has not
    # learned the query yet leaves the contribution honestly absent.
    if hasattr(proxytool, 'source_contributions'):
        for source_id, values in proxytool.source_contributions(db, ids).items():
            runtime[source_id]['contribution'] = values
    return runtime


def _age(runtime_item, key, now):
    value = runtime_item.get(key)
    if not value:
        return None
    return max(0, int(now - value))


def public_row(source, *, selected_ids=(), disabled_ids=(), set_ids=(), runtime=None, custom=False,
               redact=True, now=None):
    """One catalog row: what the catalog claims, what the app stored, what the user chose."""
    now = time.time() if now is None else now
    runtime_item = dict(runtime or _empty_runtime(now))
    runtime_item['data_age_seconds'] = _age(runtime_item, 'last_body_at', now)
    runtime_item['last_good_age_seconds'] = _age(runtime_item, 'last_good_at', now)
    runtime_item.pop('now', None)
    runtime_item.pop('endpoints', None)
    evidence = source.get('evidence') or {}
    reason = not_proxy_reason(source)
    blocked = access_reason(source) if source.get('collection_allowed', True) is False else None
    if blocked is None and source.get('collection_allowed', True) is False:
        # A record can refuse collection without an access rule naming it. The
        # catalog's own words are the only honest answer, so they are carried
        # through instead of the row pretending there is no explanation.
        blocked = 'catalog_not_enabled'
    rights = source.get('rights') or {}
    endpoints = [{'id': item.get('id'), 'role': item.get('role'), 'url': _endpoint_url(item.get('url', ''), redact)}
                 for item in source.get('endpoints') or []]
    row = {
        'id': source['id'],
        'name': source.get('name') or source['id'],
        'publisher': source.get('publisher') or {},
        'family_id': source.get('family_id'),
        'dataset_group': source_catalog.dataset_group_of(source),
        'category': source.get('category', 'unknown'),
        'homepage': source.get('homepage', ''),
        'documentation_url': source.get('documentation_url', ''),
        'terms_url': terms_url(source),
        'checked_at': _checked_at(source),
        'endpoints': endpoints,
        'protocols': list(source.get('protocols') or []),
        'formats': list(source.get('formats') or []),
        'adapter': (source.get('adapter') or {}).get('kind') or 'none',
        'support': source_catalog.support_status(source),
        'payload_role': source.get('payload_role', 'unknown'),
        'access': (source.get('access') or {}).get('kind', 'unknown'),
        'access_group': access_group((source.get('access') or {}).get('kind')),
        'access_note': source.get('access_note', ''),
        'quota_text': source.get('quota_text', 'unknown'),
        'account_required': bool(source.get('account_required')),
        'collection_allowed': bool(source.get('collection_allowed', True)),
        'collection_note': (source.get('priority_reason') or source.get('access_note', '')),
        'rights': {'terms_url': terms_url(source), 'data_license': rights.get('data_license', 'unknown'),
                   'code_license': rights.get('code_license', 'unknown'), 'checked_at': rights.get('checked_at')},
        'rights_approved': bool(source.get('rights_approved')),
        'evidence': {key: (evidence.get(key) or {}) for key in source_catalog.EVIDENCE_STATES},
        'not_proxy_source': reason,
        'not_proxy_source_reason': NOT_PROXY_REASONS.get(reason, ''),
        'access_blocked_reason': ACCESS_REASONS.get(blocked, 'Требуется отдельный доступ, которого у приложения нет.' if blocked else None),
        'access_blocked_code': blocked,
        'custom': bool(custom),
        'retired': bool(source.get('catalog_retired')),
        'selected': source['id'] in set(selected_ids),
        'download_disabled': source['id'] in set(disabled_ids),
        'sets': sorted(set(set_ids)),
        'runtime': runtime_item,
    }
    row['collectable'] = source_catalog.collectable_source(source)
    row['state'] = _row_state(row, now)
    row['selection_state'] = _selection_state(row)
    return row


def _endpoint_url(url, redact):
    if not redact:
        return url
    from .proxytool import public_url
    return public_url(url)


def _row_state(row, now):
    """Data and format state of one source, kept apart from the user's choice.

    Never 'working', 'dead' or 'quality': the label only reports what the
    application itself observed, and the selection lives in its own field.
    """
    runtime_item = row['runtime']
    if row['custom']:
        return 'custom'
    if row['retired']:
        return 'retired'
    if not row['collectable']:
        if row['access_blocked_reason']:
            return 'needs_access'
        if row['rights']['data_license'] not in ('unknown', '', None):
            return 'rights_unresolved'
        return 'not_proxy_source'
    if runtime_item.get('quarantine_until') and runtime_item['quarantine_until'] > now:
        return 'quarantined'
    if runtime_item.get('error'):
        return 'failed'
    if runtime_item.get('cache_state') in ('stale_last_good', 'partial'):
        return 'stale'
    if runtime_item.get('generation'):
        return 'last_good'
    if runtime_item.get('observed_at'):
        return 'has_data'
    return 'never_checked'


def _selection_state(row):
    """The user's own choice, reported apart from the observed data state."""
    if row['download_disabled']:
        return 'disabled'
    return 'selected' if row['selected'] else 'unselected'


STATE_FILTERS = {
    'selected': lambda row, now: row['selected'],
    'unselected': lambda row, now: not row['selected'],
    'disabled': lambda row, now: row['download_disabled'],
    'custom': lambda row, now: row['custom'],
    'not_proxy_source': lambda row, now: bool(row['not_proxy_source']),
    'provider': lambda row, now: row['access_group'] in PROVIDER_GROUPS,
    'needs_access': lambda row, now: bool(row['access_blocked_reason']),
    # The same rule the row badge uses, so a filter and the badge it filters on
    # can never answer different questions about the same row.
    'rights_unresolved': lambda row, now: (not row['collectable'] and not row['rights_approved']
                                           and row['rights']['data_license'] not in ('unknown', '', None)),
    'never_checked': lambda row, now: row['runtime'].get('observed_at') is None,
    'has_data': lambda row, now: row['runtime'].get('observed_at') is not None,
    'last_good': lambda row, now: bool(row['runtime'].get('generation')),
    'stale': lambda row, now: row['runtime'].get('cache_state') in ('stale_last_good', 'partial'),
    'failed': lambda row, now: bool(row['runtime'].get('error')),
    'quarantined': lambda row, now: bool(row['runtime'].get('quarantine_until') and row['runtime']['quarantine_until'] > now),
    'collectable': lambda row, now: row['collectable'],
    'retired': lambda row, now: row['retired'],
}


def _matches_query(row, text):
    if not text:
        return True
    haystack = ' '.join(str(value).lower() for value in (
        row['id'], row['name'], row['publisher'].get('id'), row['publisher'].get('name'),
        row['family_id'], row['category'], ' '.join(row['protocols']), ' '.join(row['formats']),
        ' '.join(endpoint['url'] for endpoint in row['endpoints'])))
    return text in haystack


def parse_query(query):
    """Validated filters from a query dict or a parsed query string."""
    if not query:
        return {}
    if isinstance(query, str):
        from urllib.parse import parse_qs
        query = {key: items[-1] for key, items in parse_qs(query, keep_blank_values=True).items()}
    result = {}
    text = str(query.get('q', '')).strip().lower()
    if len(text) > MAX_QUERY:
        raise ValueError(f'Поиск: максимум {MAX_QUERY} символов.')
    result['q'] = text
    for key in ('set', 'category', 'protocol', 'format', 'access', 'state', 'sort'):
        value = str(query.get(key, '') or '').strip()
        if len(value) > 100:
            raise ValueError(f'Фильтр {key}: максимум 100 символов.')
        result[key] = value
    if result['state'] and result['state'] not in FILTER_STATES:
        raise ValueError('Неизвестный фильтр состояния.')
    if result['sort'] and result['sort'] not in SORT_KEYS:
        raise ValueError('Неизвестная сортировка.')
    for key, default in (('limit', 200), ('offset', 0)):
        try:
            value = int(query.get(key, default))
        except (TypeError, ValueError):
            raise ValueError(f'{key}: ожидается целое число.') from None
        if key == 'limit':
            if not 1 <= value <= MAX_ROWS:
                raise ValueError(f'limit: от 1 до {MAX_ROWS}.')
        elif not 0 <= value <= 100_000:
            raise ValueError('offset: слишком большое значение.')
        result[key] = value
    return result


def selection_of(value):
    """Accept either a full settings dict or a bare ``source_selection``."""
    if not isinstance(value, dict):
        return {}
    inner = value.get('source_selection')
    return inner if isinstance(inner, dict) else value


def sets_view(catalog, selection, applied=()):
    """Catalog sets plus what the user's already-applied snapshot is missing."""
    selection = selection_of(selection)
    selected = set(selection.get('selected_ids', []))
    custom_ids = [item.get('id') for item in selection.get('custom_sources', []) if isinstance(item, dict)]
    applied_sets = {item.get('id'): item for item in selection.get('sets', []) if isinstance(item, dict)}
    result = []
    for definition in catalog.get('sets', []):
        members = source_catalog.set_members(definition, custom_ids)
        applied_item = applied_sets.get(definition['id'])
        snapshot = list((applied_item or {}).get('members') or [])
        result.append({
            'id': definition['id'], 'name': definition.get('name', definition['id']),
            'kind': definition.get('kind', 'system'), 'members': len(members),
            'member_ids': members[:MAX_ROWS],
            'applied': bool(applied_item),
            'applied_members': len(snapshot),
            'new_members': [value for value in members if value not in snapshot] if applied_item else [],
            'retired_members': [value for value in snapshot if value not in set(members)] if applied_item else [],
            'selected_members': len([value for value in members if value in selected]),
        })
    return result


def provider_groups(rows):
    """Access conditions shown as separate groups with the date they were checked."""
    result = []
    for group, _kinds in ACCESS_GROUPS:
        members = [row for row in rows if row['access_group'] == group]
        checked = sorted(value for value in (row['checked_at'] for row in members) if value)
        terms = []
        for row in members:
            url = row['terms_url']
            if url and url not in terms:
                terms.append(url)
        result.append({'id': group, 'count': len(members),
                       'checked_at': checked[-1] if checked else None,
                       'terms_urls': terms[:10],
                       'ids': [row['id'] for row in members][:MAX_ROWS]})
    return result


def build_view(catalog, selection, runtime=None, query=None, *, redact=True, now=None, contributions=True,
               db=None):
    """Filtered, paginated catalog view plus facets and the provider groups."""
    now = time.time() if now is None else now
    runtime = runtime if runtime is not None else {}
    selection = selection_of(selection)
    filters = parse_query(query)
    disabled = list(selection.get('download_disabled_ids', []))
    selected_ids = list(selection.get('selected_ids', []))
    custom = {item.get('id'): item for item in selection.get('custom_sources', []) if isinstance(item, dict)}
    set_members = {definition['id']: set(source_catalog.set_members(definition, custom))
                   for definition in catalog.get('sets', [])}
    rows = []
    for source in catalog.get('sources', []):
        if source['id'] in custom:
            continue
        rows.append(public_row(source, selected_ids=selected_ids, disabled_ids=disabled,
                               set_ids=[key for key, members in set_members.items() if source['id'] in members],
                               runtime=runtime.get(source['id']), redact=redact, now=now))
    for source_id, item in custom.items():
        record = _custom_view_record(source_id, item)
        if record is None:
            continue
        rows.append(public_row(record, selected_ids=selected_ids, disabled_ids=disabled,
                               set_ids=[key for key, members in set_members.items() if source_id in members],
                               runtime=runtime.get(source_id), custom=True, redact=redact, now=now))
    # A source the accepted catalog no longer lists stays selected and keeps its
    # stored data, so it must stay a row: otherwise the count of the selection
    # and the rows on screen disagree and the user cannot undo their own choice.
    for source_id in _retired_selection(selected_ids, disabled, rows, custom):
        rows.append(public_row(_retired_view_record(source_id), selected_ids=selected_ids,
                               disabled_ids=disabled, runtime=runtime.get(source_id), now=now))
    if contributions and db is not None:
        attach_contributions(runtime, db, [row['id'] for row in rows])
        for row in rows:
            if row['id'] in runtime:
                row['runtime']['contribution'] = runtime[row['id']].get('contribution')
    if filters.get('q'):
        rows = [row for row in rows if _matches_query(row, filters['q'])]
    for key, values in (('category', 'category'), ('protocol', 'protocols'), ('format', 'formats')):
        wanted = filters.get(key)
        if wanted:
            wanted = {value.strip() for value in wanted.split(',') if value.strip()}
            rows = [row for row in rows if wanted & set(row[values])]
    if filters.get('access'):
        # Accepts the group the UI shows and the raw catalog access kind.
        wanted = {value.strip() for value in filters['access'].split(',') if value.strip()}
        rows = [row for row in rows
                if wanted & {row['access'], row['access_group'], access_group(row['access'])}]
    if filters.get('set'):
        wanted = filters['set']
        rows = [row for row in rows if wanted in row['sets']]
    if filters.get('state'):
        predicate = STATE_FILTERS[filters['state']]
        rows = [row for row in rows if predicate(row, now)]
    custom_ids = [item.get('id') for item in (selection.get('custom_sources') or [])
                  if isinstance(item, dict) and item.get('id')]
    rows = _sort_rows(rows, filters.get('sort') or 'name', custom_ids)
    total = len(rows)
    offset = filters.get('offset', 0)
    limit = filters.get('limit', 200)
    page = rows[offset:offset + limit]
    return {
        'schema_version': source_catalog.CATALOG_SCHEMA_VERSION,
        'catalog_id': catalog.get('catalog_id'),
        'revision': catalog.get('revision'),
        'published_at': catalog.get('published_at'),
        'minimum_app_version': catalog.get('minimum_app_version'),
        'selection': {'selected': len([row for row in rows if row['selected']]),
                      'disabled': len([row for row in rows if row['download_disabled']]),
                      'selected_ids': [row['id'] for row in page if row['selected']],
                      'disabled_ids': [row['id'] for row in page if row['download_disabled']],
                      'applied_sets': [item.get('id') for item in selection.get('sets', []) if isinstance(item, dict)],
                      'retired': [row['id'] for row in rows if row['retired']][:MAX_ROWS],
                      'materialized': len(selection.get('selected_ids', []))},
        'filters': filters,
        'total': total,
        'offset': offset,
        'limit': limit,
        'sources': page,
        'sets': sets_view(catalog, selection),
        'access_groups': provider_groups(rows),
        'facets': _facets(rows),
        'custom_count': len(custom),
    }


def _custom_view_record(source_id, item):
    url = item.get('url')
    if not isinstance(url, str) or not url:
        return None
    adapter = item.get('adapter') or {}
    return {'id': source_id, 'name': item.get('name') or url, 'publisher': {'id': 'local', 'name': 'Пользователь'},
            'family_id': source_id, 'category': 'custom', 'homepage': '', 'documentation_url': '',
            'terms_url': '', 'checked_at': None, 'data_urls': [url], 'fallback_urls': [],
            'endpoints': [{'id': 'primary', 'url': url, 'role': 'primary', 'relation': 'custom'}],
            'protocols': list(adapter.get('protocols') or []), 'formats': [],
            'adapter': {'kind': adapter.get('kind', 'line')}, 'payload_role': 'proxy_list',
            'access': {'kind': 'public', 'account_required': False, 'quota_text': 'unknown'},
            'access_note': '', 'quota_text': 'unknown', 'account_required': False,
            'collection_allowed': True, 'rights': {'terms_url': '', 'data_license': 'unknown', 'code_license': 'unknown',
                                                   'checked_at': None},
            'rights_approved': False, 'evidence': {key: {'state': 'not_run', 'checked_at': None}
                                                   for key in source_catalog.EVIDENCE_STATES},
            'maturity': 'custom', 'catalog_state': 'custom', 'tags': ['custom']}


def _retired_selection(selected_ids, disabled, rows, custom):
    """Selected ids the accepted catalog and the local custom list dropped."""
    known = {row['id'] for row in rows} | set(custom)
    return [source_id for source_id in selected_ids
            if source_id not in known and source_id not in set(disabled)]


def _retired_view_record(source_id):
    """A placeholder for a source the catalog no longer lists.

    The selection, the disables and the stored data all survive the catalog
    update, so the row must exist too: otherwise the source is selected but
    invisible and cannot be taken back out of the set.
    """
    return {'id': source_id, 'name': source_id, 'publisher': {'id': 'retired', 'name': '—'},
            'family_id': source_id, 'category': 'retired', 'homepage': '', 'documentation_url': '',
            'terms_url': '', 'checked_at': None, 'data_urls': [], 'fallback_urls': [], 'endpoints': [],
            'protocols': [], 'formats': [], 'adapter': {'kind': 'none'}, 'payload_role': 'unknown',
            'access': {'kind': 'unknown', 'account_required': False, 'quota_text': 'unknown'},
            'access_note': '', 'quota_text': 'unknown', 'account_required': False,
            'collection_allowed': False, 'rights': {'terms_url': '', 'data_license': 'unknown',
                                                    'code_license': 'unknown', 'checked_at': None},
            'rights_approved': False, 'catalog_retired': True,
            'evidence': {key: {'state': 'not_run', 'checked_at': None} for key in source_catalog.EVIDENCE_STATES},
            'maturity': 'retired', 'catalog_state': 'retired', 'tags': ['retired']}


def _sort_rows(rows, key, custom_order=None):
    runtime_contribution = lambda row: (row['runtime'].get('contribution') or {}).get('exclusive') or 0
    # A user's own lists keep the order they added them in: the catalog screen
    # and the state filter must not disagree about the same two rows.
    if custom_order:
        order = {source_id: index for index, source_id in enumerate(custom_order)}

        def custom_first(row):
            if row['id'] in order:
                return (0, order[row['id']], '')
            return (1, 0, str(row['name']).lower())
    else:
        custom_first = None
    keys = {
        'name': lambda row: (str(row['name']).lower(), row['id']),
        'id': lambda row: row['id'],
        'category': lambda row: (str(row['category']), str(row['name']).lower()),
        'access': lambda row: (row['access_group'], str(row['name']).lower()),
        'last_attempt': lambda row: -(row['runtime'].get('last_attempt_at') or 0),
        'accepted': lambda row: -(row['runtime'].get('accepted') or 0),
        'exclusive': lambda row: -runtime_contribution(row),
    }
    if custom_first is not None:
        return sorted(rows, key=lambda row: (custom_first(row), keys.get(key, keys['name'])(row)))
    return sorted(rows, key=keys.get(key, keys['name']))


def _facets(rows):
    def counter(values):
        result = {}
        for value in values:
            result[value] = result.get(value, 0) + 1
        return dict(sorted(result.items()))
    return {
        'categories': counter([row['category'] for row in rows]),
        'protocols': counter([value for row in rows for value in row['protocols']]),
        'formats': counter([value for row in rows for value in row['formats']]),
        'access': counter([row['access'] for row in rows]),
        'access_groups': counter([row['access_group'] for row in rows]),
        'states': counter([row['state'] for row in rows]),
        'adapters': counter([row['adapter'] for row in rows]),
    }


def detail_view(catalog, selection, source_id, runtime=None, db=None, *, now=None, redact=True):
    """One source with its five research statuses, rights, history and cache.

    Only the requested row is built, so opening a detail does not cost the
    whole catalog on a large local database.
    """
    now = time.time() if now is None else now
    selection = selection_of(selection)
    runtime = runtime if runtime is not None else {}
    custom = {item.get('id'): item for item in selection.get('custom_sources', []) if isinstance(item, dict)}
    set_members = {definition['id']: set(source_catalog.set_members(definition, custom))
                   for definition in catalog.get('sets', [])}
    source = source_catalog.source_by_id(catalog, source_id)
    if source is None and source_id in custom:
        source = _custom_view_record(source_id, custom[source_id])
    retired = source is None and (source_id in set(selection.get('selected_ids', []))
                                  or source_id in set(selection.get('download_disabled_ids', [])))
    if source is None:
        if not retired:
            return None
        source = _retired_view_record(source_id)
    row = public_row(source, selected_ids=selection.get('selected_ids', []),
                     disabled_ids=selection.get('download_disabled_ids', []),
                     set_ids=[key for key, members in set_members.items() if source_id in members],
                     runtime=runtime.get(source_id), custom=source_id in custom,
                     redact=redact, now=now)
    if db is not None and source_id in runtime:
        attach_contributions(runtime, db, [source_id])
        row['runtime']['contribution'] = runtime[source_id].get('contribution')
    row['history'] = history(db, source_id)
    row['cache'] = cache_state(db, source_id, now)
    row['research'] = _research(source_catalog.source_by_id(catalog, source_id))
    return row


def _research(source):
    if source is None:
        return {}
    return {
        'maturity': source.get('maturity', 'unknown'),
        'catalog_state': source.get('catalog_state', 'listed'),
        'priority': source.get('priority', 'normal'),
        'priority_reason': source.get('priority_reason', ''),
        'relation_to_current': source.get('relation_to_current', 'new_source'),
        'update_claim': source.get('update_claim', ''),
        'license_status': source.get('license_status', ''),
        'quota_text': source.get('quota_text', 'unknown'),
        'access_note': source.get('access_note', ''),
        'unknowns': list(source.get('unknowns') or []),
        'research_refs': list(source.get('research_refs') or []),
        'tags': list(source.get('tags') or []),
    }


def history(db, source_id, limit=MAX_DETAIL_HISTORY):
    if db is None:
        return []
    try:
        rows = db.execute('''SELECT id,run_id,endpoint_id,started_at,ended_at,http_state,parse_state,cache_state,
                                   outcome,status,attempts,pages,bytes,received,recognized,accepted,rejected,
                                   blocked,new_endpoints,partial,error,retryable,fallback_used,retry_after
                            FROM source_observation WHERE source_id=? ORDER BY id DESC LIMIT ?''',
                          (source_id, int(limit)))
        names = [column[0] for column in rows.description]
        return [dict(zip(names, row)) for row in rows]
    except sqlite3.Error:
        return []


def cache_state(db, source_id, now=None):
    """What is actually stored for this source, with its age."""
    now = time.time() if now is None else now
    if db is None:
        return None
    try:
        rows = db.execute('''SELECT id,state,active,last_good,created_at,record_count,estimated_bytes,endpoint_url
                             FROM source_generation WHERE source_id=? ORDER BY id DESC LIMIT 10''', (source_id,))
        generations = [dict(zip(('id', 'state', 'active', 'last_good', 'created_at', 'record_count',
                                 'estimated_bytes', 'endpoint_url'), row)) for row in rows]
        # The stored endpoint is the URL after every redirect, query string
        # included. A CDN that signs its list would leak that signature through
        # the API, so it is redacted the same way every other shown URL is.
        for item in generations:
            item['endpoint_url'] = _endpoint_url(item.get('endpoint_url'), True)
        totals = dict(db.execute('SELECT source_id,COALESCE(SUM(estimated_bytes),0) FROM source_generation'
                                 ' GROUP BY source_id').fetchall()).get(source_id, 0)
    except sqlite3.Error:
        return None
    last_good = next((item for item in generations if item['active'] or item['last_good']), None)
    return {'generations': generations, 'last_good': last_good,
            'last_good_age_seconds': max(0, int(now - last_good['created_at'])) if last_good and last_good['created_at'] else None,
            'cached_bytes': totals}


def source_addresses(db, source_id, *, exclusive=False, limit=200_000, dataset_groups=None):
    """Addresses this source already delivered to the local database.

    ``exclusive=True`` keeps only addresses that no other publisher family
    offers, so excluding them from the current scope removes this source's own
    contribution without touching addresses somebody else still provides.

    ``dataset_groups`` is the catalog's ``{source_id: dataset group}`` map.  It
    is optional and defaults to the family rule; when it is given, a source
    whose dataset another source already serves is not an independent
    publisher even if the two come from different people, which is exactly the
    case a snapshot comparison in the source research found.
    """
    if db is None:
        return set()
    try:
        own = {row[0] for row in db.execute('SELECT proxy FROM candidate_seen WHERE source=?', (source_id,))}
        if not exclusive or not own:
            return set(own) if not exclusive else set()
        families = dict(db.execute('SELECT source_id,family_id FROM source_identity WHERE family_id IS NOT NULL'))
        mine = families.get(source_id, source_id)
        if isinstance(dataset_groups, dict) and dataset_groups:
            mine = dataset_groups.get(source_id, mine)
        # The comparison is a set question, so it is asked of the index instead
        # of by reading every membership row into Python: this runs under the
        # workbench lock, and a full scan there blocks the whole application.
        db.execute('CREATE TEMP TABLE IF NOT EXISTS own_source_address (proxy TEXT PRIMARY KEY)')
        db.execute('DELETE FROM own_source_address')
        db.executemany('INSERT OR IGNORE INTO own_source_address VALUES (?)', ((proxy,) for proxy in own))
        # Same rule as before, asked of the index: another source, in another
        # publisher family, also offers the address. A source with no family
        # row counts as its own family.
        pairs = db.execute(
            'SELECT DISTINCT c.proxy, c.source FROM candidate_seen c'
            ' JOIN own_source_address o ON o.proxy = c.proxy'
            ' LEFT JOIN source_identity i ON i.source_id = c.source'
            ' WHERE c.source <> ?', (source_id,)).fetchall()
        if isinstance(dataset_groups, dict) and dataset_groups:
            # The identity of a *dataset* is a property of the catalog, not a
            # column, so the last step of the comparison is done in Python on
            # the joined pairs only -- never on the whole membership table.
            others = {proxy for proxy, other in pairs
                      if dataset_groups.get(other, families.get(other, other)) != mine}
        else:
            others = {proxy for proxy, other in pairs
                      if families.get(other, other) != mine}
    except sqlite3.Error:
        return set()
    result = own - others
    if len(result) > limit:
        return set(sorted(result)[:limit])
    return result


LIMIT_CODES = ('SOURCE_TOO_LARGE', 'SOURCE_LINE_TOO_LARGE', 'SOURCE_CANDIDATE_LIMIT',
               'SOURCE_RECORD_LIMIT', 'SOURCE_PAGE_LIMIT', 'SOURCE_TRUNCATED')


def _preview_outcome(entry):
    """Outcome of a preview, including the built-in line reports.

    The nine legacy kinds are read by the streaming parser, whose report has no
    outcome of its own; deriving it here keeps "hit the preview bound" from
    reading as "the source is unavailable".
    """
    if entry.get('outcome'):
        return entry['outcome']
    if entry.get('error') in LIMIT_CODES:
        return 'limit_exceeded'
    if entry.get('complete'):
        return 'available' if entry.get('rows') else 'empty'
    return 'unavailable'


def preview_view(report, source_id=None, name=None):
    """Shape one ``collect(preview=True)`` report into an honest preview.

    Accepted addresses are recognised records, not verified proxies: the app
    never checked a single one of them here.  ``truncated`` says the preview
    stopped at its own byte/record bound, which is a fact about the check and
    not about the source.
    """
    from . import proxytool
    sources = report.get('sources') or []
    entry = sources[0] if sources else {}
    rejects = {reason: count for reason, count in (entry.get('reject_reasons') or {}).items() if count}
    error = entry.get('error')
    limits = {'max_bytes': proxytool.PREVIEW_MAX_BYTES, 'max_candidates': proxytool.PREVIEW_MAX_CANDIDATES}
    return {
        'preview': True,
        'source_id': source_id or entry.get('source_id'),
        'name': name,
        'http_state': entry.get('http_state', 'not_attempted'),
        'parse_state': entry.get('parse_state', 'not_run'),
        'cache_state': entry.get('cache_state', 'none'),
        'outcome': _preview_outcome(entry),
        'complete': bool(entry.get('complete')),
        'truncated': bool(entry.get('partial') or error in LIMIT_CODES),
        'limits': limits,
        'partial': bool(entry.get('partial')),
        'error': error,
        'status': entry.get('status'),
        'format': entry.get('format'),
        'pages': entry.get('pages', 0),
        'bytes': entry.get('bytes', 0),
        'attempts': entry.get('attempts', 0),
        'fallback_used': bool(entry.get('fallback_used')),
        'recognized': entry.get('recognized', 0),
        'accepted': entry.get('accepted', 0),
        'rejected': entry.get('rejected', 0),
        'blocked': entry.get('blocked', 0),
        'duplicate': entry.get('duplicate', 0),
        'new_endpoints': entry.get('new_endpoints', 0),
        'reject_reasons': rejects,
        'sample': list(report.get('sample') or [])[:20],
        'unique': report.get('unique', 0),
        'proxies_checked': 0,
    }


def read_settings(data):
    """Current settings through the one validator, or None when unreadable."""
    from . import gui
    return gui.read_settings(data)


def write_settings(data, settings):
    """Persist a changed selection with the same validation the GUI uses."""
    from . import gui
    return gui.save_settings(data, settings)


def apply_set(settings, set_id, catalog=None, *, keep_disabled=True):
    return source_catalog.apply_set(settings, set_id, catalog, keep_disabled=keep_disabled)


def set_downloads(settings, source_ids, disabled, catalog=None):
    return source_catalog.set_disabled(settings, source_ids, disabled, catalog)


def remove_sources(settings, source_ids, catalog=None):
    return source_catalog.remove_ids(settings, source_ids, catalog)


def select_ids(settings, source_ids, catalog=None):
    """Add IDs to the selection without dropping anything the user removed."""
    result = deepcopy(settings)
    selection = deepcopy(result.get('source_selection') or {})
    catalog = catalog or source_catalog.load_bundled()
    custom = {item.get('id') for item in selection.get('custom_sources', []) if isinstance(item, dict)}
    selected = list(selection.get('selected_ids', []))
    disabled = list(selection.get('download_disabled_ids', []))
    for source_id in source_ids or []:
        if source_catalog.source_by_id(catalog, source_id) is None and source_id not in custom:
            continue
        # Already in the set: taking it back in must not take it out again.
        if source_id not in selected:
            selected.append(source_id)
        disabled = [value for value in disabled if value != source_id]
    selection['selected_ids'] = list(dict.fromkeys(selected))
    selection['download_disabled_ids'] = disabled
    selection['catalog_revision'] = catalog['revision']
    result['source_selection'] = selection
    result['settings_version'] = 3
    return source_catalog.migrate_settings(result, catalog)
