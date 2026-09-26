#!/usr/bin/env python3
"""Loopback-only browser interface; no external server or frontend dependencies."""
from __future__ import annotations

import argparse
import re
import copy
import asyncio
import dataclasses
import gzip
import hashlib
import json
import math
import os
import threading
import time
from contextlib import closing, contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
from urllib.parse import parse_qs, quote, urlsplit
import webbrowser

import httpx

from .branding import (PRODUCT_ID, PRODUCT_NAME, PRODUCT_VERSION, REQUEST_PROFILES,
                        SOURCE_CATALOG_URL, SOURCES_URL)
from . import proxytool as core
from .maintenance import clear_runtime, exclusive_lock
from .reputation import Denylist, normalize_zones
from . import anonymity
from . import api
from . import apikeys
from . import core as admission
from . import gateway
from .i18n import tr, utf8_output, state_detail_text
from . import paths
from . import geoip
from . import importer
from . import source_catalog
from . import source_management
from . import db as schema
from . import servicecatalog

def _source_id_flag(payload, flag):
    """A source id and an explicit boolean beside it.

    The flag has to be stated and has to be a boolean.  A pause request that
    omits it used to be read as "pause", and one that sent the string ``"yes"``
    was read as "pause" too: a typo silently switched a source off and the
    answer said it had been asked to.  Both are now refused, and refusal is the
    only honest answer to a request that does not say which way it means.
    """
    payload = payload or {}
    source_id = payload.get('id')
    if not isinstance(source_id, str) or not source_id:
        raise ValueError('Укажите источник.')
    value = payload.get(flag, _MISSING)
    if value is _MISSING:
        raise ValueError(f'Укажите, что делать с источником: {flag}=true или {flag}=false.')
    if not isinstance(value, bool):
        raise ValueError(f'{flag}: ожидается true или false.')
    return source_id, value


class _Missing:
    __slots__ = ()

    def __repr__(self):
        return '<missing>'


_MISSING = _Missing()


def _source_id(payload):
    payload = payload or {}
    source_id = payload.get('id')
    if not isinstance(source_id, str) or not source_id:
        raise ValueError('Укажите источник.')
    return source_id


#: The five support statuses F13 asks of a catalog record.  `support_status`
#: computes them; the names are repeated here only so the filter can reject a
#: typo instead of quietly returning an empty list.
SUPPORT_STATUSES = ('supported', 'needs-auth', 'needs-adapter', 'unsupported', 'experimental')


def _known_source(catalog, settings, source_id):
    if source_catalog.source_by_id(catalog, source_id) is not None:
        return True
    selection = settings.get('source_selection') or {}
    return any(item.get('id') == source_id for item in selection.get('custom_sources', []) if isinstance(item, dict))


def dataclass_as_dict(value):
    """Any frozen dataclass of another module, as the JSON a page can read."""
    if value is None:
        return None
    for name in ('as_dict', 'to_dict'):
        method = getattr(value, name, None)
        if callable(method):
            with suppress(TypeError):
                return method()
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    return {key: item for key, item in vars(value).items() if not key.startswith('_')}


def jobs_state_names():
    """The job and item state vocabularies, so a button knows what is legal."""
    from . import jobs as jobs_module
    return dict(job=list(jobs_module.JOB_STATES), item=list(jobs_module.ITEM_STATES),
                terminal=sorted(jobs_module.TERMINAL_JOB_STATES),
                active=sorted(jobs_module.ACTIVE_JOB_STATES))

ROOT = paths.PACKAGE
MAX_BODY = 32 * 1024 * 1024
MAX_SELECTION = 1000
# A bulk action over "all matching" runs on the server: the browser sends the
# filter, not the rows, so a 100k-row scope never has to reach the page.
MAX_BULK_SELECTION = 200_000
MAX_BULK_COPY = 50_000
# The result table loads one page at a time.  50 keeps the existing density of
# the user's table; the ceiling exists so a request cannot ask for everything.
PAGE_SIZE = 50
PAGE_SIZE_MAX = 200
# A quick row test is diagnostic.  It gets its own whole-call deadline so one
# click cannot run for minutes (defect 23).
QUICK_TEST_DEADLINE_S = 45.0
QUICK_TEST_TIMEOUT_CAP_S = 10.0
QUICK_TEST_MAX_BYTES = 1024 * 1024
# Sidecar files of the interface: user-authored, never touched by the worker.
ANNOTATIONS_FILE = 'gui-annotations.json'
VIEWS_FILE = 'gui-views.json'
HISTORY_FILE = 'gui-history.json'
HISTORY_LIMIT = 50
EVENTS_FILE = 'gui-events.jsonl'
EVENT_RETENTION = 2000
# The user's own service sets (F06).  A pinned set is a snapshot of definitions
# the catalog published once: later catalog updates must not change it silently,
# so the pin lives beside the user's other documents, not in the worker runtime.
SERVICE_SETS_FILE = 'gui-service-sets.json'
SERVICE_SET_LIMIT = 64
SERVICE_SET_PAGE = 200
# One paste into a personal list is bounded: the interface never reads a file
# the size of a public source dump on behalf of a membership edit.
MAX_COLLECTION_MEMBERS = 20_000
# The scope exclusions live in `db.candidate_scope_exclusion` (migration 18).
# The file below is only where a build from before that migration kept them, and
# `App.import_scope_sidecar` moves its contents across once; nothing reads it
# after that.
SCOPE_EXCLUSIONS_FILE = 'gui-scope-exclusions.json'
SCOPE_EXCLUSION_LIMIT = 20_000
#: The country criterion the table applies (F08).  These are the values the
#: CLI already accepts in ``--country-basis`` / ``--country-unknown``; the page
#: takes them in the query so a link to a scoped table can be shared.
COUNTRY_BASES = ('endpoint', 'exit', 'either')
COUNTRY_UNKNOWNS = ('exclude', 'include_unverified', 'require_measurement')
#: How many publishers one comparison may name.  The overlap matrix is
#: quadratic, so a page that let a whole catalog through would answer a
#: question nobody asked with a request that never finishes.
SOURCE_COMPARE_LIMIT = 24
# The gateway binding the user chose on the gateway page.  A user document for
# the same reason: it names a pool and a profile, it does not run anything.
GATEWAY_CONFIG_FILE = 'gui-gateway.json'
# When a schedule became active.  `schedules` (migration 7) has no
# `activated_at` column and `SqliteScheduleStore` cannot persist one, so the
# interface keeps the instant it created the schedule here; the next run is
# still computed by `scheduler.Scheduler.plan`, not by this file.
SCHEDULES_FILE = 'gui-schedules.json'
# One preview is kept in memory so the commit button acts on exactly the plan
# that was shown.  A few plans, not all of them: the list is a menu of pending
# imports, not a cache of every file the user ever opened.
IMPORT_PLANS = 6
# Bulk actions that change stored state can be undone; a scan cannot.
RECOVERABLE_OPS = frozenset({'tag', 'untag', 'note', 'favorite', 'unfavorite', 'exclude', 'include', 'denylist'})
BULK_OPS = frozenset({'recheck', 'export', 'copy', 'tag', 'untag', 'note', 'favorite',
                      'unfavorite', 'exclude', 'include', 'denylist'})
BULK_SCOPES = frozenset({'page', 'selected', 'all_matching'})
RESULT_VIEWS = ('fresh', 'stale', 'failed', 'unknown', 'all')
# Freshness buckets.  ``rejected`` exists only inside the "all" view: a row
# that is neither fresh, expired, failed nor time-unknown was simply filtered
# out by the current policy, and saying "failed" about it would be a lie.
FRESHNESS_FRESH = 'fresh'
FRESHNESS_STALE = 'stale'
FRESHNESS_FAILED = 'failed'
FRESHNESS_UNKNOWN = 'unknown'
FRESHNESS_REJECTED = 'rejected'
# Compact is the default column set of the user's table (defect: F19).
COMPACT_COLUMNS = ('check', 'num', 'proxy', 'score', 'latency', 'success', 'cleanliness', 'country', 'actions')
ALL_COLUMNS = ('check', 'num', 'proxy', 'score', 'latency', 'jitter', 'speed', 'success', 'uptime',
               'cleanliness', 'anonymity', 'country', 'provider', 'age', 'actions')
DOWNLOAD_CHUNK = 65536


def read_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return fallback


#: F21 lives on the sources page, and the page is the user's file.  Rather than
#: editing ``ui/index.html`` -- which belongs to the design, not to a feature --
#: the section is added while the page is served, through the same substitution
#: the token already goes through.  The markup below uses the classes the page
#: already uses, so it looks like the rest of the screen rather than like a
#: widget dropped on it, and the anchor is the one closing tag of the sources
#: page: a page that is rearranged does not get the section rather than getting
#: a broken one.
SOURCE_COMPARE_ANCHOR = '</tbody>\n          </table>\n        </div>\n      </section>\n    </section>'
SOURCE_COMPARE_SECTION = SOURCE_COMPARE_ANCHOR.replace(
    '    </section>',
    '''      <section class="card" id="source-compare-card">
        <div class="card-title">
          <div class="geo-title-wrap">
            <span class="code-editor-badge">COMPARE</span>
            <h2>Сравнение источников</h2>
          </div>
          <div class="catalog-head-meta">
            <span id="source-compare-status" class="badge subtle">Нет измерений</span>
          </div>
        </div>
        <p class="hint">Сравнение идёт по тому, что уже измерено: доля прошедших, пересечение
          наборов, уникальный вклад и цена одного пригодного адреса. Сеть при этом не трогается.
          Два источника с одинаковым набором адресов — это один издатель, посчитанный дважды.</p>

        <div class="field-grid">
          <label>
            <span>Идентификаторы источников</span>
            <input id="source-compare-input" spellcheck="false" placeholder="cur-41, cur-43, new-043">
          </label>
          <label>
            <span>Порог приёмки</span>
            <input id="source-compare-min" spellcheck="false" placeholder="0.667">
          </label>
          <label>
            <span>Окон для выживания</span>
            <input id="source-compare-windows" type="number" min="0" max="64" value="0">
          </label>
          <label></label>
          <button type="button" class="button primary chip" id="source-compare-run">Сравнить</button>
        </div>

        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Источник</th>
                <th>Состояние</th>
                <th>Отдал</th>
                <th>Измерено</th>
                <th>Прошло</th>
                <th>Неизвестно</th>
                <th>Пригодно</th>
                <th>Доля</th>
                <th>Доверительный интервал</th>
                <th>Своих адресов</th>
                <th>Семейство</th>
              </tr>
            </thead>
            <tbody id="source-compare-rows">
              <tr><td colspan="11" class="empty">Укажите источники и нажмите «Сравнить».</td></tr>
            </tbody>
          </table>
        </div>
        <div id="source-compare-detail"></div>
      </section>
    </section>''')


def inject_source_compare(html):
    """Add the F21 section to the sources page, once, in the page's own style."""
    if SOURCE_COMPARE_ANCHOR not in html or 'id="source-compare-card"' in html:
        return html
    return html.replace(SOURCE_COMPARE_ANCHOR, SOURCE_COMPARE_SECTION, 1)


#: The script that drives the section.  It talks to ``/api/sources/compare``
#: only -- the same ``sourcedesk`` comparison the CLI and ``/v1`` serve -- and
#: escapes every value it prints, because a source name is catalog data.  The
#: marker is what makes the injection idempotent: an element id would not be,
#: because the markup carries the same ids the script looks up.
SOURCE_COMPARE_MARKER = '/* PW_SOURCE_COMPARE_DRIVER */'
SOURCE_COMPARE_SCRIPT = """
<script>/* PW_SOURCE_COMPARE_DRIVER */
(function () {
  const card = document.getElementById('source-compare-card');
  if (!card) return;
  const rows = document.getElementById('source-compare-rows');
  const badge = document.getElementById('source-compare-status');
  const detail = document.getElementById('source-compare-detail');
  const esc = (v) => String(v === null || v === undefined ? '' : v)
    .replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;',
                                  '"': '&quot;', "'": '&#39;'}[c]));
  const pct = (v) => (v === null || v === undefined ? '—' : (v * 100).toFixed(1) + '%');
  const n = (v) => (v === null || v === undefined ? '—' : String(v));

  function head(text) {
    return '<div class="card-title"><h2>' + esc(text) + '</h2></div>';
  }

  function show(message, badgeText) {
    rows.innerHTML = '<tr><td colspan="11" class="empty">' + esc(message) + '</td></tr>';
    badge.textContent = badgeText;
    detail.innerHTML = '';
  }

  function table(headers, body) {
    if (!body.length) return '';
    return '<div class="table-wrap"><table><thead><tr>' +
      headers.map((h) => '<th>' + esc(h) + '</th>').join('') +
      '</tr></thead><tbody>' + body.join('') + '</tbody></table></div>';
  }

  function line(text) {
    return '<li>' + esc(text) + '</li>';
  }

  function render(data) {
    const measured = data.rows.filter((r) => r.status === 'measured').length;
    badge.textContent = measured ? ('измерено источников: ' + measured + ' из ' + data.rows.length)
                                 : ('измерений в окне нет');
    rows.innerHTML = data.rows.map((r) => '<tr>' +
      '<td>' + esc(r.source_id) + '</td>' +
      '<td>' + esc(r.status) + '</td>' +
      '<td>' + n(r.offered) + '</td>' +
      '<td>' + n(r.measured) + '</td>' +
      '<td>' + n(r.passed) + '</td>' +
      '<td>' + n(r.unknown) + '</td>' +
      '<td>' + n(r.admitted) + '</td>' +
      '<td>' + pct(r.reliability) + '</td>' +
      '<td>' + pct(r.reliability_low) + ' … ' + pct(r.reliability_high) +
        (r.sample_sufficient ? '' : ' · выборка мала') + '</td>' +
      '<td>' + n(r.unique_offered) + ' (' + n(r.unique_admitted) + ' пригодных)</td>' +
      '<td>' + esc(r.family_id || '—') + '</td>' +
      '</tr>').join('') ||
      '<tr><td colspan="11" class="empty">Сравнивать нечего.</td></tr>';

    let html = '';
    const notes = (data.warnings || []).map(line);
    (data.rows || []).forEach((r) => (r.notes || []).forEach((t) => notes.push(line(r.source_id + ': ' + t))));
    if (notes.length) html += head('Что нужно знать до чтения числа') + '<ul class="hint">' + notes.join('') + '</ul>';

    const pairs = (data.overlaps || []).filter((p) => p.shared > 0);
    html += head('Пересечение наборов');
    html += pairs.length ? table(['A', 'B', 'Общих', 'Jaccard', 'Только в A', 'Только в B', 'Вердикт'],
      pairs.map((p) => '<tr><td>' + esc(p.left) + '</td><td>' + esc(p.right) + '</td><td>' +
        n(p.shared) + '</td><td>' + pct(p.jaccard) + '</td><td>' + n(p.left_only) + '</td><td>' +
        n(p.right_only) + '</td><td>' +
        (p.identical ? 'одинаковые наборы — уникальный вклад второго равен нулю' : 'частичное совпадение') +
        '</td></tr>'))
      : '<p class="hint">Общих адресов в этом сравнении нет.</p>';

    const groups = (data.families || []).filter((f) => (f.members || []).length > 1);
    html += head('Семейства и уникальный вклад');
    html += groups.length ? table(['Семейство', 'Участники', 'Адресов', 'Своих', 'Наборы идентичны'],
      groups.map((f) => '<tr><td>' + esc(f.family_id) + '</td><td>' + esc((f.members || []).join(', ')) +
        '</td><td>' + n(f.endpoints) + '</td><td>' + n(f.unique_endpoints) + '</td><td>' +
        (f.identical_group ? 'да' : 'нет') + '</td></tr>'))
      : '<p class="hint">Ни один источник не делит набор адресов с другим.</p>';

    html += head('Цена одного пригодного адреса');
    html += table(['Источник', 'Пригодных', 'Секунд', 'Байт', 'Попыток', 'Почему нет цены'],
      (data.cost || []).map((c, i) => '<tr><td>' + esc((data.rows[i] || {}).source_id) + '</td><td>' +
        n(c.admitted) + '</td><td>' + n(c.seconds) + '</td><td>' + n(c.bytes) + '</td><td>' +
        n(c.attempts) + '</td><td>' + esc(c.reason || '') + '</td></tr>'));

    if ((data.survival || []).length) {
      html += head('Выживание по окнам');
      html += table(['Окно', 'Начало', 'Конец', 'Вошло', 'Выжило', 'Умерло', 'Не проверено', 'Доля'],
        data.survival.map((s) => '<tr><td>' + (s.index + 1) + '</td><td>' + n(s.start) + '</td><td>' +
          n(s.end) + '</td><td>' + n(s.entered) + '</td><td>' + n(s.alive) + '</td><td>' +
          n(s.dead) + '</td><td>' + n(s.censored) + '</td><td>' + pct(s.rate) + '</td></tr>'));
    }

    if (data.biases && data.biases.length) {
      html += head('Смещения, из-за которых число не вся правда');
      html += table(['Код', 'Что это значит', 'Источники'],
        data.biases.map((b) => '<tr><td>' + esc(b.code) + '</td><td>' + esc(b.detail) + '</td><td>' +
          esc((b.sources || []).join(', ')) + '</td></tr>'));
    }

    if (data.suppliers) {
      html += head('Два поставщика на одинаковых условиях');
      html += '<p class="hint">Одинаковые условия: ' +
        (data.suppliers.equal_terms ? 'да' : 'нет') + '</p>';
      (data.suppliers.warnings || []).forEach((w) => { html += '<p class="hint">' + esc(w) + '</p>'; });
    }
    detail.innerHTML = html;
  }

  document.getElementById('source-compare-run').addEventListener('click', () => {
    const sources = document.getElementById('source-compare-input').value.trim();
    if (!sources) { show('Укажите хотя бы один источник.', 'Нет измерений'); return; }
    const query = new URLSearchParams({sources});
    const min = document.getElementById('source-compare-min').value.trim();
    if (min) query.set('min_success', min);
    const windows = document.getElementById('source-compare-windows').value;
    if (windows && Number(windows) > 0) query.set('survival_windows', windows);
    show('Считаю…', 'Считаю…');
    const meta = document.querySelector('meta[name="workbench-token"]');
    const token = meta ? meta.content : '';
    fetch('/api/sources/compare?' + query.toString(), {headers: {'X-Workbench-Token': token}})
      .then((r) => r.json().then((body) => ({ok: r.ok, body})))
      .then((answer) => {
        if (!answer.ok) {
          const err = (answer.body && answer.body.error) || 'Сравнение не выполнено.';
          show(typeof err === 'string' ? err : (err || 'Сравнение не выполнено.'), 'Отказ');
          return;
        }
        render(answer.body);
      })
      .catch((err) => show('Сравнение не выполнено: ' + err, 'Отказ'));
  });
})();
</script>
"""


def append_source_compare_script(html):
    """Put the F21 driver at the end of the page, after ``app.js`` has run."""
    if SOURCE_COMPARE_MARKER in html or 'id="source-compare-card"' not in html:
        return html
    if '</body>' in html:
        return html.replace('</body>', SOURCE_COMPARE_SCRIPT + '</body>', 1)
    return html + SOURCE_COMPARE_SCRIPT


def public_source(value, *, keyed=True):
    if not isinstance(value, str):
        return ''
    parts = value.strip().split(None, 1)
    if len(parts) == 2 and parts[0] in core.SOURCE_KINDS:
        label = parts[0] + ' ' + core.public_url(parts[1])
    else:
        label = core.public_url(value)
    # Paths and query strings are intentionally hidden, but a host-only label
    # is ambiguous for several GitHub/raw mirrors.  A short stable digest lets
    # users identify the exact configured entry without exposing credentials.
    return label + ' [' + core.source_key(value)[:8] + ']' if keyed else label


RESULT_ORDERS = {
    # Re-sorted in Python by the recommended score; SQL only gives a stable start.
    'recommended': "json_extract(payload,'$.score') DESC, proxy",
    'quality': "json_extract(payload,'$.score') DESC, json_extract(payload,'$.latency_ms'), proxy",
    'speed': "json_extract(payload,'$.latency_ms'), json_extract(payload,'$.reliability') DESC, proxy",
    'stability': "json_extract(payload,'$.jitter_ms'), json_extract(payload,'$.latency_ms'), proxy",
    'uptime': "COALESCE(1.0*json_extract(payload,'$.history.passes')/json_extract(payload,'$.history.checks'), 1) DESC, "
              "COALESCE(json_extract(payload,'$.history.checks'), 1) DESC, json_extract(payload,'$.score') DESC, proxy",
    'bandwidth': "json_extract(payload,'$.speed.mbps') IS NULL, json_extract(payload,'$.speed.mbps') DESC, "
                 "json_extract(payload,'$.score') DESC, proxy",
}
# A source needs this many checked proxies before "no working ones" is trusted.
PRUNE_MIN_CHECKED = 20
# The UI translates the scanner's log itself, so the scanner always writes Russian here.
CHILD_ENV = dict(os.environ, PROXY_WORKBENCH_LANG='ru', PYTHONUNBUFFERED='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
DOWNLOADS = ('proxies.txt', 'ranked.csv', 'ranked.json', *core.PROTOCOL_EXPORTS.values(), 'hostport.txt', 'proxychains.txt',
             'proxy.pac', 'clash.yaml', 'singbox.json')
# An empty/stale generation may still be inspected as plain text, but a
# ready-made client configuration must not be presented as usable.
EMPTY_SAFE_DOWNLOADS = {'proxies.txt', 'ranked.csv', 'ranked.json', *core.PROTOCOL_EXPORTS.values(),
                        'hostport.txt', 'proxychains.txt'}


def public_sources(values, *, keyed=True):
    return [public_source(value, keyed=keyed) for value in values] if isinstance(values, list) else []


class Sidecar:
    """One small JSON document the interface owns: tags, views, undo history.

    These are user documents, not worker state, so they never join the runtime
    files of the scanner and are never rewritten by a check.
    """

    def __init__(self, path, fallback=None):
        self.path = Path(path)
        self.fallback = {} if fallback is None else fallback
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            return read_json(self.path, copy.deepcopy(self.fallback))

    def write(self, payload):
        with self.lock:
            core.atomic(self.path, json.dumps(payload, ensure_ascii=False))
        return payload


def digest_of(*parts):
    """Stable identity of a result scope, used to refuse a stale selection."""
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]


def freshness_of(verdict, row):
    """One of the four user-facing freshness buckets for one row."""
    reason = verdict.reason_code
    if verdict.time_state in (admission.TIME_UNKNOWN, admission.CLOCK_ROLLBACK) or (
            verdict.observation_state == admission.OBSERVATION_MISSING):
        return FRESHNESS_UNKNOWN
    if verdict.observation_state == admission.OBSERVATION_FAILED:
        return FRESHNESS_FAILED
    if reason in ('E_TIME_TTL_EXPIRED', 'E_TIME_TTL_MISSING', 'E_TIME_FUTURE') or (
            verdict.time_state == admission.TIME_EXPIRED):
        return FRESHNESS_STALE
    if verdict.admitted:
        return FRESHNESS_FRESH
    if row.get('min_target_reliability') is None and row.get('error'):
        return FRESHNESS_FAILED
    return FRESHNESS_REJECTED


def normalize_hosting_filter(value):
    """Map the two UI labels to the backend's canonical keep/hide values."""
    if value in ('', 'any', 'keep'):
        return ''
    if value in ('hide', 'exclude'):
        return 'hide'
    raise ValueError('Неверный фильтр провайдера для экспорта.')


SNAPSHOT_OK = 'ok'
SNAPSHOT_LEGACY = 'legacy'
SNAPSHOT_BROKEN = 'broken'
SNAPSHOT_MISSING = 'missing'


class Snapshot:
    """One pinned published generation, read without any writer lock.

    Defect 5: the table, the details and the download must answer while a scan
    or a watch hold ``workbench.lock``.  A generation directory is immutable and
    is published by an atomic rename, so a read-only path can simply resolve the
    pointer once and open the files.  Defect 3: the snapshot keeps per-row
    freshness, so one expired member never empties the set.
    """

    def __init__(self, state, *, generation=None, rows=(), status=None, directory=None, detail=None):
        self.state = state
        self.generation = generation
        self.rows = list(rows)
        self.status = dict(status or {})
        self.directory = Path(directory) if directory else None
        self.detail = detail

    @property
    def usable(self):
        """A snapshot the interface may describe as "the current export"."""
        return self.state in (SNAPSHOT_OK, SNAPSHOT_LEGACY) and bool(self.status)


def read_snapshot(data, *, now=None):
    """Read the current generation once; never take the exclusive data lock."""
    exports = Path(data)/'exports'
    generation = core.current_generation_name(exports)
    if generation:
        rows_path = core.export_file(exports, 'ranked.json', generation=generation)
        status_path = core.export_file(exports, 'status.json', generation=generation)
        try:
            rows = json.loads(rows_path.read_text(encoding='utf-8'))
            status = json.loads(status_path.read_text(encoding='utf-8')) if status_path.is_file() else {}
        except (OSError, UnicodeError, ValueError):
            # A published generation never changes; a generation that cannot be
            # read is a broken publication, not a licence to serve database
            # rows the reader itself would reject (defect 9, R05).
            return Snapshot(SNAPSHOT_BROKEN, generation=generation, directory=exports,
                            detail='generation_unreadable')
        if not isinstance(rows, list) or not isinstance(status, dict):
            return Snapshot(SNAPSHOT_BROKEN, generation=generation, directory=exports,
                            detail='generation_malformed')
        if status.get('generation') not in (None, generation):
            return Snapshot(SNAPSHOT_BROKEN, generation=generation, directory=exports,
                            detail='generation_mismatch')
        return Snapshot(SNAPSHOT_OK, generation=generation, rows=rows, status=status, directory=exports)
    if not (exports/'generations').is_dir() and (exports/'ranked.json').is_file():
        # Pre-generation installation: the mutable root files are the snapshot.
        try:
            rows = json.loads((exports/'ranked.json').read_text(encoding='utf-8'))
            status = read_json(exports/'status.json', {})
        except (OSError, UnicodeError, ValueError):
            return Snapshot(SNAPSHOT_BROKEN, directory=exports, detail='legacy_unreadable')
        if not isinstance(rows, list):
            return Snapshot(SNAPSHOT_BROKEN, directory=exports, detail='legacy_malformed')
        return Snapshot(SNAPSHOT_LEGACY, rows=rows, status=status, directory=exports)
    if (exports/'generations').is_dir():
        # Generations exist but the pointer does not resolve: the publication is
        # broken, and falling back to arbitrary database rows is forbidden.
        return Snapshot(SNAPSHOT_BROKEN, directory=exports, detail='pointer_unreadable')
    return Snapshot(SNAPSHOT_MISSING, directory=exports)


def defaults():
    # A fresh install already answers in the version-3 shape: the flat list of
    # 55 pre-catalog URLs, each mapped onto its catalog record so the selection
    # exists from the first start and nothing has to be guessed later.  The
    # alias map walks the whole catalog, so it is built once per call rather
    # than once per spec.
    sources = json.loads((ROOT/'sources.json').read_text(encoding='utf-8'))
    catalog = source_catalog.load_bundled()
    aliases = source_catalog.legacy_aliases(catalog)
    return dict(settings_version=3, targets=[dict(name='example.com', url='https://example.com/', statuses=[200],
                             contains='Example Domain', headers={}, method='GET')],
                sources=sources,
                source_selection=dict(schema_version=1, catalog_revision=catalog['revision'],
                                      selected_ids=[aliases[spec] for spec in sources if spec in aliases],
                                      download_disabled_ids=[], sets=[], custom_sources=[]),
                use_sources=True,
                proxies='', attempts=3, timeout=8, workers=128, rate=100,
                max_bytes=1048576, source_timeout=60, min_success=2/3, top=0, sort='recommended',
                request_profile='workbench', denylist='',
                reputation=dict(local_enabled=True, dnsbl_enabled=False, dnsbl_zones=[],
                                timeout=2.5, strict=False),
                anonymity=dict(judge_url=''), min_anonymity='any',
                connect_timeout=4, fail_fast=True, protocol='all', max_latency=0, countries='', want=0,
                detect_protocols=False, watch=0, prefilter=512, exclude_hosting=False, speedtest=dict(url='', max_bytes=core.SPEEDTEST_BYTES),
                collection='')


def validate(settings):
    if not isinstance(settings, dict):
        raise ValueError('Ожидаются настройки проверки.')
    # A document written before the source catalog carries a flat list of URLs
    # and no selection.  It is migrated here, on the one path every reader and
    # every writer already goes through, so an old install is upgraded the
    # first time anything reads its settings and never loses a URL or a pause
    # doing it.  ``migrate_settings`` is idempotent, so a document that is
    # already v3 is returned untouched.
    if settings.get('settings_version') in (1, 2) or 'source_selection' not in settings:
        settings = source_catalog.migrate_settings(settings)
    clean = defaults()
    clean.update({k: settings[k] for k in clean if k in settings})
    if clean['settings_version'] not in (1, 2, 3):
        raise ValueError('Неизвестная версия настроек.')
    clean['settings_version'] = 3
    # Version 3 carries the source-catalog selection made in the Sources tab;
    # it is produced by source_catalog.migrate_settings and must survive a
    # round-trip through validate, or every catalog write would silently lose
    # the user's selection.
    selection = settings.get('source_selection')
    if not isinstance(selection, dict) or not isinstance(selection.get('selected_ids'), list) \
            or not isinstance(selection.get('download_disabled_ids', []), list):
        raise ValueError('source_selection: повреждённые поля.')
    selection = dict(selection, schema_version=1, custom_sources=list(selection.get('custom_sources') or []))
    selection['selected_ids'] = list(dict.fromkeys(selection['selected_ids']))
    selection['download_disabled_ids'] = [value for value in dict.fromkeys(selection.get('download_disabled_ids') or [])
                                           if value in set(selection['selected_ids'])]
    clean['source_selection'] = selection
    if not isinstance(clean['sources'], list):
        raise ValueError('Источники должны быть списком URL (до 5000).')
    if not isinstance(clean['request_profile'], str) or clean['request_profile'] not in REQUEST_PROFILES:
        raise ValueError('Неизвестный request-профиль.')
    if not isinstance(clean['denylist'], str) or len(clean['denylist']) > 2_000_000:
        raise ValueError('Список denylist слишком большой: максимум 2 МБ.')
    for line in clean['denylist'].splitlines():
        value = line.strip()
        if value and not value.startswith('#') and ('@' in value or
                ('://' in value and core.normalize_custom(value) is None)):
            raise ValueError('Denylist не должен содержать логин или пароль.')
    incoming_rep = settings.get('reputation', {})
    if not isinstance(incoming_rep, dict):
        raise ValueError('Настройки чистоты должны быть объектом.')
    reputation = defaults()['reputation']
    reputation.update({k: incoming_rep[k] for k in reputation if k in incoming_rep})
    for key in ('local_enabled', 'dnsbl_enabled', 'strict'):
        if type(reputation[key]) is not bool:
            raise ValueError('Настройки чистоты должны быть логическими.')
    if not isinstance(reputation['dnsbl_zones'], list):
        raise ValueError('DNSBL-зоны должны быть списком.')
    reputation['dnsbl_zones'] = normalize_zones(reputation['dnsbl_zones'])
    if reputation['dnsbl_enabled'] and not reputation['dnsbl_zones']:
        reputation['dnsbl_enabled'] = False
    try:
        reputation['timeout'] = float(reputation['timeout'])
    except (TypeError, ValueError):
        raise ValueError('Таймаут DNSBL должен быть числом.') from None
    if not math.isfinite(reputation['timeout']) or not .1 <= reputation['timeout'] <= 30:
        raise ValueError('Таймаут DNSBL должен быть от 0.1 до 30 секунд.')
    clean['reputation'] = reputation
    for key, low, high, integer in [('attempts', 1, 100, True), ('timeout', .1, 300, False),
            ('workers', 1, 2048, True), ('rate', 0, 10000, False), ('max_bytes', 1, 100_000_000, True),
            ('source_timeout', 1, 3600, False), ('top', 0, 1_000_000_000, True), ('min_success', 0, 1, False),
            ('connect_timeout', .1, 300, False), ('max_latency', 0, 600_000, False), ('want', 0, 1_000_000_000, True),
            ('watch', 0, 1440, False), ('prefilter', 0, 5000, True)]:
        value = clean[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high or (integer and int(value) != value):
            raise ValueError(f'Недопустимое значение: {key}.')
        clean[key] = int(value) if integer else value
    if (clean['sort'] not in core.SORTS or clean['protocol'] not in core.PROTOCOLS
            or type(clean['use_sources']) is not bool or type(clean['fail_fast']) is not bool
            or type(clean['detect_protocols']) is not bool or type(clean['exclude_hosting']) is not bool):
        raise ValueError('Неверный режим сортировки или источников.')
    if not isinstance(clean['proxies'], str) or len(clean['proxies']) > 20_000_000:
        raise ValueError('Список прокси слишком большой: максимум 20 МБ.')
    # Normalize the user import before it can be persisted or exported.  This
    # rejects URL credentials while retaining private/hostname entries for
    # explicitly local user setups; the public collector still applies its
    # separate global-IP policy during collection.
    clean['proxies'] = core.normalize_custom_list(clean['proxies'])
    if not isinstance(clean['sources'], list) or len(clean['sources']) > 5000:
        raise ValueError('Источники должны быть списком URL (до 5000).')
    for url in clean['sources']:
        core.source_spec(url)
    clean['sources'] = list(dict.fromkeys(clean['sources']))
    targets = clean['targets']
    if not isinstance(targets, list) or not 1 <= len(targets) <= 20:
        raise ValueError('Добавьте от 1 до 20 сервисов.')
    # Use the exact same normalization/validation as the scanner without writing a file.
    args = argparse.Namespace(config=None, url=None, attempts=clean['attempts'],
                              timeout=clean['timeout'], max_bytes=clean['max_bytes'],
                              request_profile=clean['request_profile'])
    normalized = core.validate_targets(copy.deepcopy(targets), args, request_profile=clean['request_profile'])
    for t in normalized['targets']:
        name = t.get('name', '')
        if not isinstance(name, str) or len(name) > 160:
            raise ValueError('Название сервиса: максимум 160 символов.')
    clean['targets'] = normalized['targets']
    judge = clean['anonymity']
    if not isinstance(judge, dict):
        raise ValueError('Настройки анонимности должны быть объектом.')
    judge_url = judge.get('judge_url') or ''
    if not isinstance(judge_url, str):
        raise ValueError('anonymity.judge_url: ожидается http(s) URL')
    judge_url = judge_url.strip()
    anonymity.validate_judge({'judge_url': judge_url})
    clean['anonymity'] = dict(judge_url=judge_url)
    anonymity.validate_min_level(clean['min_anonymity'])
    speedtest = clean['speedtest']
    if not isinstance(speedtest, dict) or not isinstance(speedtest.get('url', ''), str):
        raise ValueError('speedtest.url: ожидается http(s) URL')
    speedtest = dict(url=speedtest.get('url', '').strip(), max_bytes=speedtest.get('max_bytes', core.SPEEDTEST_BYTES))
    core.validate_speedtest(speedtest if speedtest['url'] else None)
    if type(speedtest['max_bytes']) is not int:
        raise ValueError('speedtest.max_bytes: от 10000 до 200000000')
    clean['speedtest'] = speedtest
    if not isinstance(clean['countries'], str) or len(clean['countries']) > 1000:
        raise ValueError('Страны: используйте двухбуквенные ISO-коды, например DE,NL.')
    clean['countries'] = ','.join(geoip.parse_countries(clean['countries']))
    # F02: the scope of a check is one collection id, or empty for the public
    # base.  Existence is decided by the local database when it is used; here
    # only the shape is checked, so a stored setting survives a data folder
    # that has not been opened yet.
    if not isinstance(clean['collection'], str) or len(clean['collection']) > 128 \
            or (clean['collection'] and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}',
                                                         clean['collection'])):
        raise ValueError('Коллекция: ожидается идентификатор коллекции.')
    return clean




def save_settings(data, payload):
    """The one writer of ``gui-settings.json``: validate, then atomic replace.

    The GUI and the CLI both go through it (directly or through
    ``source_management.write_settings``), so a selection changed from the
    terminal is validated exactly like one changed in the browser and a crash
    cannot leave a half-written selection behind.
    """
    settings = validate(payload)
    core.atomic(Path(data)/'gui-settings.json',
                json.dumps(settings, ensure_ascii=False, indent=2) + '\n')
    return settings


def read_settings(data):
    """Current settings without a running server; ``None`` when unreadable.

    ``source_management`` and the CLI use this to answer "what has the user
    chosen" without booting the interface, so the migration in :func:`validate`
    runs on the same path a running server would take.
    """
    app = App.__new__(App)
    app.data = Path(data).resolve()
    try:
        return app.settings()
    except (OSError, ValueError):
        return None

class App:
    def __init__(self, data):
        self.data = data.resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.instance_lock = (self.data/'gui-instance.lock').open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.instance_lock.write(b'0'); self.instance_lock.flush(); self.instance_lock.seek(0)
                msvcrt.locking(self.instance_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.instance_lock.close()
            raise OSError('GUI already running') from None
        self.token = secrets.token_urlsafe(32)
        # The gateway password is a separate secret: the GUI session token must
        # never travel into a QR code or a phone profile (defect 18, R12).
        self.gateway_token = secrets.token_urlsafe(24)
        self.geo_cache = (None, None)
        self.asn_cache = (None, None)
        self.mutex = threading.RLock()
        self.process = None
        self.log_handle = None
        self.job = read_json(self.data/'gui-job.json', {})
        self.export_reader = api.Exports(self.data/'exports')
        self.stop_path = self.data/'gui-stop'
        self.progress_path = self.data/'gui-progress.json'
        self.annotations = Sidecar(self.data/ANNOTATIONS_FILE, {'entries': {}})
        self.views = Sidecar(self.data/VIEWS_FILE, {'views': []})
        self.history = Sidecar(self.data/HISTORY_FILE, {'entries': []})
        self.service_sets = Sidecar(self.data/SERVICE_SETS_FILE, {'sets': {}, 'pinned': None})
        self.scope_exclusions_doc = Sidecar(self.data/SCOPE_EXCLUSIONS_FILE, {'exclusions': []})
        # One-time move of the exclusions a previous build kept in the sidecar
        # into `db.candidate_scope_exclusion`.  The sidecar is not deleted --
        # it is the user's document and the import is idempotent -- but nothing
        # reads it again once the flag is set.
        self._scope_sidecar_imported = False
        self.gateway_config = Sidecar(self.data/GATEWAY_CONFIG_FILE, {})
        self.schedule_activations = Sidecar(self.data/SCHEDULES_FILE, {'activated': {}})
        self.events_path = self.data/EVENTS_FILE
        self.event_seq = {}
        self.event_floor = 0
        self.event_seen = set()
        self.event_mark = 0.0
        self.events_loaded = False
        self.event_backfilled = False
        self._status_cache = (None, None, None)
        self._catalog_cache = None
        self._service_catalog = None
        self.catalog_job = {'running': False, 'stage': 'idle', 'added': 0, 'changed': 0, 'retired': 0}
        # The API key manager opens its own connection over the workbench
        # database; it is built on first use and closed with the app.
        self._key_manager = None
        # Previews waiting for their commit button, newest last.  A preview is a
        # frozen plan, not a document: it cannot be rebuilt from JSON, so the
        # page's "apply" acts on the exact object the page was shown.
        self._import_plans = []

    def settings(self):
        settings_path = self.data/'gui-settings.json'
        try:
            stored = json.loads(settings_path.read_text(encoding='utf-8')) if settings_path.exists() else None
        except FileNotFoundError:
            stored = None
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ValueError('Файл gui-settings.json повреждён или недоступен; исправьте его перед продолжением.') from None
        if stored is None:
            stored = defaults()
            try:
                stored['denylist'] = (self.data/'denylist.txt').read_text(encoding='utf-8')
            except FileNotFoundError:
                pass
            except (OSError, UnicodeError):
                raise ValueError('Не удалось прочитать data/denylist.txt. Исправьте файл перед сохранением.') from None
        elif not isinstance(stored, dict):
            raise ValueError('Файл gui-settings.json должен содержать объект настроек.')
        elif 'denylist' not in stored:
            try:
                stored['denylist'] = (self.data/'denylist.txt').read_text(encoding='utf-8')
            except FileNotFoundError:
                stored['denylist'] = ''
            except (OSError, UnicodeError):
                raise ValueError('Не удалось прочитать data/denylist.txt. Исправьте файл перед сохранением.') from None
        legacy = isinstance(stored, dict) and stored.get('settings_version') in (1, 2)
        result = validate(stored)
        if legacy:
            # The migration to the source-catalog selection is one atomic
            # replace.  A crash before this point leaves the legacy file
            # untouched and the next start repeats the migration, so an old
            # install is never left half-converted.
            core.atomic(settings_path, json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        return result


    def save(self, payload):
        if isinstance(payload, dict) and 'denylist' not in payload:
            payload = dict(payload)
            payload['denylist'] = self.settings().get('denylist', '')
        settings = validate(payload)
        with self.mutex:
            core.atomic(self.data/'gui-settings.json', json.dumps(settings, ensure_ascii=False, indent=2))
            core.atomic(self.data/'denylist.txt', settings['denylist'])
        return settings

    # --- Source catalog: shared between UI, CLI and API ---

    CATALOG_FILE = 'source-catalog.json'
    CATALOG_STATE_FILE = 'source-catalog-state.json'

    def read_source_catalog_cache(self):
        """The validators of the last accepted catalog, for a 304 next time."""
        try:
            state = json.loads((self.data/self.CATALOG_STATE_FILE).read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return state if isinstance(state, dict) else {}

    def source_document(self):
        """Last accepted remote catalog, or the bundled one when there is none.

        Named apart from :meth:`catalog` on purpose: that one is the *service*
        catalog (F06), this one is the *source* catalog (F13).  Both were
        called `catalog`, and the later definition silently won, so every
        source page and every source preview was handed the service manifest
        and `source_catalog.source_by_id` raised `KeyError: 'sources'`.
        """
        if self._catalog_cache is not None:
            return self._catalog_cache
        path = self.data/self.CATALOG_FILE
        if path.is_file():
            try:
                self._catalog_cache = source_catalog.load_catalog(path, allow_research=False, allow_unsafe=True)
                return self._catalog_cache
            except (OSError, ValueError, source_catalog.CatalogError):
                pass
        self._catalog_cache = source_catalog.load_bundled()
        return self._catalog_cache

    def read_db(self):
        """Short read-only connection; never takes the workbench lock."""
        path = self.data/'proxies.sqlite3'
        if not path.is_file():
            return None
        try:
            return sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=2)
        except sqlite3.Error:
            return None

    @contextmanager
    def source_db(self):
        db = self.read_db()
        try:
            yield db
        finally:
            if db is not None:
                db.close()

    def source_view(self, query=None):
        """The catalog as the page asks for it, plus a support-status filter.

        `source_management.parse_query` validates the filters it knows and
        ignores the rest, so `support` is applied here.  It is a first-class
        filter and not a decoration: `supported`, `needs-auth`, `needs-adapter`,
        `unsupported` and `experimental` are exactly the questions F13 asks of
        a record, and a paid provider has to be findable under `needs-auth`
        while being visibly inert.  The whole catalog is asked for and the page
        slice is taken here, because the module's own pagination would have cut
        the rows the filter is supposed to choose between.
        """
        query = query or {}
        wanted = self._support_filter(query)
        # `parse_query` reads scalars; `parse_qs` hands over a list for every
        # key, so `?limit=200` reached it as `['200']` and `int(['200'])`
        # refused the request with "limit: expected an integer" -- a catalog
        # page that always sends a page size could never ask for one.  One
        # flattening, before either branch.
        flat = {key: (values[0] if isinstance(values, (list, tuple)) and values else values)
                for key, values in query.items()}
        with self.source_db() as db:
            runtime = source_management.runtime_snapshot(db)
            if not wanted:
                view = source_management.build_view(self.source_document(), self.settings(),
                                                    runtime, flat, db=db)
                # The counts travel with the page so the support filter can
                # say "10 need an account" instead of being an empty list.
                view['supports'] = self.support_facets(view['sources'])
                return view
            flat.pop('support', None)
            flat['limit'] = source_management.MAX_ROWS
            flat['offset'] = 0
            view = source_management.build_view(self.source_document(), self.settings(),
                                                runtime, flat, db=db)
        # `support_status` spells the statuses with an underscore
        # (`needs_auth`); the page and the contract say `needs-auth`, so the
        # comparison is on the dash form and the row keeps what the module
        # said.  One spelling, not two answers.
        rows = [row for row in view['sources']
                if str(row.get('support') or '').replace('_', '-') in wanted]
        view['supports'] = self.support_facets(view['sources'])
        view['sources_all'] = len(view['sources'])
        view['total'] = len(rows)
        try:
            offset = max(0, int(query.get('offset') or 0))
        except (TypeError, ValueError):
            offset = 0
        try:
            limit = int(query.get('limit') or 200)
        except (TypeError, ValueError):
            limit = 200
        view['offset'] = offset
        view['limit'] = limit
        view['sources'] = rows[offset:offset + limit]
        view['filters']['support'] = ','.join(sorted(wanted))
        return view

    @staticmethod
    def _support_filter(query):
        raw = query.get('support')
        if isinstance(raw, (list, tuple)):
            raw = ','.join(str(item) for item in raw)
        text = str(raw or '').strip()
        if not text:
            return set()
        values = {item.strip().replace('_', '-') for item in text.split(',') if item.strip()}
        unknown = sorted(values - set(SUPPORT_STATUSES))
        if unknown:
            raise ValueError('Неизвестный статус поддержки: ' + ', '.join(unknown))
        return values

    @staticmethod
    def support_facets(rows):
        """How many records carry each support status, for the filter row."""
        counts = {status: 0 for status in SUPPORT_STATUSES}
        for row in rows:
            status = str(row.get('support') or '').replace('_', '-')
            if status in counts:
                counts[status] += 1
        return counts

    def source_row(self, source_id):
        if not isinstance(source_id, str) or not source_id or len(source_id) > 64:
            raise ValueError('Некорректный ID источника.')
        with self.source_db() as db:
            runtime = source_management.runtime_snapshot(db, source_ids=[source_id])
            view = source_management.detail_view(self.source_document(), self.settings(), source_id, runtime, db)
        if view is None:
            raise ValueError('Такого источника нет в каталоге.')
        return view

    def _save_selection(self, settings):
        return self.save(settings)

    def apply_set(self, payload):
        """Opt in to one catalog set.  Nothing is added implicitly."""
        set_id = (payload or {}).get('set')
        if not isinstance(set_id, str) or not set_id:
            raise ValueError('Выберите набор источников.')
        settings = self.settings()
        if set_id not in {item['id'] for item in self.source_document()['sets']}:
            raise ValueError('Неизвестный набор источников.')
        result = source_management.apply_set(settings, set_id, self.source_document())
        saved = self._save_selection(result)
        return dict(settings=saved, set=set_id,
                    members=[value for value in result['source_selection']['selected_ids']],
                    disabled=saved['source_selection']['download_disabled_ids'])

    def toggle_source(self, payload):
        """One source: pause or resume its download.  Never changes the set."""
        source_id, disabled = _source_id_flag(payload, 'disabled')
        settings = self.settings()
        if not _known_source(self.source_document(), settings, source_id):
            raise ValueError('Такого источника нет в каталоге.')
        result = source_management.set_downloads(settings, [source_id], disabled, self.source_document())
        saved = self._save_selection(result)
        return dict(settings=saved, id=source_id, download_disabled=disabled,
                    in_set=source_id in saved['source_selection']['selected_ids'])

    def select_source(self, payload):
        source_id, selected = _source_id_flag(payload, 'selected')
        settings = self.settings()
        if not _known_source(self.source_document(), settings, source_id):
            raise ValueError('Такого источника нет в каталоге.')
        result = (source_management.select_ids(settings, [source_id], self.source_document()) if selected
                  else source_management.remove_sources(settings, [source_id], self.source_document()))
        saved = self._save_selection(result)
        return dict(settings=saved, id=source_id, selected=selected,
                    in_set=source_id in saved['source_selection']['selected_ids'])

    def remove_source(self, payload):
        """Remove a source from the active set.  Cache and history are kept."""
        source_id = _source_id(payload)
        settings = self.settings()
        if not _known_source(self.source_document(), settings, source_id):
            raise ValueError('Такого источника нет в каталоге.')
        result = source_management.remove_sources(settings, [source_id], self.source_document())
        saved = self._save_selection(result)
        return dict(settings=saved, id=source_id, removed=True,
                    in_set=source_id in saved['source_selection']['selected_ids'])

    def add_source_url(self, payload):
        """Add one of the user's own URLs with an explicitly chosen format."""
        payload = payload or {}
        url = payload.get('url')
        kind = payload.get('kind') or 'http'
        if not isinstance(url, str) or not url.strip():
            raise ValueError('Укажите адрес списка.')
        try:
            descriptor = source_catalog.custom_source(url.strip(), kind)
        except source_catalog.CatalogError as exc:
            raise ValueError(str(exc)) from None
        settings = self.settings()
        selection = copy.deepcopy(settings.get('source_selection') or {})
        custom = {item.get('id'): item for item in selection.get('custom_sources', []) if isinstance(item, dict)}
        custom[descriptor['id']] = {'id': descriptor['id'], 'url': descriptor['url'],
                                    'name': payload.get('name') or descriptor['url'],
                                    'adapter': descriptor['adapter']}
        selection['custom_sources'] = list(custom.values())
        selection['selected_ids'] = list(dict.fromkeys(list(selection.get('selected_ids', [])) + [descriptor['id']]))
        selection['download_disabled_ids'] = [value for value in selection.get('download_disabled_ids', [])
                                              if value != descriptor['id']]
        selection['catalog_revision'] = self.source_document()['revision']
        settings['source_selection'] = selection
        settings['settings_version'] = 3
        saved = self._save_selection(source_catalog.migrate_settings(settings, self.source_document()))
        return dict(settings=saved, id=descriptor['id'], url=descriptor['url'], kind=kind,
                    adapter=descriptor['adapter']['kind'], added=True)

    def _preview_plan(self, payload, settings):
        """Resolve a preview request to one concrete plan, without writing it."""
        payload = payload or {}
        source_id = payload.get('id')
        url = payload.get('url')
        kind = payload.get('kind') or 'http'
        if isinstance(url, str) and url.strip():
            allow_private = bool(payload.get('allow_private'))
            try:
                descriptor = source_catalog.custom_source(url.strip(), kind, allow_unsafe=allow_private)
            except source_catalog.CatalogError as exc:
                raise ValueError(str(exc)) from None
            return source_id or descriptor['id'], descriptor['record'], allow_private
        if not isinstance(source_id, str) or not source_id:
            raise ValueError('Укажите источник или адрес списка.')
        catalog = self.source_document()
        selection = settings.get('source_selection') or {}
        custom = {item.get('id'): item for item in selection.get('custom_sources', []) if isinstance(item, dict)}
        item = source_catalog.source_by_id(catalog, source_id)
        if item is None or not item.get('endpoints'):
            item = source_catalog._custom_plan(source_id, custom[source_id], []) if source_id in custom else None
        if item is None:
            raise ValueError('Такого источника нет в каталоге.')
        return source_id, item, False

    def preview_source(self, payload):
        """Availability and format check. Never writes candidates or settings.

        The check runs the collector's own fetch path -- the same destination
        validation, the same redirect rules, the same byte and record budgets
        and the same adapter the profile names -- against a private temporary
        database.  It used to be a second, simpler implementation: a plain
        ``httpx.Client`` that followed redirects to anywhere, read the whole
        body with no budget and parsed it with a default profile.  That made a
        check able to reach a private address the collector would refuse, and
        able to answer for a format the collector cannot read.
        """
        from . import proxytool
        settings = self.settings()
        source_id, plan, allow_private = self._preview_plan(payload, settings)
        if not source_catalog.collectable_source(plan):
            raise ValueError('Это не список прокси-адресов: формат источника не поддерживается сборщиком.')
        report = self._preview_report(plan)
        return source_management.preview_view(report, source_id, plan.get('name'))

    def _preview_report(self, plan):
        """One checked source, through ``proxytool``, writing nothing."""
        from . import proxytool
        try:
            db = self.read_db()
        except sqlite3.Error:
            db = None
        if db is None:
            db = proxytool.open_db(self.data / 'preview-check.sqlite3')
            try:
                report = self._run_preview(plan, db)
            finally:
                db.close()
                path = self.data / 'preview-check.sqlite3'
                for suffix in ('', '-wal', '-shm'):
                    (path.parent / (path.name + suffix)).unlink(missing_ok=True)
            return report
        try:
            return self._run_preview(plan, db)
        finally:
            db.close()

    #: A check is a question, not a collection.  Eight seconds is long enough
    #: for a list on a slow link and short enough that a page waiting on it
    #: stays a page; the collector's own default is four times that.
    PREVIEW_TIMEOUT = 8

    def _run_preview(self, plan, db):
        from . import proxytool
        return proxytool.run_preview(db, [plan], timeout=self.PREVIEW_TIMEOUT,
                                     max_source_candidates=proxytool.PREVIEW_CHECK_CANDIDATES,
                                     allow_private_sources=True,
                                     allow_private_endpoints=True)

    # --- source recovery and scope exclusions -----------------------------
    #
    # These four used to answer a fixed "ok" body no matter what the database
    # held, which is worse than no route at all: the page showed an action as
    # done when nothing had happened.  They now do the work they name, and when
    # the work is not possible they say so with a reason instead of a smile.
    #
    # The exclusions live in `db.candidate_scope_exclusion` (migration 18), so
    # they are read by the same names the rest of the storage uses and undo is
    # a delete.  The addresses themselves are never deleted: an exclusion
    # removes an address from the user's *scope*, and `scoped_rows` is what
    # every read of the results and of the export goes through, so the
    # exclusion is real where the user meets it.

    def source_scope_keys(self, source_id):
        """The ``candidate_seen`` keys of one source, resolved from the catalog.

        A catalog source is stored under its own id -- that is what makes the
        same list one source whether it was reached from the flat URL file or
        from the catalog screen -- so the id is a key in its own right.  The
        short digest of each configured entry is kept alongside it, because a
        collection made before the catalog existed has its membership under
        that digest and this must still be able to exclude, recover or count
        it.  A source that resolves to nothing is reported as unknown rather
        than silently excluding zero addresses.
        """
        source_id = str(source_id or '').strip()
        if not source_id:
            raise ValueError('Укажите источник.')
        keys = {source_id}
        try:
            catalog = self.source_document()
        except (OSError, ValueError):
            catalog = {}
        item = source_catalog.source_by_id(catalog, source_id) if catalog else None
        if item is not None:
            for endpoint in (item.get('endpoints') or []):
                url = endpoint.get('url') if isinstance(endpoint, dict) else None
                if url:
                    keys.add(core.source_key(url))
            for spec in (item.get('legacy_specs') or []):
                if isinstance(spec, str) and spec.strip():
                    keys.add(core.source_key(spec.strip()))
            if item.get('url'):
                keys.add(core.source_key(item['url']))
        settings = self.settings()
        selection = settings.get('source_selection') or {}
        for custom in (selection.get('custom_sources') or []):
            if isinstance(custom, dict) and custom.get('id') == source_id and custom.get('url'):
                keys.add(core.source_key(custom['url']))
        return source_id, sorted(keys)

    def recover_source(self, payload):
        """Clear the failure state of one source and re-check it once.

        Recovery is a state transition with evidence behind it: the runtime
        table of the source is reset (quarantine and backoff gone) and the one
        availability check that follows is a real request.  Without a catalog
        entry there is nothing to recover and the route says so.
        """
        source_id, keys = self.source_scope_keys((payload or {}).get('id'))
        catalog = self.source_document()
        item = source_catalog.source_by_id(catalog, source_id) if catalog else None
        known = item is not None and bool(item.get('endpoints') or item.get('url'))
        cleared = 0
        if known and keys:
            placeholders = ','.join('?' * len(keys))
            try:
                with self.collection_write() as conn:
                    # What "cleared" counts is the pause itself: the backoff,
                    # the quarantine and the failure counter of this source.
                    # The metadata the source wrote is cleaned with it, but it
                    # is not what recovery is about, and counting it made the
                    # answer say "0 cleared" for a source whose quarantine had
                    # just been lifted.
                    cleared = conn.execute(
                        'DELETE FROM source_state WHERE source_id IN (%s)' % placeholders,
                        keys).rowcount or 0
                    conn.execute('DELETE FROM candidate_meta WHERE source IN (%s)' % placeholders,
                                 keys)
                    conn.commit()
            except (schema.DbError, sqlite3.Error, ValueError):
                cleared = 0
        if not known:
            return dict(id=source_id, recovered=False, cleared=cleared, known=False,
                        error='Такого источника нет в каталоге; восстанавливать нечего.',
                        check=None)
        try:
            # ``preview_source`` answers with the flat preview view, so the
            # re-check read a key that view never had and reported nothing
            # after every request it had just made.
            outcome = self.preview_source({'id': source_id}) or {}
            report = {'http_state': outcome.get('http_state'),
                      'parse_state': outcome.get('parse_state'),
                      'status': outcome.get('status'),
                      'recognized': outcome.get('recognized'),
                      'accepted': outcome.get('accepted'),
                      'complete': outcome.get('complete'),
                      'error': outcome.get('error')}
        except ValueError as exc:
            # The state was cleared and the single check that follows refused
            # the source.  Both facts are reported: a recovery that half
            # happened is neither a failure nor a success.
            report = {'http_state': 'refused', 'parse_state': None, 'status': None,
                      'recognized': 0, 'complete': False, 'error': str(exc)}
        return dict(id=source_id, recovered=True, cleared=cleared, known=True,
                    name=item.get('name') or source_id, check=report)

    def import_scope_sidecar(self):
        """Move exclusions written by a build without the table into it, once.

        The previous storage was `gui-scope-exclusions.json`; the table arrived
        with migration 18.  The addresses in the sidecar were the user's own
        decision, so they are carried over rather than dropped, and the import
        is keyed the same way the table is -- one row per (address, scope) --
        so running it twice adds nothing.
        """
        if self._scope_sidecar_imported:
            return 0
        self._scope_sidecar_imported = True
        legacy = [item for item in (self.scope_exclusions_doc.read().get('exclusions') or [])
                  if isinstance(item, dict) and item.get('proxy')]
        if not legacy:
            return 0
        try:
            with self.collection_write() as conn:
                for item in legacy[:SCOPE_EXCLUSION_LIMIT]:
                    try:
                        schema.add_scope_exclusion(
                            conn, str(item['proxy']), source_id=item.get('source'),
                            as_seen=item.get('as_seen'),
                            reason=str(item.get('reason') or 'source_scope'),
                            shared=bool(item.get('shared')))
                    except (schema.DbError, sqlite3.Error, ValueError):
                        continue
                conn.commit()
        except (schema.DbError, sqlite3.Error, OSError):
            return 0
        return len(legacy)

    def scope_exclusion_rows(self):
        """The exclusions as they are stored, newest last, bounded.

        Read through `db.scope_exclusions`, so a row written by anything else
        (a second window of the same application, a restore) is visible here
        too.  A database that is not there yet is an empty scope, not an
        error: the page says "nothing excluded" and the collection is intact.
        """
        self.import_scope_sidecar()
        conn = self.read_rows_connection()
        if conn is None:
            return []
        try:
            rows = schema.scope_exclusions(conn)
        except (schema.DbError, sqlite3.Error):
            rows = []
        finally:
            with suppress(sqlite3.Error):
                conn.close()
        return [dict(proxy=row.get('proxy'), as_seen=row.get('as_seen'),
                     source=row.get('source_id'), reason=row.get('reason'),
                     created_at=row.get('created_at'), shared=bool(row.get('shared')))
                for row in rows if row.get('proxy')]

    def scope_exclusions(self, query=None):
        """What is excluded from the user's scope right now, and by whom."""
        rows = self.scope_exclusion_rows()
        sources = sorted({str(item.get('source') or '') for item in rows} - {''})
        return dict(count=len(rows), proxies=rows[:SCOPE_EXCLUSION_LIMIT], sources=sources,
                    limit=SCOPE_EXCLUSION_LIMIT, truncated=len(rows) > SCOPE_EXCLUSION_LIMIT,
                    storage='database')

    # -- source comparison (F21) --------------------------------------------

    def source_comparison(self, payload):
        """Publishers compared on what was actually measured (F21).

        The sources page is where the catalog lives, so it is also where the
        question "which of these actually adds anything?" is asked.  The
        comparison itself is ``sourcedesk``'s and is reached through the one
        ``Workbench`` the CLI and the API also use, so the number on this page
        and the number in a terminal cannot disagree.

        Nothing is collected and no address is contacted: the rows already in
        the database are read.  A database with no measurement therefore
        answers "there is nothing to compare" instead of an empty table that
        would read as "nothing passed".
        """
        payload = payload or {}
        wanted = [part.strip() for part in
                  str(payload.get('sources') or '').replace(',', ' ').split() if part.strip()]
        if not wanted:
            raise ValueError('Выберите хотя бы один источник для сравнения.')
        if len(wanted) > SOURCE_COMPARE_LIMIT:
            raise ValueError(f'Сравнивать можно не больше {SOURCE_COMPARE_LIMIT} источников.')
        windows = int(payload.get('survival_windows') or 0)
        try:
            return self._source_comparison(wanted, payload, windows)
        except core.WorkbenchError as exc:
            # ``WorkbenchError`` is a ``RuntimeError``, which the GET handler
            # does not name -- it escaped, the worker thread died mid-response
            # and the page saw a dropped connection instead of the sentence the
            # refusal carries.  A rejected control is a message (F25).
            raise ValueError(str(exc)) from None

    def _source_comparison(self, wanted, payload, windows):
        with core.Workbench(self.data) as workbench:
            cohort = workbench.source_cohort(
                wanted, label='sources-page',
                start=payload.get('start'), end=payload.get('end'),
                profile_id=str(payload.get('profile_id') or ''),
                profile_revision=int(payload.get('profile_revision') or 1),
                collection_id=str(payload.get('collection_id') or ''),
                min_success=float(payload.get('min_success') or 2 / 3))
            report = workbench.source_comparison(
                wanted, cohort=cohort,
                family_jaccard=payload.get('family_jaccard'),
                sample_floor=payload.get('sample_floor'))
            body = report.as_dict()
            if payload.get('survival') or windows:
                from . import sourcedesk
                body['survival'] = [step.as_dict() for step in sourcedesk.survival_across_windows(
                    workbench.conn, sources=tuple(sorted(wanted)),
                    profile_id=cohort.profile_id, profile_revision=cohort.profile_revision,
                    collection_id=cohort.collection_id, min_success=cohort.min_success,
                    start=cohort.start, end=cohort.end, count=max(2, windows or 3))]
            if len(wanted) == 2:
                # Two sources are also the supplier question, and the same
                # cohort answers it -- so the page can say whether the two are
                # really independent publishers.
                body['suppliers'] = workbench.source_supplier_comparison(
                    wanted[0], wanted[1], cohort=cohort).as_dict()
        return body

    def exclude_source_scope(self, payload):
        """Exclude the addresses one source delivered from the current scope.

        Only *exclusive* addresses go by default: an address another source
        also delivered is not this source's to remove, and a shared address
        needs an explicit ``include_shared`` (source-system-design §12.3).
        """
        payload = payload or {}
        source_id, keys = self.source_scope_keys(payload.get('id'))
        if not keys:
            return dict(id=source_id, excluded=0, delivered=0, shared=0, already_excluded=0,
                        exclusive=0, error='Источник ничего не доставлял; исключать нечего.')
        include_shared = bool(payload.get('include_shared'))
        conn = self.read_connection()
        if conn is None:
            raise ValueError('Локальная база ещё не создана. Сначала соберите адреса.')
        try:
            placeholders = ','.join('?' * len(keys))
            delivered = [row[0] for row in conn.execute(
                'SELECT proxy FROM candidate_seen WHERE source IN (%s) ORDER BY proxy' % placeholders,
                keys)]
            shared = set()
            for row in conn.execute(
                    'SELECT proxy, COUNT(DISTINCT source) AS n FROM candidate_seen '
                    'WHERE proxy IN (SELECT proxy FROM candidate_seen WHERE source IN (%s)) '
                    'GROUP BY proxy HAVING n > 1' % placeholders, keys):
                shared.add(row[0])
        except sqlite3.Error:
            delivered, shared = [], set()
        finally:
            with suppress(sqlite3.Error):
                conn.close()
        already = {str(item.get('proxy')) for item in self.scope_exclusion_rows()}
        exclusive = [value for value in delivered if value not in shared]
        chosen = delivered if include_shared else exclusive
        added, present = [], 0
        stamp = time.time()
        with self.collection_write() as writer:
            for value in chosen:
                # `candidate_seen.proxy` is the address exactly as a list
                # published it (`198.51.100.1:8080`), while a results row and
                # `endpoints.canonical` carry the normalised one
                # (`http://198.51.100.1:8080`).  The exclusion is stored in the
                # normalised form, because that is the form `scoped_rows`
                # compares against; without this the list would be right and
                # the exclusion would hide nothing.
                canonical = core.normalize_custom(value) or value
                if canonical in already:
                    present += 1
                    continue
                try:
                    schema.add_scope_exclusion(
                        writer, canonical, source_id=source_id, as_seen=value,
                        reason='source_scope', shared=value in shared, now=stamp)
                except (schema.DbError, sqlite3.Error):
                    continue
                already.add(canonical)
                added.append(canonical)
            writer.commit()
        return dict(id=source_id, excluded=len(added), delivered=len(delivered),
                    exclusive=len(exclusive), shared=len(shared & set(delivered)) - present,
                    already_excluded=present, include_shared=include_shared,
                    excluded_total=len(self.scope_exclusion_rows()),
                    sample=[{'proxy': value, 'shared': value in shared} for value in added[:20]])

    def clear_scope_exclusions(self, payload):
        """Remove exclusions, either all of them or those of one source."""
        payload = payload or {}
        source_id = payload.get('source')
        if source_id:
            source_id, _keys = self.source_scope_keys(source_id)
        with self.collection_write() as conn:
            try:
                removed = schema.clear_scope_exclusions(conn, source_id=source_id or None)
                conn.commit()
            except (schema.DbError, sqlite3.Error) as exc:
                raise ValueError('Не удалось вернуть адреса в область: %s' % exc) from None
        return dict(removed=removed, remaining=len(self.scope_exclusion_rows()),
                    source=str(source_id) if source_id else None)

    def refresh_catalog(self, payload=None):
        with self.mutex:
            if self.catalog_job.get('running'):
                return dict(self.catalog_job)
            self.catalog_job = {'running': True, 'stage': 'starting', 'started_at': time.time(),
                                'added': 0, 'changed': 0, 'retired': 0}
        job = self.catalog_job

        def work():
            from . import proxytool
            url = os.environ.get('PROXY_WORKBENCH_SOURCES_URL', SOURCE_CATALOG_URL)
            try:
                job['stage'] = 'downloading'
                # The catalog is fetched by the same bounded, destination-
                # validated owner as a proxy list, with the stored validators so
                # a publisher with nothing new costs a 304 instead of a body.
                # It used to be a plain ``httpx.Client`` that followed redirects
                # anywhere and read the whole answer with no limit, which made
                # this the one route in the app that could be pointed wherever.
                current = self.source_document()
                state = self.read_source_catalog_cache()
                answer = asyncio.run(proxytool.fetch_catalog(
                    url, current=current, allow_private_sources=False,
                    etag=state.get('etag'), last_modified=state.get('last_modified')))
                if answer.get('state') == 'not_modified':
                    job.update(running=False, stage='done', not_modified=True, error=None)
                    return
                job['stage'] = 'validating'
                incoming = answer['catalog']
                if not isinstance(incoming, dict):
                    raise ValueError('Каталог источников недоступен.')
                diff = source_catalog.catalog_diff(current, incoming)
                core.atomic(self.data/self.CATALOG_FILE, json.dumps(incoming, ensure_ascii=False))
                self._catalog_cache = incoming
                core.atomic(self.data/self.CATALOG_STATE_FILE, json.dumps(
                    {'etag': answer.get('etag'), 'last_modified': answer.get('last_modified'),
                     'body_sha256': answer.get('body_sha256'),
                     'fetched_at': time.time()}, ensure_ascii=False))
                job.update(running=False, stage='done', error=None, **diff)
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                job.update(running=False, stage='error', error=reason or type(exc).__name__)
        threading.Thread(target=work, daemon=True).start()
        return dict(self.catalog_job)

    def catalog_update_status(self):
        with self.mutex:
            return dict(self.catalog_job)

    def export_settings(self, payload):
        """Validate a settings backup without persisting the submitted form."""
        if not isinstance(payload, dict):
            raise ValueError('Ожидается JSON объект настроек.')
        if 'denylist' not in payload:
            payload = dict(payload)
            payload['denylist'] = self.settings().get('denylist', '')
        return validate(payload)

    def snapshot(self, now=None):
        """The pinned current generation, read without the writer lock."""
        return read_snapshot(self.data, now=now)

    def export_status(self, snapshot=None, now=None):
        """Coherent status for the published generation, per-row (defect 3).

        The published ``status.json`` carries one set-wide ``valid_until``.  Using
        it as "the snapshot expired" is exactly the defect that empties a
        mixed-age pool, so freshness is decided per row here and the set state
        is derived from the rows that survived.
        """
        snapshot = self.snapshot(now=now) if snapshot is None else snapshot
        status = dict(snapshot.status)
        now = time.time() if now is None else now
        if not status:
            if snapshot.state in (SNAPSHOT_BROKEN, SNAPSHOT_MISSING):
                # An unreadable publication is named as such.  Silently serving
                # database rows the reader itself rejects is defect 9 (R05).
                return dict(state='error', state_detail=snapshot.detail or 'no_snapshot',
                            published=False, reader_state=snapshot.state, available=0,
                            expired_count=0, stale=False, empty_export=False)
            return status
        # A published generation is immutable, so its per-row admission only has
        # to be recomputed when the generation or the clock moved far enough.
        stamp = self.status_stamp(snapshot)
        cached_stamp, cached_at, cached = self._status_cache
        if cached_stamp == stamp and cached is not None and abs(now - cached_at) < 5.0:
            return dict(cached)
        policy = self.snapshot_policy(status)
        selection = admission.select(snapshot.rows, self.snapshot_scope(status), self.snapshot_access(),
                                     policy, now, published_at=status.get('generated_at'),
                                     generation=snapshot.generation)
        status.update(selection.as_status())
        status['available'] = len(selection.admitted)
        status['expired_count'] = sum(1 for item in selection.rejected
                                      if item.reason_code in admission.TIME_REASONS)
        status['rows_total'] = len(snapshot.rows)
        status['stale'] = selection.state == admission.SELECTION_EMPTY and bool(selection.rejected) and (
            selection.state_detail in (admission.DETAIL_ALL_EXPIRED, admission.DETAIL_ALL_FAILED))
        status['empty_export'] = selection.state == admission.SELECTION_EMPTY
        status['reader_state'] = snapshot.state
        status['published'] = snapshot.state in (SNAPSHOT_OK, SNAPSHOT_LEGACY)
        if not status['published']:
            # A broken pointer is reported as broken; it never falls back to rows.
            status['state'] = 'error'
            status['state_detail'] = snapshot.detail or 'pointer_unreadable'
            status['stale'] = False
        self._status_cache = (stamp, now, dict(status))
        return status

    @staticmethod
    def status_stamp(snapshot):
        paths = []
        if snapshot.directory and snapshot.generation:
            base = snapshot.directory/'generations'/snapshot.generation
            paths = [base/'ranked.json', base/'status.json']
        else:
            paths = [snapshot.directory/'ranked.json', snapshot.directory/'status.json'] if snapshot.directory else []
        marks = []
        for path in paths:
            try:
                info = path.stat()
                marks.append((info.st_mtime_ns, info.st_size))
            except OSError:
                marks.append(None)
        return (snapshot.state, snapshot.generation, tuple(marks), snapshot.detail)

    def snapshot_scope(self, status):
        profile = status.get('profile') if isinstance(status, dict) else None
        return admission.Scope(collection_id=str((status or {}).get('collection_id') or 'default'),
                               profile_id=str(profile or ''), profile_revision=1,
                               network_id=str((status or {}).get('request_profile_digest') or 'default'))

    def snapshot_access(self):
        return admission.Access(access_id='default', access_revision=1)

    def snapshot_policy(self, status):
        """Admission policy of a published snapshot, from its own status."""
        max_age = (status or {}).get('max_age_seconds')
        try:
            max_age = float(max_age)
        except (TypeError, ValueError):
            max_age = admission.DEFAULT_MAX_AGE_SECONDS
        if not math.isfinite(max_age) or max_age <= 0:
            max_age = admission.DEFAULT_MAX_AGE_SECONDS
        scope = (status or {}).get('scope') or {}
        countries = frozenset(code for code in (scope.get('countries') or []) if isinstance(code, str))
        protocol = scope.get('protocol')
        return admission.Policy(max_age_seconds=max_age,
                                min_success=float((status or {}).get('min_success') or 2/3),
                                min_anonymity=str((status or {}).get('min_anonymity') or 'any'),
                                strict=bool(((status or {}).get('reputation') or {}).get('strict')),
                                protocol=protocol if protocol in core.PROTOCOLS else None,
                                countries=countries,
                                exclude_hosting=bool(scope.get('exclude_hosting')),
                                max_latency_ms=(float(scope['max_latency'])
                                                if isinstance(scope.get('max_latency'), (int, float)) else None),
                                allow_missing_identity=True)

    def export_downloads(self, status=None):
        """Advertise files only for a coherent, non-stale published snapshot."""
        status = self.export_status() if status is None else status
        if not status or not status.get('published') or status.get('state') == 'error' or status.get('stale'):
            return []
        snapshot = self.snapshot()
        return [name for name in DOWNLOADS
                if core.export_file(self.data/'exports', name, generation=snapshot.generation).is_file()]


    def gateway_lan_mode(self):
        """What the listener must be told: LAN on/off and which adapter.

        ``resolve_bind`` refuses an interface without the opt-in, so the two
        travel together.  A non-loopback ``--gateway-host`` is already an
        explicit request to be reachable, and it does not need the flag twice.
        """
        host = (getattr(self, 'gateway_bind', None) or {}).get('host') or '127.0.0.1'
        lan = getattr(self, 'gateway_lan', None)
        if lan is None:
            lan = not api.is_loopback(host)
        return bool(lan), getattr(self, 'gateway_interface', None)

    def gateway_state(self):
        """Everything the connect page needs, and nothing it must not publish.

        The QR and the copy address carry the gateway password only.  The GUI
        session token and the API token are different identities (defect 18,
        F17) and must never appear here, in a file, or in a log.
        """
        runner = getattr(self, 'gateway', None)
        if runner is None:
            return None
        snapshot = runner.server.gateway.pool.snapshot(top=5)
        host = getattr(runner, 'display_host', '127.0.0.1')
        bind_host = getattr(runner, 'host', '127.0.0.1')
        address = f'[{host}]:{runner.port}' if ':' in host else f'{host}:{runner.port}'
        token = getattr(runner, 'token', None)
        # A phone can use the QR only when the listener is reachable beyond
        # this computer.  The question is answered by the listener itself, not
        # by the address that was asked for: `--lan` with the default address
        # listens on the wildcard, so `runner.host` is still `127.0.0.1` while
        # the socket is already in the network -- and a QR that refuses to
        # appear for a listener a phone can reach is the exact lie F17 is
        # about.  `reachable_from_lan` is `bind.lan and not bind.local and
        # token`, so a passwordless LAN listener is not advertised either.
        listen_host = getattr(runner, 'listen_host', bind_host)
        mobile_ready = bool(getattr(runner.gateway, 'reachable_from_lan', False))
        copy_address = address
        if token:
            copy_address = f'http://workbench:{quote(token, safe="")}@{address}'
        published = self.export_status()
        binding = dict(generation=published.get('generation'),
                       published_at=published.get('published_at') or published.get('generated_at'),
                       expires_at=published.get('expires_at'),
                       state=published.get('state'), state_detail=published.get('state_detail'),
                       state_detail_label=state_detail_text(published.get('state_detail')),
                       scope=published.get('scope') or {},
                       available=published.get('available', 0))
        return dict(snapshot, address=address, copy_address=copy_address, bind_host=bind_host,
                    listen_host=listen_host, lan=bool(getattr(runner, 'lan', False)),
                    mobile_ready=mobile_ready, udp_supported=False,
                    username='workbench' if token else None,
                    password=token if token else None, proxies=snapshot['available'],
                    binding=binding, transport=['http', 'socks5'],
                    probe_note=tr('Лента и быстрый тест — это пробы Workbench, а не трафик вашего клиента.',
                                  'The live feed and the quick test are Workbench probes, not your client traffic.'),
                    disconnect_hint=tr('Отключение: уберите прокси в приложении или остановите ротирующий прокси.',
                                       'To disconnect: remove the proxy in your app, or stop the rotating proxy.'))

    def start_gateway(self):
        """A way back in: recreate the listener after Stop, same host, port and token."""
        runner = getattr(self, 'gateway', None)
        if runner is not None:
            return self.gateway_state()
        bind = getattr(self, 'gateway_bind', None)
        if bind is None:
            raise ValueError('Ротирующий прокси отключён при запуске приложения (--no-gateway).')
        lan, interface = self.gateway_lan_mode()
        with self.mutex:
            try:
                # `bind=` is the pool/profile the user chose on the gateway
                # page: without it the listener served the whole published
                # export whatever the page said, and the pool picker was a
                # control that did nothing.  `lan=`/`interface=` are the
                # opt-in that makes `--lan` reach the socket.
                self.gateway = gateway.Background(self.data, bind['host'], bind['port'],
                                                  token=self.gateway_token,
                                                  bind=self.gateway_binding(),
                                                  lan=lan, interface=interface)
            except (OSError, ValueError) as exc:
                self.gateway = None
                raise ValueError(f'Не удалось запустить ротирующий прокси: {exc}. Проверьте порт и адрес.') from None
        return self.gateway_state()

    def stop_gateway(self):
        """A clear way out of the connection path: the listener really stops."""
        runner = getattr(self, 'gateway', None)
        if runner is None:
            raise ValueError('Ротирующий прокси не запущен.')
        with self.mutex:
            try:
                runner.close()
            except Exception as exc:
                raise ValueError(f'Не удалось остановить ротирующий прокси: {exc}') from None
            self.gateway = None
        return dict(stopped=True, address=None)

    def prune_sources(self, payload):
        """Drop sources that delivered only non-working fresh profile checks."""
        settings = validate(payload)
        status = read_json(core.export_file(self.data/'exports', 'status.json'), {})
        if status.get('stale') or status.get('state') in ('error', 'stale'):
            quality = {}
        else:
            quality = status.get('source_quality') or {}
        dead = [source for source in settings['sources']
                if (stats := quality.get(core.source_key(source))) and stats.get('checked', 0) >= PRUNE_MIN_CHECKED
                and not stats.get('passed')]
        settings['sources'] = [source for source in settings['sources'] if source not in dead]
        saved = self.save(settings)
        return dict(settings=saved, removed=[public_source(source) for source in dead])

    def update_sources(self, payload):
        """Add sources that were published after this version; never re-adds removed built-ins."""
        settings = validate(payload)
        try:
            response = httpx.get(os.environ.get('PROXY_WORKBENCH_SOURCES_URL', SOURCES_URL), timeout=20,
                                 follow_redirects=False, trust_env=False)
            response.raise_for_status()
            if len(response.content) > 1_000_000:
                raise ValueError
            latest = response.json()
            if not isinstance(latest, list):
                raise ValueError
            latest = [source for source in latest if isinstance(source, str)]
            for source in latest:
                core.source_spec(source)
        except (httpx.HTTPError, ValueError):
            raise ValueError('Не удалось получить список источников с GitHub.') from None
        bundled = set(defaults()['sources'])
        known = set(settings['sources'])
        added = [source for source in latest if source not in bundled and source not in known]
        settings['sources'] = settings['sources'] + added
        saved = self.save(settings)
        return dict(settings=saved, added=[public_source(source) for source in added])

    def running(self):
        return self.process is not None and (self.process.poll() is None or self.log_handle is not None)

    @contextmanager
    def data_lock(self):
        with self.mutex:
            with exclusive_lock(self.data/'workbench.lock'):
                yield

    def start(self, payload, max_selection=MAX_SELECTION):
        with self.mutex:
            if not isinstance(payload, dict):
                raise ValueError('Ожидается JSON объект.')
            if self.running():
                raise ValueError('Проверка уже идёт. Сначала остановите её.')
            action = payload.get('action', 'run')
            if action not in ('run', 'scan', 'recheck', 'recheck_passing', 'collect', 'export'):
                raise ValueError('Неизвестное действие.')

            selection = None
            export_query = ''
            export_hosting = ''
            export_quick = ''
            if action == 'export':
                if 'selection' in payload:
                    values = payload.get('selection')
                    if not isinstance(values, list) or not 1 <= len(values) <= max_selection:
                        raise ValueError(f'Выберите от 1 до {max_selection} прокси для экспорта.')
                    selection = []
                    seen = set()
                    for value in values:
                        if not isinstance(value, str):
                            raise ValueError('Выбран список содержит некорректный адрес прокси.')
                        normalized = core.normalize(value)
                        if normalized is None:
                            raise ValueError('Нужен публичный IP-адрес, порт и протокол без логина или пароля.')
                        if normalized not in seen:
                            seen.add(normalized)
                            selection.append(normalized)
                export_query = payload.get('q', '')
                if not isinstance(export_query, str) or len(export_query) > 100:
                    raise ValueError('Поиск экспорта слишком длинный: максимум 100 символов.')
                export_query = export_query.strip()
                export_hosting = normalize_hosting_filter(payload.get('hosting', ''))
                export_quick = payload.get('quick', '')
                if export_quick not in ('', 'clean', 'speed', 'http'):
                    raise ValueError('Неверный быстрый фильтр экспорта.')
                try:
                    active_profile = (self.data/'last-profile.txt').read_text(encoding='utf-8').strip()
                except (OSError, UnicodeError):
                    active_profile = ''
                if not active_profile or not (self.data/'proxies.sqlite3').is_file():
                    raise ValueError('Сначала запустите проверку.')
                try:
                    db = sqlite3.connect((self.data/'proxies.sqlite3').as_uri()+'?mode=ro', uri=True, timeout=2)
                    try:
                        active = db.execute('SELECT 1 FROM profiles WHERE id=?', (active_profile,)).fetchone()
                    finally:
                        db.close()
                except sqlite3.Error:
                    raise ValueError('Не удалось прочитать активный профиль. Перезапустите проверку.') from None
                if active is None:
                    raise ValueError('Активный профиль не найден. Сначала запустите проверку.')
            elif 'selection' in payload:
                raise ValueError('Выбранные адреса можно экспортировать только действием export.')

            submitted = payload.get('settings')
            if action == 'export':
                # Export controls are deliberately transient. Only scans and collection
                # update the settings that define the next profile.
                settings = validate(self.settings() if submitted is None else submitted)
                if 'hosting' in payload:
                    settings['exclude_hosting'] = export_hosting == 'hide'
                if selection is not None:
                    settings['top'] = 0
            else:
                settings = self.save(self.settings() if submitted is None else submitted)
            if action in ('run', 'collect') and not settings['proxies'].strip() and (not settings['use_sources'] or not settings['sources']):
                raise ValueError('Включите источники или добавьте свой список прокси.')
            # F02: the scope of the check is decided before the worker starts,
            # and an unknown collection is refused here instead of becoming a
            # scan of something else.  The publication carries the same scope,
            # so the results table and the export agree with the check.
            scope_collection = ''
            if settings['collection']:
                conn = self.read_connection()
                if conn is not None:
                    try:
                        scope_collection = self.require_collection(conn, settings['collection'])
                    finally:
                        conn.close()

            selection_path = self.data/'gui-selection.json'
            selection_path.unlink(missing_ok=True)
            if selection is not None:
                core.atomic(selection_path, json.dumps(selection, ensure_ascii=False))
            core.atomic(self.data/'gui-targets.json', json.dumps({
                'targets': settings['targets'], 'request_profile': settings['request_profile'],
                'reputation': settings['reputation'], 'anonymity': settings['anonymity'],
                'speedtest': settings['speedtest']}, ensure_ascii=False))
            core.atomic(self.data/'gui-sources.json', json.dumps(settings['sources']))
            core.atomic(self.data/'gui-input.txt', settings['proxies'])
            self.stop_path.unlink(missing_ok=True)
            core.atomic(self.progress_path, json.dumps(dict(phase='starting', checked=0, candidates=0)))
            command = paths.worker_command('scan' if action in ('recheck', 'recheck_passing') else action,
                       '--data', str(self.data), '--config', str(self.data/'gui-targets.json'),
                       '--sources', str(self.data/'gui-sources.json'), '--input', str(self.data/'gui-input.txt'),
                       '--denylist-file', str(self.data/'denylist.txt'),
                       '--progress-file', str(self.progress_path), '--stop-file', str(self.stop_path))
            if selection is not None:
                command.extend(['--selection-file', str(selection_path)])
            if scope_collection and action in ('run', 'scan', 'recheck', 'recheck_passing'):
                # A check is a measurement of one collection; the worker reads
                # membership as the candidate set (F02, CONTRACTS §1.2).
                command.extend(['--collection', scope_collection])
            if action == 'export':
                if scope_collection:
                    # The published generation is a statement about the same
                    # collection the check measured (F02).
                    command.extend(['--collection', scope_collection])
                if 'q' in payload:
                    command.extend(['--export-query', export_query])
                if 'hosting' in payload:
                    command.extend(['--export-hosting', export_hosting])
                if 'quick' in payload:
                    command.extend(['--export-quick', export_quick])
            for key in ('attempts', 'timeout', 'connect_timeout', 'workers', 'rate', 'max_bytes', 'source_timeout',
                        'min_success', 'top', 'sort', 'min_anonymity', 'protocol', 'max_latency', 'want', 'watch', 'prefilter'):
                command.extend(['--'+key.replace('_', '-'), str(settings[key])])
            command.append('--fail-fast' if settings['fail_fast'] else '--no-fail-fast')
            if settings['detect_protocols']:
                command.append('--detect-protocols')
            if settings['exclude_hosting']:
                command.append('--no-hosting')
            command.extend(['--country', settings['countries']])
            reputation = settings['reputation']
            command.append('--local-denylist' if reputation['local_enabled'] else '--no-local-denylist')
            if reputation['dnsbl_enabled']:
                command.append('--dnsbl')
            for zone in reputation['dnsbl_zones']:
                command.extend(['--dnsbl-zone', zone])
            command.extend(['--reputation-timeout', str(reputation['timeout'])])
            if reputation['strict']:
                command.append('--strict-clean')
            if not settings['use_sources']:
                command.append('--no-sources')
            if action == 'recheck':
                command.append('--recheck')
            if action == 'recheck_passing':
                command.append('--recheck-passing')
            self.job = dict(id=secrets.token_hex(8), action=action, started_at=time.time(),
                            targets=[dict(name=t.get('name', ''), url=core.public_url(t['url'])) for t in settings['targets']],
                            min_success=settings['min_success'], sort=settings['sort'], top=settings['top'],
                            request_profile=settings['request_profile'], reputation=reputation,
                            anonymity=bool(settings['anonymity']['judge_url']), min_anonymity=settings['min_anonymity'],
                            collection=scope_collection or schema.PUBLIC_COLLECTION_ID,
                            selection_requested=len(selection) if selection is not None else 0)
            if self.log_handle:
                self.log_handle.close()
            self.log_handle = (self.data/'gui-run.log').open('wb')
            try:
                self.process = subprocess.Popen(command, stdout=self.log_handle, stderr=subprocess.STDOUT,
                                                stdin=subprocess.DEVNULL, cwd=ROOT.parent, env=CHILD_ENV)
            except OSError:
                self.log_handle.close()
                self.log_handle = None
                selection_path.unlink(missing_ok=True)
                raise ValueError('Не удалось запустить проверку.') from None
            core.atomic(self.data/'gui-job.json', json.dumps(self.job))
            threading.Thread(target=self._wait, args=(self.process,), daemon=True).start()
            return self.job

    def _wait(self, process):
        code = process.wait()
        with self.mutex:
            if self.process is process:
                self.job.update(exit_code=code, finished_at=time.time())
                core.atomic(self.data/'gui-job.json', json.dumps(self.job))
                (self.data/'gui-selection.json').unlink(missing_ok=True)
                if self.log_handle:
                    self.log_handle.close()
                    self.log_handle = None

    def stop(self):
        with self.mutex:
            if self.running():
                core.atomic(self.stop_path, 'stop')
                self.job['stopping'] = True
            return dict(stopping=self.running())

    def geo(self):
        """The offline country database, reloaded when the file changes."""
        path = geoip.default_path(self.data)
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return None
        if self.geo_cache[0] != stamp:
            try:
                self.geo_cache = (stamp, geoip.CountryDB.from_file(path))
            except (OSError, EOFError, UnicodeError, ValueError):
                self.geo_cache = (stamp, None)
        return self.geo_cache[1]

    def asn(self):
        """The offline provider (ASN) database, reloaded when the file changes."""
        path = geoip.asn_path(self.data)
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return None
        if self.asn_cache[0] != stamp:
            try:
                self.asn_cache = (stamp, geoip.AsnDB.from_file(path))
            except (OSError, EOFError, UnicodeError, ValueError):
                self.asn_cache = (stamp, None)
        return self.asn_cache[1]

    def geo_status(self):
        database, providers = self.geo(), self.asn()
        return dict(available=database is not None, ranges=database.size if database else 0,
                    providers=providers is not None, provider_ranges=providers.size if providers else 0,
                    attribution=geoip.ATTRIBUTION)

    def update_geo(self):
        with self.mutex:
            if self.running():
                raise ValueError('Сначала остановите текущую операцию.')
        # Not under the mutex: the download can take a while and the UI keeps
        # polling. The CLI takes the data-folder lock, so a scan cannot overlap.
        try:
            done = subprocess.run(paths.worker_command('update-geoip', '--data', str(self.data)),
                                  capture_output=True, text=True, timeout=600, cwd=ROOT.parent, stdin=subprocess.DEVNULL,
                                  env=CHILD_ENV)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('Не удалось скачать базу стран.') from None
        if done.returncode:
            lines = (done.stderr or done.stdout).strip().splitlines()
            raise ValueError(lines[-1] if lines else 'Не удалось скачать базу стран.')
        return self.geo_status()

    def clear_data(self):
        with self.mutex:
            if self.running():
                raise ValueError('Сначала остановите текущую операцию.')
            try:
                with self.data_lock():
                    removed = clear_runtime(self.data, keep_lock=True)
            except RuntimeError as exc:
                raise ValueError(str(exc)) from None
            # The measurement event log is machine runtime data and follows the
            # clear.  Tags, favourites, notes and saved views are user documents
            # and stay, exactly like the settings and the denylist.
            self.events_path.unlink(missing_ok=True)
            self.event_seq, self.event_seen, self.events_loaded = {}, set(), False
            return dict(removed=removed)

    def state(self):
        with self.mutex:
            active = self.running()
            export = self.export_status()
            state = dict(running=active, job=dict(self.job),
                         progress=read_json(self.progress_path, {}),
                         sources=read_json(self.data/'sources-report.json', {}),
                         source_urls=public_sources(read_json(self.data/'gui-sources.json', []), keyed=True),
                         source_keys=[core.source_key(url) for url in read_json(self.data/'gui-sources.json', [])
                                      if isinstance(url, str)],
                         export=export,
                         diagnostic=read_json(core.export_file(self.data/'exports', 'status.json',
                                                                pointer='diagnostic.json'), {}),
                         api=getattr(self, 'api_url', None),
                         gateway=self.gateway_state(),
                         downloads=self.export_downloads(export),
                         tags=self.tags_in_use(),
                         views=self.saved_views(),
                         history=[dict(item, undo=None) for item in
                                  (self.history.read().get('entries') or [])[-10:]][::-1],
                         columns=list(COMPACT_COLUMNS),
                         all_columns=list(ALL_COLUMNS))
            if not active and self.job.get('exit_code', 0) not in (0, 130):
                state['progress']['phase'] = 'error'
            elif not active and state['progress'].get('phase') in ('starting', 'scanning', 'collecting', 'exporting', 'waiting'):
                state['progress']['phase'] = 'interrupted'
            try:
                with (self.data/'gui-run.log').open('rb') as handle:
                    handle.seek(0, 2)
                    handle.seek(max(0, handle.tell()-12000))
                    state['log'] = handle.read().decode('utf-8', errors='replace')
            except OSError:
                state['log'] = ''
            return state

    # -- collections (F02) -------------------------------------------------
    #
    # A collection is the scope of a check: the public base, the migrated
    # legacy list and a personal list are different rows of the same table, so
    # choosing one cannot show the others.  Membership is the scope, and a
    # collection with no members yields nothing rather than falling back to
    # every address ever collected (defect 11).

    def collection_items(self, conn):
        """Every collection with the number of its own members.

        The read connection of the interface is a plain one without a row
        factory, so the columns are read by position.  The SQL is the one the
        schema uses: this is not a second definition of a collection.
        """
        items = []
        for identifier, name, kind, archived_at in conn.execute(
                'SELECT id, name, kind, archived_at FROM collections ORDER BY kind, name'):
            items.append(dict(id=identifier, name=name, kind=kind, archived=bool(archived_at),
                              members=len(self.collection_member_rows(conn, identifier))))
        items.sort(key=lambda item: (item['archived'], item['kind'], item['name']))
        return items

    @staticmethod
    def collection_member_rows(conn, collection_id):
        """``(endpoint_id, canonical, origin, added_at)`` of exactly one collection."""
        return conn.execute(
            'SELECT e.id, e.canonical, m.origin, m.added_at FROM membership m '
            'JOIN endpoints e ON e.id = m.endpoint_id WHERE m.collection_id = ? '
            'ORDER BY e.canonical', (collection_id,)).fetchall()

    @staticmethod
    def collection_row(conn, collection_id):
        """One collection as a plain row, or None."""
        return conn.execute('SELECT id, name, kind, archived_at FROM collections WHERE id = ?',
                            (collection_id,)).fetchone()

    def collections(self, query=None):
        """The list the interface needs to offer an explicit scope (F02)."""
        selected = str((query or {}).get('selected', [''])[0] or '').strip()
        conn = self.read_connection()
        if conn is None:
            return dict(collections=[], selected='', default_id=schema.PUBLIC_COLLECTION_ID)
        try:
            items = self.collection_items(conn)
            if selected and not any(item['id'] == selected for item in items):
                raise ValueError(f'Коллекция не найдена: {selected}')
        finally:
            conn.close()
        return dict(collections=items, selected=selected,
                    default_id=schema.PUBLIC_COLLECTION_ID)

    def require_collection(self, conn, collection_id):
        """One existing, not archived collection or an honest refusal."""
        wanted = str(collection_id or '').strip()
        if not wanted:
            wanted = schema.PUBLIC_COLLECTION_ID
        row = self.collection_row(conn, wanted)
        if row is None or row[3] is not None:
            raise ValueError(f'Коллекция не найдена: {wanted}')
        return wanted

    def collection_members(self, payload):
        """Members of one collection, with the collections each address is in."""
        payload = payload or {}
        conn = self.read_connection()
        if conn is None:
            raise ValueError('Локальная база ещё не создана. Сначала соберите адреса.')
        try:
            wanted = self.require_collection(conn, payload.get('collection'))
            rows = [{'proxy': canonical, 'origin': origin, 'added_at': added_at,
                     'collections': [row[0] for row in conn.execute(
                         'SELECT collection_id FROM membership WHERE endpoint_id = ? '
                         'ORDER BY collection_id', (endpoint,))]}
                    for endpoint, canonical, origin, added_at
                    in self.collection_member_rows(conn, wanted)]
        finally:
            conn.close()
        return dict(collection=wanted, members=rows, total=len(rows))

    def create_collection(self, payload):
        """Create a personal list. It starts empty and stays separate (defect 11)."""
        payload = payload or {}
        name = payload.get('name')
        if not isinstance(name, str) or not name.strip() or len(name) > 160:
            raise ValueError('Название коллекции: от 1 до 160 символов.')
        kind = payload.get('kind') or 'private'
        if kind not in schema.COLLECTION_KINDS:
            raise ValueError(f'Неизвестный вид коллекции: {kind}')
        with self.collection_write() as conn:
            try:
                identifier = schema.create_collection(conn, name, kind=kind)
                conn.commit()
            except schema.DbError as exc:
                raise ValueError(f'Не удалось создать коллекцию: {exc}') from None
        return dict(collection=identifier, name=name.strip(), kind=kind, members=0,
                    collections=self.collections().get('collections', []))

    def add_collection_members(self, payload):
        """Add addresses to one collection only, skipping repeats and bad lines."""
        payload = payload or {}
        raw = payload.get('proxies')
        if isinstance(raw, str):
            values = [line.strip() for line in raw.splitlines()]
        elif isinstance(raw, list):
            values = [str(line).strip() for line in raw]
        else:
            raise ValueError('Список адресов ожидается текстом или массивом.')
        # A paste is bounded, and the bound is reported: dropping addresses
        # without saying so is the silent parameter loss F07 forbids.
        values = [value for value in values if value]
        dropped = max(0, len(values) - MAX_COLLECTION_MEMBERS)
        values = values[:MAX_COLLECTION_MEMBERS]
        with self.collection_write() as conn:
            try:
                wanted = self.require_collection(conn, payload.get('collection'))
                added, rejected, known = 0, [], 0
                for value in values:
                    # A user list may name a private or hostname endpoint, the
                    # same rule the own-list field follows (validate()).
                    canonical = core.normalize_custom(value)
                    if canonical is None:
                        rejected.append(value[:120])
                        continue
                    endpoint = schema.upsert_endpoint(conn, canonical)
                    member = conn.execute(
                        'SELECT 1 FROM membership WHERE collection_id=? AND endpoint_id=?',
                        (wanted, endpoint)).fetchone()
                    if member is not None:
                        known += 1
                        continue
                    schema.add_member(conn, wanted, endpoint, origin='manual')
                    added += 1
                conn.commit()
            except (schema.DbError, sqlite3.Error) as exc:
                raise ValueError(f'Не удалось изменить коллекцию: {exc}') from None
        return dict(collection=wanted, added=added, already=known, rejected=rejected[:20],
                    rejected_total=len(rejected), dropped=dropped,
                    members=self.collection_members({'collection': wanted}))

    def remove_collection_member(self, payload):
        """Drop one address from one collection; every other list keeps it (F02)."""
        payload = payload or {}
        value = payload.get('proxy')
        if not isinstance(value, str) or not value.strip():
            raise ValueError('Укажите адрес прокси.')
        canonical = core.normalize_custom(value)
        if canonical is None:
            raise ValueError('Нужен адрес прокси в виде host:port или protocol://host:port.')
        with self.collection_write() as conn:
            try:
                wanted = self.require_collection(conn, payload.get('collection'))
                removed = schema.remove_member(conn, wanted, schema.endpoint_id(canonical))
                conn.commit()
            except (schema.DbError, sqlite3.Error) as exc:
                raise ValueError(f'Не удалось изменить коллекцию: {exc}') from None
        if not removed:
            raise ValueError(f'Адреса {canonical} нет в коллекции {wanted}.')
        return dict(collection=wanted, removed=1, proxy=canonical,
                    members=self.collection_members({'collection': wanted}))

    def writable_connection(self):
        """A writing connection under the data lock; never used by a read path.

        The file is created through the one migrator the worker uses, so a
        personal list can be prepared before the first collection instead of
        being refused for a database that does not exist yet.
        """
        if self.running():
            raise ValueError('Проверка идёт и держит базу. Дождитесь окончания или остановите её.')
        try:
            conn, _report = schema.open_db(self.data/'proxies.sqlite3',
                                           app_version=PRODUCT_VERSION)
        except schema.DbError as exc:
            raise ValueError(f'Не удалось открыть локальную базу: {exc}') from None
        return closing(conn)

    @contextmanager
    def collection_write(self):
        """Membership writes take the same lock a check takes, never a side door."""
        with self.data_lock():
            with self.writable_connection() as conn:
                yield conn

    def collection_scope(self, plan):
        """Addresses of the collection this plan is about, or None for all."""
        wanted = plan.get('collection')
        if not wanted:
            return None
        conn = self.read_connection()
        if conn is None:
            return frozenset()
        try:
            return frozenset(row[1] for row in self.collection_member_rows(conn, wanted))
        finally:
            conn.close()

    # -- service catalog (F06) ---------------------------------------------
    #
    # The catalog is a shipped manifest with an id, a version, a maintainer and
    # a real pass condition per service.  A set is a combination of services
    # with a rule, and applying one writes a complete field inventory, so no
    # judge or threshold of the previous set survives the switch (defect 24).

    def catalog(self, query=None):
        """Categories, services and sets, with search and multi-selection."""
        query = query or {}
        one = lambda key, default='': str((query.get(key) or [default])[0] or default)
        try:
            catalog = self.service_catalog()
            found = servicecatalog.search_presets(
                catalog, one('q')[:100], one('category') or None, one('capability') or None)
        except servicecatalog.CatalogError as exc:
            raise ValueError(str(exc)) from None
        presets = [self.preset_view(catalog, preset) for preset in found]
        stored = self.service_sets.read()
        user_sets = stored.get('sets') or {}
        pinned = stored.get('pinned') or None
        sets = []
        for item in catalog.service_sets:
            try:
                snapshot = catalog.resolve(item.set_id).to_dict()
            except servicecatalog.CatalogError:
                continue
            sets.append(self.pinned_view(snapshot, applied=bool(pinned and pinned.get('set_id') == item.set_id)))
        for set_id, snapshot in sorted(user_sets.items()):
            sets.append(self.pinned_view(snapshot, applied=bool(pinned and pinned.get('set_id') == set_id)))
        selected = [key for key in str(one('selected') or '').split(',') if key]
        # The cost line is a preview: a service the catalog no longer lists is
        # left out of it instead of turning the whole catalog view into an error.
        known = [key for key in selected if key in catalog.preset_index]
        return dict(catalog=catalog.summary(),
                    presets=presets, total=len(presets), sets=sets,
                    pinned_id=(pinned or {}).get('set_id'),
                    pinned_digest=(pinned or {}).get('set_digest'),
                    selected=known,
                    combinations=list(servicecatalog.COMBINATION_IDS),
                    cost=servicecatalog.estimated_cost(
                        servicecatalog.select_presets(catalog, known)) if known else None)

    def service_catalog(self, path=None):
        """The validated manifest, loaded once per process: it cannot change."""
        cached = self._service_catalog
        if cached is None:
            cached = self._service_catalog = servicecatalog.load_catalog(path)
        return cached

    @staticmethod
    def preset_view(catalog, preset):
        """One service as the interface shows it: what it proves and what it does not."""
        payload = preset.to_dict()
        payload['capability_title'] = catalog.capability(preset.capability).title_ru
        payload['probes'] = [dict(probe.to_dict(), pass_condition=probe.pass_condition_ru())
                             for probe in preset.probes]
        payload['in_sets'] = [item.set_id for item in catalog.service_sets
                              if preset.preset_id in item.preset_ids]
        return payload

    @staticmethod
    def pinned_view(snapshot, *, applied=False):
        """A user set or a catalog set in one shape, ready for the interface.

        ``id``/``title``/``digest`` are the same three names the catalog uses,
        so a saved set and a shipped set render through one code path.
        """
        presets = snapshot.get('presets')
        if isinstance(presets, dict):
            probes = sum(len((value or {}).get('probes') or []) for value in presets.values())
        else:
            probes = sum(len((value or {}).get('probes') or []) for value in (presets or []))
        return dict(id=snapshot.get('set_id'), title=snapshot.get('title_ru'),
                    description=snapshot.get('description_ru') or '',
                    version=snapshot.get('set_version'),
                    digest=snapshot.get('set_digest'), origin=snapshot.get('origin') or 'catalog',
                    required=list(snapshot.get('required') or []),
                    optional=list(snapshot.get('optional') or []),
                    combination=snapshot.get('combination'),
                    min_passes=snapshot.get('min_passes') or 0,
                    probes=probes, applied=applied)

    def pinned_set(self):
        """The snapshot the current form was built from, if it is still readable."""
        stored = self.service_sets.read()
        pinned = stored.get('pinned')
        if not isinstance(pinned, dict):
            return None
        try:
            return servicecatalog.PinnedSet.from_dict(pinned)
        except (servicecatalog.CatalogError, TypeError, ValueError):
            return None

    def service_set_detail(self, payload):
        """One set with its services, their probes, and the update diff."""
        payload = payload or {}
        set_id = str(payload.get('set') or '').strip()
        if not set_id:
            raise ValueError('Укажите набор сервисов.')
        pinned = None
        for snapshot in (self.service_sets.read().get('sets') or {}).values():
            if isinstance(snapshot, dict) and snapshot.get('set_id') == set_id:
                try:
                    pinned = servicecatalog.PinnedSet.from_dict(snapshot)
                except (servicecatalog.CatalogError, TypeError, ValueError):
                    pinned = None
                break
        if pinned is None:
            try:
                pinned = self.service_catalog().resolve(set_id)
            except servicecatalog.CatalogError as exc:
                raise ValueError(str(exc)) from None
        detail = dict(pinned.summary(), set=self.pinned_view(pinned.to_dict()),
                      applied=bool((self.service_sets.read().get('pinned') or {}).get('set_id') == set_id),
                      presets=[self.preset_view(self.service_catalog(), preset)
                               for preset in pinned.ordered_presets()])
        try:
            preview = servicecatalog.preview_update(self.service_catalog(), pinned)
            detail['update'] = preview.to_dict()
        except servicecatalog.CatalogError as exc:
            detail['update'] = {'error': str(exc), 'changes': []}
        return detail

    def apply_service_set(self, payload):
        """Apply a set: its complete field inventory plus the targets it measures."""
        payload = payload or {}
        set_id = str(payload.get('set') or '').strip()
        if not set_id:
            raise ValueError('Укажите набор сервисов.')
        catalog = self.service_catalog()
        try:
            pinned = self.resolve_set(catalog, set_id, payload)
            targets = pinned.targets()
            applied = servicecatalog.apply_scenario(self.settings(), pinned.scenario,
                                                    catalog.user_fields(), targets)
        except servicecatalog.CatalogError as exc:
            raise ValueError(str(exc)) from None
        settings = validate(applied.settings)
        with self.data_lock():
            stored = self.service_sets.read()
            stored['pinned'] = pinned.to_dict()
            self.service_sets.write(stored)
        return dict(settings=settings, report=applied.report(), set=self.pinned_view(pinned.to_dict()),
                    cost=servicecatalog.estimated_cost(pinned.ordered_presets()))

    def resolve_set(self, catalog, set_id, payload):
        """A catalog set, a saved set, or a multi-selection the user named."""
        stored = self.service_sets.read()
        snapshot = (stored.get('sets') or {}).get(set_id)
        if isinstance(snapshot, dict):
            try:
                return servicecatalog.PinnedSet.from_dict(snapshot)
            except (servicecatalog.CatalogError, TypeError, ValueError):
                pass
        try:
            return catalog.resolve(set_id)
        except servicecatalog.CatalogError:
            pass
        # A selection that has no name yet is a set the user is building here.
        wanted = payload.get('preset_ids') or []
        return servicecatalog.new_user_set(
            catalog, set_id, str(payload.get('title') or set_id),
            str(payload.get('title') or set_id), wanted,
            combination=str(payload.get('combination') or 'any'),
            min_passes=int(payload.get('min_passes') or 1),
            required_ids=payload.get('required_ids'))

    def save_service_set(self, payload):
        """Save the current selection as the user's own set (F06)."""
        payload = payload or {}
        set_id = str(payload.get('id') or '').strip()
        title = str(payload.get('title') or '').strip()
        if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,63}', set_id or ''):
            raise ValueError('Идентификатор набора: строчные латинские буквы, цифры, точка и дефис.')
        if not title or len(title) > 120:
            raise ValueError('Название набора: от 1 до 120 символов.')
        catalog = self.service_catalog()
        try:
            pinned = servicecatalog.new_user_set(
                catalog, set_id, title, str(payload.get('title_en') or title),
                payload.get('preset_ids') or [],
                combination=str(payload.get('combination') or 'any'),
                min_passes=int(payload.get('min_passes') or 1),
                required_ids=payload.get('required_ids'),
                description_ru=str(payload.get('description') or ''))
        except (servicecatalog.CatalogError, TypeError, ValueError) as exc:
            raise ValueError(str(exc) if isinstance(exc, servicecatalog.CatalogError)
                             else 'Неверные параметры набора.') from None
        with self.data_lock():
            stored = self.service_sets.read()
            saved = dict(stored.get('sets') or {})
            if set_id not in saved and len(saved) >= SERVICE_SET_LIMIT:
                raise ValueError(f'Уже сохранено {SERVICE_SET_LIMIT} наборов. Удалите лишние.')
            saved[set_id] = pinned.to_dict()
            self.service_sets.write(dict(stored, sets=saved))
        return dict(set=self.pinned_view(pinned.to_dict()), saved=len(saved))

    def stored_set(self, set_id):
        """The user's saved snapshot of one set, or None."""
        snapshot = (self.service_sets.read().get('sets') or {}).get(set_id)
        if not isinstance(snapshot, dict):
            return None
        try:
            return servicecatalog.PinnedSet.from_dict(snapshot)
        except (servicecatalog.CatalogError, TypeError, ValueError):
            raise ValueError(f'Набор {set_id} повреждён; сохраните его заново.') from None

    def update_service_set(self, payload):
        """Show the diff first, then apply it: a definition never changes silently."""
        payload = payload or {}
        set_id = str(payload.get('set') or '').strip()
        pinned = self.stored_set(set_id) if set_id else None
        if payload.get('apply'):
            pinned = pinned or self.pinned_set()
            if pinned is None:
                raise ValueError('Нет сохранённого набора для обновления.')
            if not set_id:
                set_id = pinned.set_id
            try:
                catalog = self.service_catalog()
                preview = servicecatalog.preview_update(catalog, pinned)
                upgraded = servicecatalog.upgrade_set(catalog, preview)
            except servicecatalog.CatalogError as exc:
                raise ValueError(str(exc)) from None
            if set_id != upgraded.set_id:
                raise ValueError(f'Обновление меняет набор на {upgraded.set_id}, а выбран {set_id}.')
            with self.data_lock():
                stored = self.service_sets.read()
                saved = dict(stored.get('sets') or {})
                saved[set_id] = upgraded.to_dict()
                self.service_sets.write(dict(stored, sets=saved, pinned=upgraded.to_dict()))
            return dict(set=self.pinned_view(upgraded.to_dict()),
                        changes=[change.to_dict() for change in preview.changes],
                        applied=len([change for change in preview.changes
                                     if change.state != 'unchanged']))
        if pinned is None:
            pinned = self.pinned_set()
        if pinned is None and set_id:
            # A shipped set that was never saved is diffed against itself, so
            # the answer is an honest "already current" instead of a refusal.
            try:
                pinned = self.service_catalog().resolve(set_id)
            except servicecatalog.CatalogError as exc:
                raise ValueError(str(exc)) from None
        if pinned is None:
            raise ValueError('Сначала примените набор: обновлять нечего.')
        try:
            preview = servicecatalog.preview_update(self.service_catalog(), pinned)
        except servicecatalog.CatalogError as exc:
            raise ValueError(str(exc)) from None
        return dict(set_id=preview.set_id, update=preview.to_dict(),
                    changes=[change.to_dict() for change in preview.changes],
                    applied=len([change for change in preview.changes
                                 if change.state != 'unchanged']))

    def delete_service_set(self, payload):
        """Forget one of the user's own sets; the catalog is never touched."""
        set_id = str((payload or {}).get('set') or '').strip()
        with self.data_lock():
            stored = self.service_sets.read()
            saved = dict(stored.get('sets') or {})
            if set_id not in saved:
                raise ValueError(f'Сохранённого набора {set_id} нет.')
            saved.pop(set_id)
            pinned = stored.get('pinned')
            self.service_sets.write(dict(stored, sets=saved,
                                         pinned=None if (pinned or {}).get('set_id') == set_id else pinned))
        return dict(set=set_id, saved=len(saved))

    # -- result scope -----------------------------------------------------
    #
    # The table, the row details, the matrix and every bulk action resolve the
    # same plan first.  A plan is derived from one pinned generation and one
    # profile, it carries a digest, and nothing downstream re-reads the pointer
    # (defect 5: no writer lock; F19: selection cannot silently move to another
    # scope).

    @staticmethod
    def query_of(raw):
        """Accept both a query string and the JSON form the page posts."""
        if not isinstance(raw, dict):
            raise ValueError('Некорректные условия выборки.')
        out = {}
        for key, value in raw.items():
            if isinstance(value, (list, tuple)):
                out[key] = [str(item) for item in value]
            elif value is None:
                out[key] = ['']
            else:
                out[key] = [str(value)]
        return out

    def parse_results_query(self, query):
        """Validate one set of table controls.  Unknown values are never ignored."""
        sort = query.get('sort', ['quality'])[0]
        order = RESULT_ORDERS.get(sort)
        view = query.get('view', [FRESHNESS_FRESH])[0]
        try:
            threshold = float(query.get('min_success', [2/3])[0])
            offset = max(0, int(query.get('offset', ['0'])[0]))
            limit = int(query.get('limit', [PAGE_SIZE])[0])
            min_anonymity = anonymity.validate_min_level(query.get('min_anonymity', ['any'])[0])
            protocol = query.get('protocol', ['all'])[0]
            quick = query.get('quick', [''])[0]
            max_latency = float(query.get('max_latency', ['0'])[0])
            search = query.get('q', [''])[0].strip().lower()[:100]
            countries = frozenset(geoip.parse_countries(query.get('country', [''])[0][:1000]))
            # F08 parity: the same three knobs the CLI and the API already
            # accept.  Their defaults are what this page has always done --
            # the endpoint's own country, an unknown one excluded -- so a link
            # without them selects the same rows it always did.
            country_basis = (query.get('country_basis', ['endpoint'])[0] or 'endpoint').strip()
            country_unknown = (query.get('country_unknown', ['exclude'])[0] or 'exclude').strip()
            hide_hosting = normalize_hosting_filter(query.get('hosting', [''])[0]) == 'hide'
            # F02: the table can be about one collection.  An empty value is
            # every collection the profile measured, which is what the scope
            # was before collections were selectable here.
            collection = query.get('collection', [''])[0].strip()[:128]
        except ValueError:
            raise ValueError('Неверные параметры рейтинга.') from None
        if (not 0 <= threshold <= 1 or order is None or protocol not in core.PROTOCOLS
                or view not in RESULT_VIEWS
                or quick not in ('', 'clean', 'speed', 'http')
                or not math.isfinite(max_latency) or max_latency < 0):
            raise ValueError('Неверные параметры рейтинга.')
        # Named separately so the message says which control was wrong; a
        # filter that silently kept the default would be a filter the user
        # cannot steer.
        if country_basis not in COUNTRY_BASES:
            raise ValueError('country_basis: ' + ', '.join(COUNTRY_BASES) + '.')
        if country_unknown not in COUNTRY_UNKNOWNS:
            raise ValueError('country_unknown: ' + ', '.join(COUNTRY_UNKNOWNS) + '.')
        limit = max(1, min(limit, PAGE_SIZE_MAX))
        return dict(sort=sort, order=order, view=view, min_success=threshold, offset=offset, limit=limit,
                    min_anonymity=min_anonymity, protocol=protocol, quick=quick, max_latency=max_latency,
                    search=search, countries=countries, hide_hosting=hide_hosting, collection=collection,
                    country_basis=country_basis, country_unknown=country_unknown)

    def result_plan(self, query):
        """One validated description of "which rows the table is about"."""
        parsed = self.parse_results_query(query)
        # A collection that does not exist is refused, not answered with an
        # empty table: an empty table would read as "nothing passed here".
        if parsed['collection']:
            self.collections({'selected': [parsed['collection']]})
        snapshot = self.snapshot()
        status = self.export_status(snapshot)
        published = bool(status.get('published')) and status.get('state') != 'error'
        profile = status.get('profile') if published else None
        visible = None
        if published:
            visible = {row['proxy']: row for row in snapshot.rows if isinstance(row, dict) and row.get('proxy')}
        if not profile:
            # A legacy database stays readable until the first coherent export
            # exists; a broken publication must never reach arbitrary rows.
            if snapshot.state == SNAPSHOT_BROKEN:
                profile = None
            else:
                profile_path = self.data/'last-profile.txt'
                try:
                    profile = profile_path.read_text(encoding='utf-8').strip() or None
                except (OSError, UnicodeError):
                    profile = None
        digest = digest_of('results-v1', profile, snapshot.generation, parsed['view'], parsed['sort'],
                           round(parsed['min_success'], 6), parsed['min_anonymity'], parsed['protocol'],
                           parsed['quick'], int(parsed['max_latency'] or 0), parsed['search'],
                           sorted(parsed['countries']), parsed['hide_hosting'], published,
                           parsed['collection'])
        parsed.update(profile=profile, generation=snapshot.generation, snapshot=snapshot, status=status,
                      published=published, visible=visible, digest=digest,
                      measured_collection=(status.get('collection_id') or ''),
                      available=bool((self.data/'proxies.sqlite3').is_file()))
        return parsed

    def provider_resolver(self):
        return core.provider_resolver(self.asn())

    def result_policy(self, plan, cfg, *, denylist):
        """The admission policy of this read, assembled in one place (F18)."""
        reputation = cfg.get('reputation', {}) if isinstance(cfg, dict) else {}
        strict = bool(reputation.get('strict', False))
        if not (cfg or {}).get('anonymity'):
            plan = dict(plan, min_anonymity='any')
        max_age = self.status_max_age(plan['status']) if plan.get('status') else admission.DEFAULT_MAX_AGE_SECONDS
        # The country half is the ``geo.CountryCriterion`` the export already
        # applies, not the legacy ``countries=`` shortcut.  The shortcut is an
        # include-only set that cannot say "not this country", cannot compare an
        # exit country and drops an unknown one without saying so -- so the
        # table and an export of the same rows could disagree about one filter.
        # Behaviour is unchanged for the picker the page has: an include list
        # with ``unknown='exclude'`` keeps exactly what it kept before.
        return admission.Policy(max_age_seconds=max_age, min_success=max(plan['min_success'], 1e-9),
                                min_anonymity=plan['min_anonymity'], strict=strict,
                                protocol=plan['protocol'],
                                country_criterion=core.country_criterion(
                                    sorted(plan['countries']), basis=plan.get('country_basis', 'endpoint'),
                                    unknown=plan.get('country_unknown', 'exclude')),
                                countries=plan['countries'] if plan['countries'] else frozenset(),
                                exclude_hosting=plan['hide_hosting'],
                                denied=frozenset(denylist.proxies) if denylist else frozenset(),
                                max_latency_ms=plan['max_latency'] or None,
                                allow_missing_identity=True,
                                deny_match=(denylist.match if denylist else None),
                                protocol_of=core.proxy_protocol,
                                country_of=(self.geo().country_of if self.geo() else None),
                                hosting_of=(lambda proxy: core.is_hosting(proxy, self.provider_resolver())))

    def status_max_age(self, status):
        try:
            value = float((status or {}).get('max_age_seconds'))
        except (TypeError, ValueError):
            return admission.DEFAULT_MAX_AGE_SECONDS
        return value if math.isfinite(value) and value > 0 else admission.DEFAULT_MAX_AGE_SECONDS

    def read_connection(self):
        """A read-only SQLite snapshot: WAL readers never need the writer lock."""
        path = self.data/'proxies.sqlite3'
        if not path.is_file():
            return None
        conn = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=2)
        try:
            conn.execute('BEGIN')
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    def read_rows_connection(self):
        """The same snapshot, but with rows as mappings.

        `db.scope_exclusions` and the other storage readers build `dict(row)`,
        which needs `sqlite3.Row`; `read_connection` hands out tuples for the
        readers that index by position, so the two cannot be one method.
        """
        conn = self.read_connection()
        if conn is not None:
            conn.row_factory = sqlite3.Row
        return conn

    def profile_config(self, conn, profile):
        if not profile or conn is None:
            return {}
        try:
            record = conn.execute('SELECT config FROM profiles WHERE id=?', (profile,)).fetchone()
        except sqlite3.Error:
            return {}
        if not record:
            return {}
        try:
            cfg = json.loads(record[0])
        except (TypeError, ValueError):
            return {}
        return cfg if isinstance(cfg, dict) else {}

    def scoped_rows(self, plan, *, limit=None):
        """Every row of the plan scope with its admission decision, in table order."""
        if not plan.get('profile') or not plan.get('available'):
            return [], 0, 0, {}
        conn = self.read_connection()
        if conn is None:
            return [], 0, 0, {}
        try:
            cfg = self.profile_config(conn, plan['profile'])
            denylist = Denylist.from_file(self.data/'denylist.txt', normalizer=core.normalize)
            if denylist.error:
                raise ValueError('Не удалось прочитать локальный denylist; обновите список.')
            policy = self.result_policy(plan, cfg, denylist=denylist)
            source_keys = self.source_keys(conn)
            provider_of = self.provider_resolver()
            now = time.time()
            entries = self.annotations.read().get('entries') or {}
            matched = []
            seen = 0
            condition = "profile=? AND json_extract(payload,'$.min_target_reliability')>0"
            if plan['view'] == FRESHNESS_FRESH:
                condition += " AND json_extract(payload,'$.min_target_reliability')+1e-12>=?"
                params = (plan['profile'], plan['min_success'])
            else:
                # Failed, expired, unknown and "all" must not be pre-filtered by
                # reliability: a failed measurement is exactly the row the user
                # asked to see.
                condition = "profile=?"
                params = (plan['profile'],)
            recommended = None
            if plan['sort'] == 'recommended':
                recommended = core.recommender((plan.get('status') or {}).get('source_quality'),
                                               core.listed_counts(conn), source_keys, plan['min_success'])
            if plan['quick'] == 'clean':
                pass
            members = self.collection_scope(plan)
            # A scope exclusion removes an address from the user's own scope
            # (source-system-design §12.3).  It is applied here, in the one
            # place every read of the results table and of the export passes
            # through, so it is a real exclusion and not a flag in a list.
            excluded = frozenset(str(item.get('proxy')) for item in self.scope_exclusion_rows())
            for (payload,) in conn.execute('SELECT payload FROM results WHERE '+condition+' ORDER BY '+plan['order'], params):
                try:
                    row = json.loads(payload)
                except (TypeError, ValueError):
                    continue
                proxy = row.get('proxy', '')
                if plan['visible'] is not None and proxy not in plan['visible']:
                    continue
                if members is not None and proxy not in members:
                    # F02: another collection's rows are not this table's rows.
                    continue
                if proxy in excluded:
                    continue
                verdict = admission.admit(row, self.snapshot_scope(plan.get('status') or {}),
                                          self.snapshot_access(), policy, now,
                                          published_at=(plan.get('status') or {}).get('generated_at'))
                bucket = freshness_of(verdict, row)
                if plan['view'] != 'all' and bucket != plan['view']:
                    continue
                if plan['quick'] == 'clean' and core.reputation_status(row) != 'clean':
                    continue
                speed = row.get('speed') if isinstance(row.get('speed'), dict) else {}
                speed_mbps = speed.get('mbps', row.get('mbps'))
                if plan['quick'] == 'speed' and not (speed_mbps is not None and speed_mbps > 0):
                    continue
                if plan['quick'] == 'http' and core.proxy_protocol(proxy) not in ('http', 'https'):
                    continue
                if not self.matches_filters(plan, row, verdict):
                    continue
                if plan['search'] and plan['search'] not in proxy.lower():
                    continue
                seen += 1
                summary = dict(row)
                summary.pop('samples', None)
                summary.update(verdict.as_dict())
                summary['freshness'] = bucket
                summary['country'] = core.row_country(row, self.geo().country_of if self.geo() else None)
                summary['exit_country'] = core.exit_country(row, self.geo().country_of if self.geo() else None)
                summary['source_keys'] = list(source_keys.get(proxy, ()))
                summary['provider'] = provider_of(proxy) if provider_of else None
                if recommended is not None:
                    summary['recommended'] = recommended(row)
                annotation = entries.get(proxy) or {}
                summary['tags'] = list(annotation.get('tags') or [])
                summary['favorite'] = bool(annotation.get('favorite'))
                summary['note'] = annotation.get('note') or ''
                matched.append((summary, verdict))
            if plan['sort'] == 'recommended':
                matched.sort(key=lambda item: (-(item[0].get('recommended') or 0.0), item[0].get('proxy') or ''))
            if limit is not None:
                matched = matched[:limit]
            return [item[0] for item in matched], seen, len(matched), cfg
        finally:
            conn.close()

    def matches_filters(self, plan, row, verdict):
        """Country and free-text filters that admission cannot decide alone."""
        if plan['countries']:
            country = core.row_country(row, self.geo().country_of if self.geo() else None)
            if country is None or country not in plan['countries']:
                return False
        return True

    def source_keys(self, conn):
        try:
            return core.source_map(conn)
        except sqlite3.Error:
            return {}

    def results(self, query):
        """One page of the current scope, with a digest the client must reuse."""
        plan = self.result_plan(query)
        scope = dict(collection=plan['collection'], measured_collection=plan.get('measured_collection') or '')
        if not plan['profile'] or not plan['available']:
            return dict(rows=[], total=0, targets=[], profile=plan['profile'], offset=plan['offset'],
                        limit=plan['limit'], view=plan['view'], scope_digest=plan['digest'],
                        generation=plan['generation'], published=plan['published'], **scope,
                        state=(plan.get('status') or {}).get('state'),
                        state_detail=(plan.get('status') or {}).get('state_detail'),
                        state_detail_label=state_detail_text((plan.get('status') or {}).get('state_detail')),
                        counts={}, columns=list(COMPACT_COLUMNS), snapshot_state=plan['snapshot'].state)
        rows, total, _, cfg = self.scoped_rows(plan)
        window = rows[plan['offset']:plan['offset'] + plan['limit']]
        counts = self.view_counts(plan)
        targets = [dict(name=item.get('name', ''), url=core.public_url(item['url']))
                   for item in cfg.get('targets', []) if isinstance(item, dict) and item.get('url')]
        return dict(rows=window, total=total, targets=targets, profile=plan['profile'],
                    offset=plan['offset'], limit=plan['limit'], view=plan['view'],
                    scope_digest=plan['digest'], generation=plan['generation'],
                    published=plan['published'], snapshot_state=plan['snapshot'].state, **scope,
                    state=(plan.get('status') or {}).get('state'),
                    state_detail=(plan.get('status') or {}).get('state_detail'),
                    state_detail_label=state_detail_text((plan.get('status') or {}).get('state_detail')),
                    counts=counts, columns=list(COMPACT_COLUMNS),
                    request_profile=cfg.get('request_profile', 'workbench'),
                    reputation_policy=cfg.get('reputation', {}), anonymity=bool(cfg.get('anonymity')),
                    stale=False, favorites_only=False)

    def view_counts(self, plan):
        """How many rows each freshness view holds; drives the view tabs."""
        counts = {name: 0 for name in RESULT_VIEWS}
        for bucket in self.iter_buckets(plan):
            counts[bucket] = counts.get(bucket, 0) + 1
        counts['all'] = sum(counts[name] for name in RESULT_VIEWS if name != 'all')
        return counts

    def iter_buckets(self, plan):
        """Freshness bucket of every row in scope, ignoring the active view."""
        probe = dict(plan, view='all', offset=0, limit=PAGE_SIZE_MAX)
        rows, _, _, _ = self.scoped_rows(probe, limit=None)
        return (row.get('freshness') for row in rows)

    def matrix(self, query):
        """proxy x target for the visible page: which target answered, and how."""
        plan = self.result_plan(query)
        conn = self.read_connection()
        rows = []
        targets = []
        if conn is not None and plan.get('profile'):
            try:
                cfg = self.profile_config(conn, plan['profile'])
                targets = [dict(name=item.get('name', ''), url=core.public_url(item['url']))
                           for item in cfg.get('targets', []) if isinstance(item, dict) and item.get('url')]
                page, _, _, _ = self.scoped_rows(plan)
                names = {row.get('proxy') for row in page}
                if names:
                    marks = ','.join('?' * len(names))
                    for proxy, payload in conn.execute(
                            f'SELECT proxy, payload FROM results WHERE profile=? AND proxy IN ({marks})',
                            (plan['profile'], *sorted(names))):
                        try:
                            body = json.loads(payload)
                        except (TypeError, ValueError):
                            continue
                        cells = []
                        for sample in (body.get('samples') or []):
                            if not isinstance(sample, dict):
                                continue
                            cells.append(dict(target=sample.get('target'), attempt=sample.get('attempt'),
                                              ok=bool(sample.get('ok')), status=sample.get('status'),
                                              ms=sample.get('elapsed_ms', sample.get('latency_ms')),
                                              error=sample.get('error')))
                        rows.append(dict(proxy=proxy, cells=cells,
                                         error=body.get('error'), reliability=body.get('min_target_reliability')))
            except sqlite3.Error:
                rows = []
            finally:
                conn.close()
        return dict(rows=rows, targets=targets, generation=plan['generation'],
                    scope_digest=plan['digest'], profile=plan['profile'])

    def detail(self, proxy):
        """Full row with its samples.  Read-only, lock-free, and honest about age.

        Defect 5: the details pane must open during a scan or a watch, so no
        exclusive data lock is taken.  Defect 3: an expired row is described
        with its reason instead of being reported as "not found".
        """
        if not isinstance(proxy, str) or not proxy or len(proxy) > 512:
            raise ValueError('Некорректный адрес прокси.')
        plan = self.result_plan({})
        if plan['published'] and plan['visible'] is not None and proxy not in plan['visible']:
            raise ValueError('Детали прокси не найдены в текущем снимке.')
        if not plan['profile'] or not plan['available']:
            raise ValueError('Результаты не найдены.')
        conn = self.read_connection()
        if conn is None:
            raise ValueError('Результаты не найдены.')
        try:
            record = conn.execute('SELECT payload FROM results WHERE profile=? AND proxy=?',
                                  (plan['profile'], proxy)).fetchone()
        except sqlite3.Error:
            raise ValueError('Не удалось прочитать результаты.') from None
        finally:
            conn.close()
        if not record:
            raise ValueError('Детали прокси не найдены.')
        try:
            row = json.loads(record[0])
        except (TypeError, ValueError):
            raise ValueError('Сохранённый результат повреждён.') from None
        cfg = {}
        conn = self.read_connection()
        try:
            cfg = self.profile_config(conn, plan['profile'])
        finally:
            if conn is not None:
                conn.close()
        denylist = Denylist.from_file(self.data/'denylist.txt', normalizer=core.normalize)
        policy = self.result_policy(plan, cfg, denylist=denylist)
        verdict = admission.admit(row, self.snapshot_scope(plan.get('status') or {}),
                                  self.snapshot_access(), policy, time.time(),
                                  published_at=(plan.get('status') or {}).get('generated_at'))
        annotation = (self.annotations.read().get('entries') or {}).get(proxy) or {}
        payload = dict(row)
        payload.update(verdict.as_dict())
        payload['freshness'] = freshness_of(verdict, row)
        payload['tags'] = list(annotation.get('tags') or [])
        payload['favorite'] = bool(annotation.get('favorite'))
        payload['note'] = annotation.get('note') or ''
        payload['generation'] = plan['generation']
        return payload

    def test_proxy(self, payload):
        """A diagnostic row test that is signed with the volume it really used.

        Defect 23 and R18: the quick test must not bypass the shared policy, must
        not run forever, and must say what it did and did not measure.  It never
        replaces the stored observation, so a refresh of the row is offered as a
        separate action instead of being implied by this answer.
        """
        if not isinstance(payload, dict):
            raise ValueError('Ожидается JSON объект.')
        proxy = core.normalize(str(payload.get('proxy') or '').strip())
        if proxy is None:
            raise ValueError('Укажите публичный IP-адрес и порт прокси без credentials.')
        try:
            threshold = float(payload.get('min_success', 2/3))
        except (TypeError, ValueError):
            raise ValueError('Некорректный порог успешности.') from None
        if not 0 <= threshold <= 1:
            raise ValueError('Некорректный порог успешности.')

        plan = self.result_plan({'min_success': [str(threshold)]})
        profile = plan['profile']
        if not profile or not plan['available']:
            raise ValueError('Сначала выполните проверку: быстрый тест использует её targets.')
        conn = self.read_connection()
        try:
            profile_config = self.profile_config(conn, profile)
        finally:
            if conn is not None:
                conn.close()
        targets = profile_config.get('targets') if isinstance(profile_config, dict) else None
        if not isinstance(targets, list) or not targets:
            raise ValueError('В активном профиле нет targets для быстрого теста.')

        config = dict(profile_config)
        # A row test verifies service availability, not a second anonymity/speed
        # campaign. Keep the configured targets and threshold, but cap the
        # auxiliary request budget so one click cannot hang the GUI.
        config['anonymity'] = None
        config['speedtest'] = None
        config['timeout'] = min(max(float(config.get('timeout', 5)), .1), QUICK_TEST_TIMEOUT_CAP_S)
        config['max_bytes'] = min(max(int(config.get('max_bytes', 1024*1024)), 1024), QUICK_TEST_MAX_BYTES)
        attempts = max(1, int(config.get('attempts', 1) or 1))
        target_names = [str(item.get('name') or core.public_url(item.get('url', ''))) for item in targets
                        if isinstance(item, dict)]
        denylist = Denylist.from_file(self.data/'denylist.txt', normalizer=core.normalize)
        required_anonymity = str(profile_config.get('min_anonymity') or 'any')
        reputation = profile_config.get('reputation') or {}
        strict = bool(reputation.get('strict', False))
        denied_by = denylist.match(proxy) if not denylist.error else None
        # The volume this click can cost, stated before it is spent.
        planned_requests = attempts * max(1, len(target_names))
        deadline_s = QUICK_TEST_DEADLINE_S
        scope = dict(kind='quick_diagnostic', stored=False, targets=len(target_names),
                     attempts=attempts, planned_requests=planned_requests,
                     max_request_bytes=config['max_bytes'],
                     request_timeout_s=config['timeout'],
                     whole_deadline_s=deadline_s,
                     skipped=['reputation_pipeline', 'anonymity_judge', 'bandwidth'],
                     policy=dict(min_success=threshold, min_anonymity=required_anonymity,
                                 strict=strict, local_denylist=True),
                     recheck_action='recheck',
                     recheck_note=tr('Полная перепроверка обновит строку результата и её свежесть.',
                                     'A full re-check updates the stored row and its freshness.'))
        if denied_by:
            return dict(ok=False, latency_ms=None, status=None, error='E_SCOPE_DENYLIST',
                        targets=target_names, profile=profile, scope=scope, timed_out=False,
                        admission='E_SCOPE_DENYLIST', stored=False)

        async def _test():
            started = time.monotonic()
            try:
                row = await asyncio.wait_for(core.check_proxy(proxy, config, core.Rate(0)), deadline_s)
            except asyncio.TimeoutError:
                return dict(ok=False, latency_ms=None, status=None, error='E_LIMIT_BUDGET',
                            targets=target_names, profile=profile, timed_out=True,
                            elapsed_s=round(time.monotonic() - started, 3), requests_made=planned_requests)
            errors = [sample.get('error') for sample in row.get('samples', [])
                      if isinstance(sample, dict) and sample.get('error')]
            made = len(row.get('samples') or [])
            reliability = row.get('min_target_reliability')
            ok = bool(reliability is not None and reliability + 1e-12 >= threshold and not row.get('error'))
            admission_code = None
            if row.get('error') or reliability is None or reliability + 1e-12 < threshold:
                admission_code = 'E_STATE_MEASUREMENT_FAILED' if (row.get('error') or not reliability) else 'E_STATE_MIN_SUCCESS'
            elif strict and core.reputation_status(row) not in ('clean',):
                admission_code = 'E_STATE_REPUTATION_UNKNOWN'
            elif required_anonymity != 'any':
                # The judge was not part of this test, so this axis is unknown,
                # never a pass: the shared policy is not bypassed silently.
                ok = False
                admission_code = 'E_STATE_ANONYMITY'
            elif denied_by:
                ok = False
                admission_code = 'E_SCOPE_DENYLIST'
            return dict(ok=ok, latency_ms=row.get('latency_ms'), status=reliability,
                        error=row.get('error') or (errors[0] if errors else ('CHECK_FAILED' if not ok else None)),
                        admission=admission_code, targets=target_names, profile=profile,
                        timed_out=False, elapsed_s=round(time.monotonic() - started, 3),
                        requests_made=made, anonymity_evaluated=False, reputation_evaluated=False,
                        bandwidth_evaluated=False, stored=False)

        try:
            result = asyncio.run(_test())
        except Exception as exc:
            return dict(ok=False, error=type(exc).__name__, latency_ms=None, targets=target_names,
                        profile=profile, scope=scope, timed_out=False, stored=False)
        result['scope'] = dict(scope, requests_made=result.get('requests_made', 0),
                               elapsed_s=result.get('elapsed_s'), timed_out=result.get('timed_out', False))
        return result

    def add_denylist(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('Ожидается JSON объект.')
        proxies = payload.get('proxies')
        if not isinstance(proxies, list) or not 1 <= len(proxies) <= MAX_SELECTION:
            raise ValueError('Выберите от 1 до 1000 прокси для локального denylist.')
        normalized = []
        seen = set()
        for value in proxies:
            if not isinstance(value, str):
                raise ValueError('Выбран список содержит некорректный адрес прокси.')
            proxy = core.normalize(value)
            if proxy is None:
                raise ValueError('Нужен публичный IP-адрес, порт и протокол без логина или пароля.')
            if proxy not in seen:
                seen.add(proxy)
                normalized.append(proxy)

        with self.mutex:
            if self.running():
                raise ValueError('Сначала остановите текущую операцию.')
            settings = self.settings()
            existing = set(line.strip() for line in settings.get('denylist', '').splitlines() if line.strip())
            existing_proxies = {core.normalize(line) for line in existing}
            existing_proxies.discard(None)
            added = [proxy for proxy in normalized if proxy not in existing_proxies]
            if added:
                settings['denylist'] = '\n'.join(sorted(existing | set(added)))
                self.save(settings)
            return dict(added=added, count=len(added))

    # -- tags, favourites, notes, saved views, history ------------------

    def annotation_state(self):
        document = self.annotations.read()
        entries = document.get('entries')
        return entries if isinstance(entries, dict) else {}

    def tags_in_use(self):
        return sorted({tag for entry in self.annotation_state().values()
                       for tag in (entry.get('tags') or []) if isinstance(tag, str)})

    def set_annotation(self, proxies, op, value=None):
        """Apply one annotation operation and return its undo descriptor."""
        state = dict(self.annotation_state())
        before = {}
        changed = 0
        for proxy in proxies:
            entry = dict(state.get(proxy) or {})
            previous = dict(entry)
            if op == 'tag':
                tags = set(entry.get('tags') or [])
                tags.add(str(value))
                entry['tags'] = sorted(tags)
            elif op == 'untag':
                entry['tags'] = sorted(set(entry.get('tags') or []) - {str(value)})
                if not entry['tags']:
                    entry.pop('tags', None)
            elif op == 'note':
                entry['note'] = str(value or '')[:2000]
            elif op == 'favorite':
                entry['favorite'] = True
            elif op == 'unfavorite':
                entry['favorite'] = False
            elif op == 'exclude':
                entry['excluded'] = True
            elif op == 'include':
                entry['excluded'] = False
                if not entry.get('excluded'):
                    entry.pop('excluded', None)
            else:
                raise ValueError('Неизвестная операция с метками.')
            if entry != previous:
                state[proxy] = {key: item for key, item in entry.items() if item not in (None, [], {})}
                before[proxy] = previous
                changed += 1
        self.annotations.write(dict(self.annotations.read(), entries=state))
        return changed, {'kind': 'annotations', 'before': before}

    def annotation_undo(self, descriptor):
        state = dict(self.annotation_state())
        for proxy, previous in (descriptor.get('before') or {}).items():
            if previous:
                state[proxy] = previous
            else:
                state.pop(proxy, None)
        self.annotations.write(dict(self.annotations.read(), entries=state))
        return len(descriptor.get('before') or {})

    def record_history(self, kind, summary, undo=None, **extra):
        document = self.history.read()
        entries = [item for item in (document.get('entries') or []) if isinstance(item, dict)]
        entry = dict(id=secrets.token_hex(6), at=time.time(), kind=kind, summary=summary,
                     recoverable=bool(undo), undo=undo, **extra)
        entries.append(entry)
        self.history.write(dict(document, entries=entries[-HISTORY_LIMIT:]))
        return entry

    def undo_history(self, payload):
        target = (payload or {}).get('id')
        document = self.history.read()
        entries = [item for item in (document.get('entries') or []) if isinstance(item, dict)]
        found = None
        for entry in reversed(entries):
            if target and entry.get('id') != target:
                continue
            if entry.get('recoverable') and not entry.get('undone'):
                found = entry
                break
        if found is None:
            raise ValueError('Нет операции, которую можно отменить.')
        descriptor = found.get('undo') or {}
        if descriptor.get('kind') == 'annotations':
            self.annotation_undo(descriptor)
        elif descriptor.get('kind') == 'denylist':
            self.denylist_undo(descriptor)
        else:
            raise ValueError('Эту операцию нельзя отменить автоматически.')
        for entry in entries:
            if entry.get('id') == found.get('id'):
                entry['undone'] = True
                entry['undone_at'] = time.time()
        self.history.write(dict(document, entries=entries))
        return dict(undone=found['id'], summary=found.get('summary'), kind=found.get('kind'))

    def denylist_undo(self, descriptor):
        removed = set(descriptor.get('removed') or [])
        if not removed:
            return 0
        with self.mutex:
            settings = self.settings()
            kept = [line for line in settings.get('denylist', '').splitlines()
                    if line.strip() and line.strip() not in removed]
            settings['denylist'] = '\n'.join(kept)
            self.save(settings)
        return len(removed)

    def saved_views(self):
        document = self.views.read()
        views = [item for item in (document.get('views') or []) if isinstance(item, dict)]
        return [dict(item, id=str(item.get('id') or '')) for item in views if item.get('id')]

    def store_view(self, payload):
        name = str((payload or {}).get('name') or '').strip()[:80]
        if not name:
            raise ValueError('Дайте представлению название.')
        query = (payload or {}).get('query') or {}
        if not isinstance(query, dict):
            raise ValueError('Некорректные условия представления.')
        try:
            self.parse_results_query(self.query_of(query))
        except ValueError:
            raise ValueError('Некорректные условия представления.') from None
        columns = [str(name) for name in ((payload or {}).get('columns') or []) if str(name) in ALL_COLUMNS]
        view = dict(id=secrets.token_hex(4), name=name, query=query, columns=columns or list(COMPACT_COLUMNS),
                    created_at=time.time())
        views = [item for item in self.saved_views() if item.get('name') != name] + [view]
        self.views.write(dict(self.views.read(), views=views))
        return view

    def delete_view(self, payload):
        target = str((payload or {}).get('id') or '')
        views = self.saved_views()
        remaining = [item for item in views if item.get('id') != target]
        if len(remaining) == len(views):
            raise ValueError('Представление не найдено.')
        self.views.write(dict(self.views.read(), views=remaining))
        return dict(removed=target)

    # -- bulk scope -------------------------------------------------------

    def resolve_scope(self, payload):
        """Server-side scope resolution: the browser sends a filter, not rows."""
        scope = (payload or {}).get('scope')
        if scope not in BULK_SCOPES:
            raise ValueError('Неизвестная область массовой операции.')
        if scope == 'selected':
            values = (payload or {}).get('proxies')
            if not isinstance(values, list) or not 1 <= len(values) <= MAX_SELECTION:
                raise ValueError(f'Выберите от 1 до {MAX_SELECTION} прокси.')
            proxies = []
            for value in values:
                normalized = core.normalize(value) if isinstance(value, str) else None
                if normalized and normalized not in proxies:
                    proxies.append(normalized)
            if not proxies:
                raise ValueError('Выбранные адреса не распознаны.')
            return proxies
        plan = self.result_plan(self.query_of((payload or {}).get('query') or {}))
        expected = (payload or {}).get('scope_digest')
        if expected and expected != plan['digest']:
            # F19: a selection or an "all matching" action must not silently
            # move to another scope after filters or generation changed.
            raise ValueError('Область изменилась: обновите таблицу и повторите действие.')
        rows, total, _, _ = self.scoped_rows(plan, limit=None)
        proxies = [row['proxy'] for row in rows if row.get('proxy')]
        if scope == 'page':
            proxies = proxies[plan['offset']:plan['offset'] + plan['limit']]
        if len(proxies) > MAX_BULK_SELECTION:
            raise ValueError(f'Слишком много строк ({len(proxies)}). Сузите фильтры.')
        return proxies

    def bulk(self, payload):
        """One bulk action over page / selected / all-matching rows."""
        if not isinstance(payload, dict):
            raise ValueError('Ожидается JSON объект.')
        op = payload.get('op')
        if op not in BULK_OPS:
            raise ValueError('Неизвестная массовая операция.')
        scope = payload.get('scope', 'selected')
        if scope not in BULK_SCOPES:
            raise ValueError('Неизвестная область массовой операции.')
        proxies = self.resolve_scope(payload)
        if not proxies:
            raise ValueError('В этой области нет строк.')
        common = dict(op=op, scope=scope, count=len(proxies))
        if op in ('recheck', 'export'):
            action = 'recheck' if op == 'recheck' else 'export'
            if op == 'export' and scope != 'selected' and not payload.get('settings'):
                raise ValueError('Для экспорта всей выборки сначала сохраните настройки.')
            body = dict(action=action, selection=proxies, settings=payload.get('settings'))
            if op == 'export':
                body['q'] = payload.get('q', '')
            job = self.start(body, max_selection=MAX_BULK_SELECTION)
            entry = self.record_history('job', f'{op}: {len(proxies)}', None, count=len(proxies),
                                        scope=scope, job_id=job.get('id'))
            return dict(common, job=job, history=entry)
        if op == 'copy':
            if len(proxies) > MAX_BULK_COPY:
                raise ValueError(f'Слишком много строк для копирования ({len(proxies)}).')
            return dict(common, text='\n'.join(proxies) + '\n', history=None)
        value = None
        if op in ('tag', 'untag'):
            value = str(payload.get('tag') or '').strip()[:40]
            if not value:
                raise ValueError('Укажите тег.')
        elif op == 'note':
            value = str(payload.get('note') or '').strip()[:2000]
        if op == 'denylist':
            added = self.add_denylist(dict(proxies=proxies))
            entry = self.record_history('denylist', f'denylist: {added["count"]}',
                                        {'kind': 'denylist', 'removed': list(added['added'])},
                                        count=added['count'])
            return dict(common, added=added['added'], count=len(added['added']), history=entry)
        changed, undo = self.set_annotation(proxies, op, value)
        summary = f'{op}: {changed}'
        entry = self.record_history('annotations', summary, undo if op in RECOVERABLE_OPS else None,
                                    count=changed)
        return dict(common, changed=changed, history=entry)

    # -- real measurement events (defect 25, R17) -------------------------

    def _event_seq(self, stream):
        return self.event_seq.get(stream, 0)

    def _append_events(self, records):
        """Append real events to the durable log and return them with seq."""
        if not records:
            return []
        written = []
        try:
            with self.events_path.open('a', encoding='utf-8') as handle:
                for record in records:
                    stream = record['stream']
                    self.event_seq[stream] = self._event_seq(stream) + 1
                    event = dict(record, seq=self.event_seq[stream])
                    handle.write(json.dumps(event, ensure_ascii=False) + '\n')
                    written.append(event)
        except OSError:
            return written
        self._trim_events()
        return written

    def _trim_events(self):
        try:
            if self.events_path.stat().st_size <= 512 * 1024:
                return
            with self.events_path.open('r', encoding='utf-8') as handle:
                lines = handle.readlines()
        except OSError:
            return
        kept = lines[-EVENT_RETENTION:]
        try:
            core.atomic(self.events_path, ''.join(kept))
            self.event_floor = min((json.loads(line).get('seq', 0) for line in kept
                                    if line.strip()), default=0)
        except (OSError, ValueError):
            return

    def _load_events(self):
        if getattr(self, 'events_loaded', False):
            return
        self.event_seq = {}
        self.event_floor = 0
        seen = set()
        try:
            with self.events_path.open('r', encoding='utf-8') as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    stream, seq = event.get('stream'), event.get('seq')
                    if not isinstance(stream, str) or not isinstance(seq, int):
                        continue
                    self.event_seq[stream] = max(self.event_seq.get(stream, 0), seq)
                    self.event_floor = min(self.event_floor or seq, seq)
                    key = (event.get('item_id'), event.get('at'), event.get('code'))
                    if key[0] is not None:
                        seen.add(key)
        except OSError:
            pass
        self.event_seen = seen
        self.events_loaded = True

    def collect_events(self):
        """Turn finished measurements into events, once each.

        The source is the measurement store itself: one event per completed
        observation, with the proxy, the measured latency and the outcome code.
        No line of the aggregated scan log is parsed and presented as a stream
        of per-proxy checks.
        """
        self._load_events()
        plan = self.result_plan({})
        records = []
        job = dict(self.job or {})
        job_id = job.get('id') or 'none'
        if job.get('started_at') and not job.get('finished_at') and self.process is not None:
            state_key = ('job', job_id, 'running', job.get('started_at'))
            if state_key not in self.event_seen:
                self.event_seen.add(state_key)
                records.append(dict(stream='system', at=time.time(), type='job.state',
                                    job_id=job_id, item_id=None, code='OK',
                                    data=dict(state='running', action=job.get('action'))))
        conn = self.read_connection() if plan.get('profile') and plan.get('available') else None
        if conn is not None:
            try:
                # Only measurements at or after the newest one already reported
                # are new; the first call backfills the latest 200 so a large
                # database is not read end to end on every poll.
                if not self.event_backfilled:
                    self.event_backfilled = True
                    rows = conn.execute(
                        'SELECT proxy, payload FROM results WHERE profile=? '
                        "AND json_extract(payload,'$.checked_at') IS NOT NULL "
                        'ORDER BY json_extract(payload,\'$.checked_at\') DESC, proxy LIMIT 200',
                        (plan['profile'],))
                else:
                    rows = conn.execute(
                        'SELECT proxy, payload FROM results WHERE profile=? '
                        "AND json_extract(payload,'$.checked_at')>=? "
                        'ORDER BY json_extract(payload,\'$.checked_at\'), proxy',
                        (plan['profile'], self.event_mark))
                for proxy, payload in rows:
                    try:
                        body = json.loads(payload)
                    except (TypeError, ValueError):
                        continue
                    checked_at = body.get('checked_at')
                    if not isinstance(checked_at, (int, float)) or isinstance(checked_at, bool):
                        continue
                    code = self.event_code(body)
                    key = (proxy, float(checked_at), code)
                    if key in self.event_seen:
                        continue
                    self.event_seen.add(key)
                    self.event_mark = max(self.event_mark, float(checked_at))
                    records.append(dict(stream=f'job:{job_id}', at=float(checked_at),
                                        type='item.observation', job_id=job_id, item_id=proxy,
                                        code=code,
                                        data=dict(proxy=proxy, latency_ms=body.get('latency_ms'),
                                                  jitter_ms=body.get('jitter_ms'),
                                                  reliability=body.get('min_target_reliability'),
                                                  requests=body.get('requests'), successes=body.get('successes'),
                                                  error=body.get('error'))))
            except sqlite3.Error:
                pass
            finally:
                conn.close()
        if job.get('finished_at') and not job.get('state_reported'):
            self.event_seen.add(('job', job_id, 'finished', job.get('finished_at')))
            self.job['state_reported'] = True
            records.append(dict(stream='system', at=float(job['finished_at']), type='job.state',
                                job_id=job_id, item_id=None, code='OK',
                                data=dict(state='succeeded' if job.get('exit_code') in (0, 130) else 'failed',
                                          exit_code=job.get('exit_code'))))
        return self._append_events(records)

    @staticmethod
    def event_code(body):
        """The outcome of one measurement, as a stable machine code."""
        error = body.get('error')
        if isinstance(error, dict):
            error = error.get('code') or error.get('stage')
        if error:
            return str(error)[:64]
        reliability = body.get('min_target_reliability')
        if isinstance(reliability, (int, float)) and not isinstance(reliability, bool):
            return 'OK' if reliability > 0 else 'E_STATE_MEASUREMENT_FAILED'
        return 'E_STATE_NO_OBSERVATION'

    def events(self, query):
        """New events after a cursor, in cursor order (CONTRACTS §5.7)."""
        self._load_events()
        stream = query.get('stream', [''])[0]
        cursor = str(query.get('after', ['0'])[0] or '0')
        try:
            limit = max(1, min(int(query.get('limit', ['200'])[0]), 1000))
        except ValueError:
            limit = 200
        after = 0
        if ':' in cursor:
            name, _, tail = cursor.rpartition(':')
            if tail.isdigit():
                stream, after = stream or name, int(tail)
            else:
                raise ValueError('Некорректный курсор событий.')
        elif cursor.isdigit():
            after = int(cursor)
        self.collect_events()
        found = []
        oldest = None
        try:
            with self.events_path.open('r', encoding='utf-8') as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if stream and event.get('stream') != stream:
                        continue
                    seq = event.get('seq')
                    if not isinstance(seq, int):
                        continue
                    if oldest is None or seq < oldest:
                        oldest = seq
                    if seq > after:
                        found.append(event)
        except OSError:
            return dict(events=[], cursor=cursor, stream=stream)
        found.sort(key=lambda item: (item.get('stream', ''), item.get('seq', 0)))
        truncated = found[:limit]
        last = truncated[-1] if truncated else None
        return dict(events=truncated, stream=stream,
                    cursor=(f'{last["stream"]}:{last["seq"]}' if last else cursor),
                    oldest_seq=oldest, source='measurements', truncated=len(found) > limit)

    # --- API keys (F29) -----------------------------------------------------
    #
    # `apikeys` was fully written and reachable only from the CLI: `grep` for
    # "key" in this file found nothing, so a user who launched the application
    # without arguments had no way to get the first key and `/v1` was a door
    # with no key in it (F29 asks for the manager in the GUI *and* the CLI).
    #
    # The interface is a locally trusted surface: it holds the data folder
    # lock, its session token is minted per run, and every request must come
    # from 127.0.0.1 with this run's Host and Origin.  So `bootstrap` may mint
    # the first administrator without presenting a credential, exactly as the
    # CLI does.  Every other action goes through the real permission machinery
    # of the module: the actor is either the administrator secret the user
    # pasted into the page (kept in the page's memory, never written to a file
    # or a log) or the installation's own active administrator key, and the
    # audit log names that key either way.

    def api_key_banner(self):
        """What the terminal says about the keys of `/v1`, without a secret.

        The first administrator is not minted here: a terminal line that shows
        a secret scrolls away, and the page is the place where a secret is shown
        once on purpose.  The banner only counts the keys, so a user who starts
        the application with no arguments is told that the door exists and where
        the key is (F29).
        """
        try:
            admins = self.key_admins()
        except (ValueError, OSError, sqlite3.Error, apikeys.ApiKeyError):
            return []
        if not admins:
            return [tr('Ключей API нет: выпустите первый на странице «Ключи» в интерфейсе.',
                       'No API keys yet: issue the first one on the Keys page.')]
        return [tr(f'Ключей API: {len(admins)} административных. Управление — в интерфейсе, вкладка «Ключи».',
                   f'API keys: {len(admins)} administrator key(s). Manage them on the Keys page.')]

    def keys(self):
        if self._key_manager is None:
            manager = api.key_manager(self.data)
            if manager is None:
                raise ValueError('Локальная база недоступна; ключи создать нельзя.')
            self._key_manager = manager
        return self._key_manager

    @staticmethod
    def key_error(exc):
        """An `ApiKeyError` as a sentence the page can show verbatim."""
        detail = str(getattr(exc, 'detail', '') or '')
        action = str(getattr(exc, 'action', '') or '')
        code = str(getattr(exc, 'code', '') or '')
        text = detail or code
        return ' '.join(part for part in (text, action) if part) or 'Ключ отклонён.'

    def key_admins(self):
        """Active keys holding ``admin.keys``, oldest first.

        The same rule the CLI applies: not revoked, not disabled, not expired.
        """
        manager = self.keys()
        now = manager.now()
        found = []
        for row in manager.conn.execute(
                'SELECT id, name, permissions_json, expires_at, revoked_at, disabled_at, created_at '
                'FROM api_keys ORDER BY created_at, id').fetchall():
            if row['revoked_at'] is not None or row['disabled_at'] is not None:
                continue
            if row['expires_at'] is not None and row['expires_at'] <= now:
                continue
            try:
                granted = set(json.loads(row['permissions_json'] or '[]'))
            except (TypeError, ValueError):
                continue
            if 'admin.keys' in granted:
                found.append({'id': row['id'], 'name': row['name'],
                              'created_at': row['created_at']})
        return found

    def key_admin_actor(self, secret=None):
        manager = self.keys()
        if secret:
            try:
                return manager.authenticate(secret, permission='admin.keys', touch=False)
            except apikeys.ApiKeyError as exc:
                raise ValueError(self.key_error(exc)) from None
        admins = self.key_admins()
        if not admins:
            raise ValueError('Административного ключа ещё нет. Выпустите его на этой странице — '
                             'управление ключами откроется сразу после этого.')
        try:
            # The oldest active administrator is the identity the interface
            # acts as; the module checks its rights and the audit log names it.
            return apikeys.Principal(manager.get_key(admins[0]['id']))
        except apikeys.ApiKeyError as exc:  # revoked between the two reads
            raise ValueError(self.key_error(exc)) from None

    def keys_view(self, payload=None):
        """The keys page: every key but never a secret, plus what may be granted."""
        payload = payload or {}
        manager = self.keys()
        admins = self.key_admins()
        body = dict(keys=[], admins=[item['id'] for item in admins], bootstrapped=bool(admins),
                    permissions=sorted(apikeys.PERMISSIONS),
                    local_permissions=list(core.local_admin_permissions()),
                    read_permissions=sorted(apikeys.READ_PERMISSIONS),
                    write_permissions=sorted(apikeys.WRITE_PERMISSIONS),
                    admin_permissions=sorted(apikeys.ADMIN_PERMISSIONS),
                    api_url=getattr(self, 'api_url', None),
                    audit=[])
        if not admins and not payload.get('admin_secret'):
            body['notice'] = tr('Административного ключа ещё нет. Выпустите первый ключ — '
                                'он показывается один раз.',
                                'There is no administrator key yet. Issue the first one — '
                                'its secret is shown once.')
            return body
        actor = self.key_admin_actor(payload.get('admin_secret'))
        now = manager.now()
        body['keys'] = [info.as_dict(now) for info in manager.list_keys(actor=actor, limit=500)]
        body['actor'] = actor.key_id
        body['audit'] = manager.read_audit(actor=actor, limit=40)
        return body

    def key_bootstrap(self, payload):
        """Mint the first administrator through the local trusted surface."""
        payload = payload or {}
        manager = self.keys()
        name = str(payload.get('name') or 'administrator').strip()[:120] or 'administrator'
        issued = manager.bootstrap_admin(local_trusted=True, name=name,
                                         purpose=str(payload.get('purpose') or '') or None,
                                         permissions=core.local_admin_permissions(),
                                         ttl_s=self.key_ttl(payload))
        return issued.as_json()

    @staticmethod
    def key_ttl(payload):
        days = payload.get('ttl_days')
        if days in (None, '', 0):
            return None
        try:
            value = float(days)
        except (TypeError, ValueError):
            raise ValueError('Срок действия должен быть числом дней.') from None
        if value <= 0 or value > 3650:
            raise ValueError('Срок действия: от 1 до 3650 дней.')
        return value * 86400

    def key_create(self, payload):
        """Issue a key with exactly the rights and the scope the user chose."""
        payload = payload or {}
        manager = self.keys()
        actor = self.key_admin_actor(payload.get('admin_secret'))
        name = str(payload.get('name') or '').strip()
        if not name:
            raise ValueError('Укажите название ключа.')
        granted = payload.get('permissions')
        if isinstance(granted, str):
            granted = [item.strip() for item in granted.split(',') if item.strip()]
        if not isinstance(granted, (list, tuple)):
            granted = apikeys.READ_PERMISSIONS
        unknown = sorted(set(granted) - set(apikeys.PERMISSIONS))
        if unknown:
            raise ValueError('Неизвестные права: ' + ', '.join(unknown))
        scope = payload.get('scope')
        if scope is not None and not isinstance(scope, dict):
            raise ValueError('Область ключа должна быть объектом со списками collections и pools.')
        try:
            issued = manager.create(actor=actor, name=name,
                                    purpose=str(payload.get('purpose') or '') or None,
                                    permissions=granted, scope=scope or None,
                                    ttl_s=self.key_ttl(payload))
        except apikeys.ApiKeyError as exc:
            raise ValueError(self.key_error(exc)) from None
        return issued.as_json()

    def key_action(self, payload):
        """Rotate, disable, enable, revoke or delete one key."""
        payload = payload or {}
        manager = self.keys()
        key_id = str(payload.get('id') or '').strip()
        action = str(payload.get('action') or '').strip()
        if not key_id:
            raise ValueError('Укажите ключ.')
        actor = self.key_admin_actor(payload.get('admin_secret'))
        try:
            if action == 'rotate':
                grace = float(payload.get('grace_s') or 0)
                if grace < 0 or grace > 86400:
                    raise ValueError('Окно ротации: от 0 до 86400 секунд.')
                return manager.rotate(key_id, actor=actor, grace_s=grace).as_json()
            if action == 'update':
                fields = {}
                if payload.get('name') is not None:
                    fields['name'] = str(payload['name']).strip()
                if payload.get('purpose') is not None:
                    fields['purpose'] = str(payload['purpose'])
                if 'ttl_days' in payload:
                    fields['ttl_s'] = self.key_ttl(payload)
                if not fields:
                    raise ValueError('Нечего менять: укажите название, описание или срок.')
                info = manager.update_metadata(key_id, actor=actor, **fields)
            elif action == 'disable':
                info = manager.disable(key_id, actor=actor)
            elif action == 'enable':
                info = manager.enable(key_id, actor=actor)
            elif action == 'revoke':
                info = manager.revoke(key_id, actor=actor)
            elif action == 'delete':
                manager.delete(key_id, actor=actor)
                return dict(id=key_id, state='deleted', deleted=True)
            else:
                raise ValueError('Неизвестное действие с ключом: ' + (action or '—'))
        except apikeys.ApiKeyError as exc:
            raise ValueError(self.key_error(exc)) from None
        return info.as_dict(manager.now())

    # --- import (F03) -------------------------------------------------------
    #
    # `importer.py` is 1162 lines of a transactionally committing, previewing,
    # mapping-aware importer and the interface could not reach any of it: the
    # own-list box writes membership directly through `add_collection_members`,
    # so there was no preview, no rejected line number, no column mapping, no
    # merge/replace choice, no cancellation and no report.  The routes below
    # are thin: the work is `importer`'s.

    @staticmethod
    def import_source_of(payload):
        text = payload.get('text')
        name = str(payload.get('name') or 'clipboard')
        channel = str(payload.get('channel') or 'clipboard')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Вставьте список адресов или выберите файл.')
        return importer.ImportSource.from_text(text, name=name, channel=channel)

    @staticmethod
    def import_mapping_of(payload):
        """The column mapping the page sends, or None when it sends none."""
        raw = payload.get('mapping')
        if not isinstance(raw, dict) or not any(raw.get(role) is not None for role in importer.ROLES):
            return None
        return importer.ColumnMapping(**{role: raw.get(role) for role in importer.ROLES})

    def import_policy_of(self, collection_id):
        """Public-only for the public base, permissive for a personal list."""
        if collection_id == schema.PUBLIC_COLLECTION_ID:
            return importer.DEFAULT_POLICY
        return importer.EndpointPolicy(public_only=False)

    def remember_import_plan(self, plan):
        stored = [item for item in self._import_plans if item[0] != plan.batch_id]
        stored.append((plan.batch_id, plan))
        self._import_plans = stored[-IMPORT_PLANS:]
        return plan.batch_id

    def import_preview(self, payload):
        """Read an import and say what it would do. Writes nothing at all."""
        payload = payload or {}
        mode = str(payload.get('mode') or 'merge')
        fmt = str(payload.get('format') or '') or None
        if fmt and fmt not in importer.FORMATS:
            raise ValueError('Неизвестный формат импорта: ' + fmt)
        source = self.import_source_of(payload)
        conn = self.read_connection()
        if conn is None:
            raise ValueError('Локальная база ещё не создана. Сначала соберите адреса.')
        try:
            wanted = self.require_collection(conn, payload.get('collection'))
            plan = importer.preview(conn, source, collection_id=wanted, mode=mode, fmt=fmt,
                                    mapping=self.import_mapping_of(payload),
                                    policy=self.import_policy_of(wanted),
                                    idempotency_key=payload.get('idempotency_key') or None)
        except importer.ImportProblem as exc:
            raise ValueError(str(exc)) from None
        finally:
            conn.close()
        self.remember_import_plan(plan)
        return self.import_plan_view(plan)

    @staticmethod
    def import_plan_view(plan):
        body = plan.to_dict()
        # `Preview.to_dict` describes the problem but not the proposal, and the
        # page cannot offer a column picker without knowing which columns the
        # header had and which role each one could take.  Both come from the
        # module's own `MappingSuggestion`; nothing here re-guesses a role.
        suggestion = plan.mapping_suggestion
        body['mapping_suggestion'] = suggestion.to_dict() if suggestion is not None else None
        body['columns'] = list(body.get('mapping') or {})
        body['can_commit'] = not plan.needs_mapping
        body['modes'] = list(importer.MODES)
        body['formats'] = list(importer.FORMATS)
        return body

    def import_commit(self, payload):
        """Apply exactly the plan the page was shown.

        The plan object is the one `preview()` returned: a `Preview` cannot be
        rebuilt from JSON, so keeping it here is what makes "apply" mean the
        numbers above the button rather than a second, possibly different
        computation.  A repeated commit of the same plan replays (F29
        idempotency) and adds nothing.
        """
        payload = payload or {}
        batch_id = str(payload.get('batch_id') or '').strip()
        found = next((item for item in self._import_plans if item[0] == batch_id), None)
        if found is None:
            raise ValueError('Предпросмотр устарел или не найден. Повторите предпросмотр.')
        plan = found[1]
        allow_partial = bool(payload.get('allow_partial'))
        if plan.rejected and not allow_partial:
            raise ValueError('В файле %d непригодных строк. Подтвердите импорт только пригодных '
                             'или исправьте файл.' % len(plan.rejected))
        # Read the reason before the write lock: `importer.commit` wants a
        # callable, and the callable must not open a second connection to the
        # file the writer already holds.
        busy_reason = self.busy_collection(plan.collection_id)
        with self.collection_write() as conn:
            try:
                report = importer.commit(conn, plan, allow_partial=allow_partial,
                                         busy=lambda: busy_reason)
            except importer.ImportProblem as exc:
                raise ValueError(str(exc)) from None
        body = report.to_dict()
        body['members'] = self.collection_members({'collection': plan.collection_id})
        return body

    def busy_collection(self, collection_id):
        """Why a collection cannot take members right now, or None.

        Read *before* the write lock is taken: opening a second connection to
        the same file from inside `collection_write()` would run the migrator
        again while the writer holds the lock.
        """
        try:
            with closing(core.Workbench(self.data)) as workbench:
                return core._busy_collection(workbench, collection_id)
        except (OSError, ValueError, sqlite3.Error):
            return None

    def import_batches(self, query=None):
        """Recent import batches with their report, newest first."""
        query = query or {}
        wanted = str((query.get('batch') or [''])[0] or '')
        conn = self.read_connection()
        if conn is None:
            return dict(batches=[], report=None)
        try:
            if wanted:
                report = importer.load_report(conn, wanted)
                if report is None:
                    raise ValueError('Отчёта об импорте нет: ' + wanted)
                return dict(batches=[], report=report.to_dict())
            # The read connection of the interface has no row factory, so the
            # columns are read by position.
            rows = [{'id': row[0], 'collection_id': row[1], 'state': row[2],
                     'revision': row[3], 'created_at': row[4]}
                    for row in conn.execute(
                        'SELECT id, collection_id, state, revision, created_at FROM import_batch '
                        'ORDER BY created_at DESC LIMIT 20').fetchall()]
        except sqlite3.Error as exc:
            raise ValueError('Не удалось прочитать историю импортов: %s' % exc) from None
        finally:
            conn.close()
        return dict(batches=rows, report=None, limits=dict(
            modes=list(importer.MODES), formats=list(importer.FORMATS)))

    # --- pools and schedules (F14, F15) ------------------------------------
    #
    # `pools.py` and `scheduler.py` are complete and were reachable only from
    # the CLI and `/v1`.  The routes below are the same calls the API makes
    # (`workbench.pool_refill`, `pools_state_for`, `Scheduler`), so there is
    # one implementation of a refill and not two.

    def pools_store(self, workbench):
        return workbench.pools()

    def pools_view(self, query=None):
        query = query or {}
        wanted = str((query.get('id') or [''])[0] or '')
        with core.Workbench(self.data) as workbench:
            store = self.pools_store(workbench)
            if wanted:
                spec = store.get(wanted)
                if spec is None:
                    raise ValueError('Пул не найден: ' + wanted)
                status = store.status(wanted)
                return dict(pool=api._pool_dict(spec), status=api._status_dict(status),
                            members=[api._member_dict(item) for item in store.members(wanted)])
            rows = []
            for spec in store.list():
                status = store.status(spec.id)
                # `sum(counts.values())` added `cooldown` and `probation` to
                # the number a client is actually served from: three active,
                # two resting and two on probation read as `served=7` next to
                # `desired=3`.  `PoolStatus.served` is the members in service
                # and is the only number comparable with `desired`; the other
                # phases are reported under their own names, and `count_unit`
                # says what one of them is -- a "5" means five addresses in one
                # pool and five distinct exit addresses in another.
                rows.append(dict(id=spec.id, collection_id=spec.collection_id,
                                 profile_id=spec.profile_id,
                                 profile_revision=spec.profile_revision,
                                 desired=spec.desired, reserve=spec.reserve,
                                 minimum=spec.minimum, state=status.state,
                                 served=status.served, count_unit=status.count_unit,
                                 counts=dict(status.counts or {}),
                                 shortfall=getattr(status, 'shortfall', None),
                                 deficit_reasons=[{'code': code, 'count': count}
                                                  for code, count in (status.deficit_reasons or ())],
                                 ready_for_clients=status.ready_for_clients,
                                 deficit_reason=status.deficit_reason,
                                 next_attempt_at=status.next_attempt_at))
            return dict(pools=rows, collections=self.collection_options(workbench.conn),
                        profiles=self.profile_options(workbench.conn))

    @staticmethod
    def collection_options(conn):
        try:
            return [{'id': row[0], 'name': row[1], 'kind': row[2]}
                    for row in conn.execute(
                        'SELECT id, name, kind FROM collections ORDER BY kind, name')]
        except sqlite3.Error:
            return []

    @staticmethod
    def profile_options(conn):
        try:
            return [{'id': row[0]} for row in conn.execute('SELECT id FROM profiles ORDER BY id')]
        except sqlite3.Error:
            return []

    def pool_create(self, payload):
        from . import pools as pools_module
        payload = payload or {}
        pool_id = str(payload.get('id') or payload.get('name') or '').strip()
        if not pool_id:
            raise ValueError('Укажите имя пула.')
        with core.Workbench(self.data) as workbench:
            store = self.pools_store(workbench)
            if store.get(pool_id) is not None:
                raise ValueError('Пул уже есть: ' + pool_id)
            profile_id = str(payload.get('profile_id') or '').strip() or self.active_profile_id()
            if not profile_id:
                raise ValueError('Пул привязан к профилю проверки: сначала выполните проверку '
                                 'или укажите профиль.')
            collection_id = str(payload.get('collection_id') or schema.PUBLIC_COLLECTION_ID)
            try:
                spec = store.create(pool_id, collection_id=collection_id, profile_id=profile_id,
                                    profile_revision=int(payload.get('profile_revision') or 1),
                                    desired=max(1, int(payload.get('desired') or 1)),
                                    minimum=max(0, int(payload.get('minimum') or 0)),
                                    reserve=max(0, int(payload.get('reserve') or 0)))
            except pools_module.PoolError as exc:
                raise ValueError(str(exc) or 'Не удалось создать пул.') from None
            return dict(pool=api._pool_dict(spec), status=api._status_dict(store.status(pool_id)))

    def active_profile_id(self):
        try:
            return (self.data/'last-profile.txt').read_text(encoding='utf-8').strip()
        except (OSError, UnicodeError):
            return ''

    def pool_action(self, payload):
        """Start, pause, refill, retarget or recheck one named pool."""
        from . import pools as pools_module
        payload = payload or {}
        pool_id = str(payload.get('id') or '').strip()
        action = str(payload.get('action') or '').strip()
        if not pool_id:
            raise ValueError('Укажите пул.')
        if action == 'recheck':
            return self.pool_recheck(pool_id, payload)
        with core.Workbench(self.data) as workbench:
            store = self.pools_store(workbench)
            spec = store.get(pool_id)
            if spec is None:
                raise ValueError('Пул не найден: ' + pool_id)
            status = None
            watch = None
            try:
                if action == 'start':
                    started = api.pools_state_for('start', store.status(pool_id))
                    store.save_status(pool_id, started.state, deficit_reason=started.deficit_reason,
                                      next_attempt_at=started.next_attempt_at)
                    status = workbench.pool_refill(pool_id, api.pool_candidate_source(workbench.conn))
                    # Start the pool and start keeping it: this is the same
                    # ``pools.watch`` the /v1 route starts, and ``pause`` below
                    # stops it, so the loop cannot outlive the pool.
                    watch = (pools_module.watch_registry(self.data).stop(pool_id)
                             if payload.get('watch') is False else
                             pools_module.watch_registry(self.data).start(
                                 pool_id, source_factory=api.pool_candidate_source))
                elif action == 'pause':
                    moved = api.pools_state_for('pause', store.status(pool_id))
                    store.save_status(pool_id, moved.state, deficit_reason=moved.deficit_reason,
                                      next_attempt_at=moved.next_attempt_at)
                    watch = pools_module.watch_registry(self.data).stop(pool_id)
                elif action == 'refill':
                    status = workbench.pool_refill(pool_id, api.pool_candidate_source(workbench.conn))
                elif action == 'target':
                    store.set_target(pool_id, desired=payload.get('desired'),
                                     reserve=payload.get('reserve'), minimum=payload.get('minimum'))
                elif action == 'policy':
                    store.set_policy(pool_id, payload.get('policy') or {})
                elif action == 'member-remove':
                    endpoint_id = str(payload.get('endpoint_id') or '')
                    if not store.remove_member(pool_id, endpoint_id):
                        raise ValueError('Участника нет в пуле: ' + endpoint_id)
                elif action == 'member-state':
                    endpoint_id = str(payload.get('endpoint_id') or '')
                    state = str(payload.get('state') or '')
                    if not endpoint_id or not state:
                        raise ValueError('Укажите участника и его состояние.')
                    store.set_member_state(pool_id, endpoint_id, state, admitted_at=time.time(),
                                           released_at=None)
                else:
                    raise ValueError('Неизвестное действие с пулом: ' + (action or '—'))
                return dict(pool=api._pool_dict(store.get(pool_id)),
                            status=api._status_dict(status or store.status(pool_id)),
                            members=[api._member_dict(item) for item in store.members(pool_id)],
                            action=action,
                            watch=watch,
                            watching=bool((watch or {}).get('watching')))
            except pools_module.PoolError as exc:
                raise ValueError(str(exc) or 'Пул отклонил изменение.') from None

    def pool_recheck(self, pool_id, payload):
        """Queue a measurement of the pool's own collection (F14)."""
        from . import jobs as jobs_module
        with core.Workbench(self.data) as workbench:
            spec = workbench.pools().require(pool_id)
            conn = workbench.conn
            members = [row[0] for row in conn.execute(
                'SELECT e.canonical FROM membership m JOIN endpoints e ON e.id = m.endpoint_id '
                'WHERE m.collection_id=? ORDER BY e.canonical', (spec.collection_id,)).fetchall()]
            items = [jobs_module.QueueItem(endpoint_id=api.schema_endpoint_id(conn, value),
                                           access_id=core.PUBLIC_ACCESS_ID, access_revision=1)
                     for value in members]
            conn.commit()
            job = workbench.jobs().submit(
                'pool_recheck',
                jobs_module.Scope(collection_id=spec.collection_id, profile_id=spec.profile_id,
                                  profile_revision=int(spec.profile_revision),
                                  profile_digest=spec.profile_id),
                items, idempotency_key=workbench.clock_ns())
            return dict(pool_id=pool_id, job_id=job.id, state=job.state,
                        collection_id=spec.collection_id, items=len(items), action='recheck')

    def schedule_activation(self, spec_id, state):
        """Give a schedule the activation instant the store cannot hold.

        ``schedules`` has no ``activated_at`` column and
        ``SqliteScheduleStore.load_state`` cannot restore one, so a schedule
        that has never run has no base instant and ``plan`` answers "not due,
        no next run".  The interface knows when it created the schedule, so it
        hands that instant to the module and lets the module compute the plan.
        """
        if state.activated_at is not None:
            return state
        stored = (self.schedule_activations.read().get('activated') or {}).get(spec_id)
        if stored:
            try:
                state.activated_at = float(stored)
            except (TypeError, ValueError):
                pass
        return state

    def remember_schedule_activation(self, spec_id, at=None):
        document = self.schedule_activations.read()
        activated = dict(document.get('activated') or {})
        activated[spec_id] = time.time() if at is None else float(at)
        self.schedule_activations.write({'activated': activated})

    def schedules_view(self, query=None):
        """Every schedule with its next run, its state and its counters."""
        query = query or {}
        wanted = str((query.get('id') or [''])[0] or '')
        from . import scheduler as scheduler_module
        with core.Workbench(self.data) as workbench:
            engine = scheduler_module.Scheduler(workbench.schedules(), clock=time.time)
            rows = []
            for spec in engine.list():
                state = self.schedule_activation(spec.id, engine.state(spec.id))
                plan = engine.plan(spec, state, time.time())
                runs = workbench.schedules().recent_runs(spec.id, limit=5)
                rows.append(dict(id=spec.id, kind=spec.kind, enabled=spec.enabled,
                                 interval_minutes=spec.interval_minutes,
                                 timezone=spec.timezone, pool_id=spec.pool_id,
                                 windows=[item.to_dict() for item in spec.windows],
                                 quiet_hours=[item.to_dict() for item in spec.quiet_hours],
                                 budgets=spec.budgets.to_dict() if hasattr(spec.budgets, 'to_dict') else {},
                                 next_at=getattr(plan, 'next_at', None),
                                 next_reason=getattr(plan, 'reason', None),
                                 overdue=getattr(plan, 'overdue', 0),
                                 paused=state.paused, pause_reason=state.pause_reason,
                                 last_run_at=state.last_run_at, runs=state.runs,
                                 skipped=state.skipped,
                                 counters=state.counters.to_dict() if hasattr(state, 'counters') else {},
                                 recent=[dict(item) for item in runs],
                                 selected=(wanted == spec.id)))
            return dict(schedules=rows, pools=[{'id': item.id} for item in workbench.pools().list()],
                        selected=wanted or None,
                        persistence=engine.persistence(),
                        power=engine.report()['power'],
                        detail=next((item for item in rows if item['id'] == wanted), None))

    def schedule_action(self, payload):
        """Add, enable, disable, remove or run one schedule."""
        import dataclasses
        from . import scheduler as scheduler_module
        payload = payload or {}
        schedule_id = str(payload.get('id') or payload.get('name') or '').strip()
        action = str(payload.get('action') or '').strip()
        if action == 'add' and not schedule_id:
            raise ValueError('Укажите имя расписания.')
        with core.Workbench(self.data) as workbench:
            engine = scheduler_module.Scheduler(workbench.schedules(), clock=time.time)
            store = workbench.schedules()
            if action == 'add':
                windows = []
                for item in (payload.get('windows') or []):
                    try:
                        windows.append(scheduler_module.Window.parse(item))
                    except (scheduler_module.ScheduleError, ValueError) as exc:
                        raise ValueError('Окно запуска неверно: %s' % exc) from None
                quiet = []
                for item in (payload.get('quiet_hours') or []):
                    try:
                        quiet.append(scheduler_module.Window.parse(item))
                    except (scheduler_module.ScheduleError, ValueError) as exc:
                        raise ValueError('Тихое время задано неверно: %s' % exc) from None
                spec_body = {'id': schedule_id, 'kind': 'interval',
                             'interval_minutes': payload.get('interval_minutes') or 60,
                             'timezone': str(payload.get('timezone') or 'UTC'),
                             'windows': [item.to_dict() for item in windows],
                             'quiet_hours': [item.to_dict() for item in quiet]}
                if payload.get('pool_id'):
                    spec_body['pool_id'] = str(payload['pool_id'])
                budgets = payload.get('budgets')
                if isinstance(budgets, dict) and budgets:
                    spec_body['budgets'] = budgets
                try:
                    spec = engine.add(spec_body)
                except scheduler_module.ScheduleError as exc:
                    raise ValueError(exc.text()) from None
                self.remember_schedule_activation(spec.id)
                self.schedule_activation(spec.id, engine.state(spec.id))
            else:
                spec = next((item for item in engine.list() if item.id == schedule_id), None)
                if spec is None:
                    raise ValueError('Расписание не найдено: ' + schedule_id)
                if action == 'remove':
                    engine.remove(schedule_id)
                    return dict(id=schedule_id, removed=True, action=action)
                if action == 'enable':
                    store.save_spec(dataclasses.replace(spec, enabled=True))
                    engine.resume(schedule_id)
                elif action == 'disable':
                    store.save_spec(dataclasses.replace(spec, enabled=False))
                    engine.pause(schedule_id)
                elif action == 'run-now':
                    run = engine.run_now(schedule_id)
                    if run is None:
                        raise ValueError('Расписание %s сейчас не может запуститься '
                                         '(окно, тихое время или пауза).' % schedule_id)
                    return dict(id=schedule_id, run_id=run.run_id, scheduled_for=run.scheduled_for,
                                reason=run.reason, action=action)
                elif action == 'pause':
                    engine.pause(schedule_id)
                elif action == 'resume':
                    engine.resume(schedule_id)
                else:
                    raise ValueError('Неизвестное действие с расписанием: ' + (action or '—'))
            spec = next((item for item in engine.list() if item.id == schedule_id), spec)
            state = self.schedule_activation(schedule_id, engine.state(schedule_id))
            plan = engine.plan(spec, state, time.time())
        return dict(id=spec.id, enabled=spec.enabled, interval_minutes=spec.interval_minutes,
                    timezone=spec.timezone, next_at=getattr(plan, 'next_at', None),
                    next_reason=getattr(plan, 'reason', None),
                    paused=state.paused, action=action)

    # --- gateway binding ----------------------------------------------------

    def gateway_settings(self):
        """The pool and profile the user bound the listener to."""
        stored = self.gateway_config.read() or {}
        known = {'pool_id': str(stored.get('pool_id') or ''),
                 'generation': str(stored.get('generation') or '') or None,
                 'profile_id': str(stored.get('profile_id') or '') or None,
                 'profile_revision': stored.get('profile_revision'),
                 'policy': stored.get('policy') if isinstance(stored.get('policy'), dict) else {}}
        return known

    def gateway_binding(self):
        """The `gateway.Binding` the settings describe, or None."""
        settings = self.gateway_settings()
        if not any((settings.get('pool_id'), settings.get('generation'),
                    settings.get('profile_id'), settings.get('profile_revision'))):
            return None
        revision = settings.get('profile_revision')
        try:
            revision = int(revision) if revision is not None else None
        except (TypeError, ValueError):
            revision = None
        return gateway.Binding(pool_id=settings['pool_id'] or 'default',
                               generation=settings.get('generation') or None,
                               profile_id=settings.get('profile_id') or None,
                               profile_revision=revision,
                               policy=dict(settings.get('policy') or {}))

    def gateway_options(self):
        """What the gateway page can bind to: pools, profiles, generations."""
        body = dict(bindings=self.gateway_settings(),
                    active_profile=self.active_profile_id() or None,
                    pools=[], profiles=[], generations=[])
        try:
            with core.Workbench(self.data) as workbench:
                body['pools'] = [{'id': item.id, 'desired': item.desired,
                                  'collection_id': item.collection_id}
                                 for item in workbench.pools().list()]
                body['profiles'] = self.profile_options(workbench.conn)
        except (OSError, ValueError, sqlite3.Error):
            pass
        status = self.export_status()
        body['generations'] = [item for item in (status.get('generation') or '',) if item]
        return body

    def gateway_configure(self, payload):
        """Bind the listener to a pool and a profile, or unbind it."""
        payload = payload or {}
        settings = self.gateway_settings()
        if payload.get('reset'):
            self.gateway_config.write({})
            settings = self.gateway_settings()
        else:
            pool_id = str(payload.get('pool_id') or '')
            if pool_id and pool_id != 'default':
                try:
                    with core.Workbench(self.data) as workbench:
                        if workbench.pools().get(pool_id) is None:
                            raise ValueError('Пул не найден: ' + pool_id)
                except (OSError, ValueError, sqlite3.Error) as exc:
                    if isinstance(exc, ValueError):
                        raise
            revision = payload.get('profile_revision')
            try:
                revision = int(revision) if revision not in (None, '', 0) else None
            except (TypeError, ValueError):
                raise ValueError('Ревозия профиля должна быть числом.') from None
            settings = {'pool_id': pool_id or 'default',
                        'generation': str(payload.get('generation') or '') or None,
                        'profile_id': str(payload.get('profile_id') or '') or None,
                        'profile_revision': revision,
                        'policy': payload.get('policy') if isinstance(payload.get('policy'), dict) else {}}
            self.gateway_config.write(settings)
        running = getattr(self, 'gateway', None)
        restarted = False
        applied = None
        if running is not None:
            bind = getattr(self, 'gateway_bind', None)
            if bind is not None:
                # The binding is applied on the live listener first.  A restart
                # used to be the only path, so an empty pool produced a 502 for
                # the client that was connected and the person had no way to
                # tell "the pool is empty" from "the gateway is broken".
                # `set_binding` reports how many rows the new binding can
                # serve right now, which is the honest answer either way.
                try:
                    applied = running.set_binding(self.gateway_binding())
                except (ValueError, OSError, RuntimeError) as exc:
                    raise ValueError('Не удалось применить привязку на живом шлюзе: %s' % exc) from None
                if not isinstance(applied, dict) or 'rows' not in applied:
                    # An older listener without the live path still gets the
                    # choice, by rebuilding it.
                    self.stop_gateway()
                    try:
                        self.start_gateway()
                    except ValueError as exc:
                        raise ValueError(str(exc)) from None
                    restarted = True
                    applied = None
        return dict(bindings=self.gateway_settings(), restarted=restarted, applied=applied,
                    rows=(applied or {}).get('rows'),
                    empty=bool(applied) and not applied.get('rows'),
                    gateway=self.gateway_state())

    # --- storage maintenance (F24) -----------------------------------------
    #
    # `db.cleanup_preview`, `db.retention_preview`, `db.restore_preview` and
    # `db.migrate_data_path` were called from tests only.  The page therefore
    # deleted first and printed what went afterwards, and the two functions
    # that describe before they act had no way to be reached.  Every route
    # below answers with a preview first and only acts on `apply`.

    def cleanup_preview_view(self):
        """What a data-folder cleanup removes, what it keeps and how big it is."""
        try:
            preview = schema.cleanup_preview(self.data)
        except (schema.DbError, OSError) as exc:
            raise ValueError('Не удалось прочитать папку data: %s' % exc) from None
        return preview.to_dict()

    def cleanup_apply(self, payload):
        """Remove the runtime artifacts the preview named, and report the result."""
        payload = payload or {}
        with self.mutex:
            if self.running():
                raise ValueError('Сначала остановите текущую операцию.')
            try:
                with self.data_lock():
                    report = schema.cleanup(self.data, apply=True, keep_lock=True)
            except RuntimeError as exc:
                raise ValueError(str(exc)) from None
            except (schema.DbError, OSError) as exc:
                raise ValueError('Не удалось удалить данные: %s' % exc) from None
        self.events_path.unlink(missing_ok=True)
        self.event_seq, self.event_seen, self.events_loaded = {}, set(), False
        self._import_plans = []
        body = report.preview.to_dict()
        body.update(applied=True, removed=list(report.removed),
                    failed=[{'name': name, 'error': error} for name, error in report.failed])
        return body

    def retention_policy_of(self, payload):
        payload = payload or {}
        include = payload.get('include')
        if include is None:
            include = ('observations', 'results')
        if isinstance(include, str):
            include = [item.strip() for item in include.split(',') if item.strip()]
        if not isinstance(include, (list, tuple)) or not include:
            raise ValueError('Выберите, что чистить: измерения, результаты или оба.')
        age = payload.get('max_age_seconds')
        if age in (None, ''):
            age = None
        else:
            try:
                age = float(age)
            except (TypeError, ValueError):
                raise ValueError('Срок хранения должен быть числом секунд.') from None
            if age < 0:
                raise ValueError('Срок хранения не может быть отрицательным.')
        keep = int(payload.get('keep_newest') or 0)
        if keep < 0:
            raise ValueError('keep_newest не может быть отрицательным.')
        return schema.RetentionPolicy(max_age_seconds=age,
                                      expired_only=bool(payload.get('expired_only', True)),
                                      include=tuple(include), keep_newest=keep)

    def retention_preview_view(self, payload=None):
        policy = self.retention_policy_of(payload or {})
        conn = self.read_connection()
        if conn is None:
            raise ValueError('Локальная база ещё не создана.')
        try:
            preview = schema.retention_preview(conn, policy)
        except (schema.DbError, sqlite3.Error) as exc:
            raise ValueError('Не удалось посчитать объём очистки: %s' % exc) from None
        finally:
            conn.close()
        return preview.to_dict()

    def retention_apply(self, payload):
        payload = payload or {}
        policy = self.retention_policy_of(payload)
        with self.mutex:
            if self.running():
                raise ValueError('Сначала остановите текущую операцию.')
            with self.writable_connection() as conn:
                try:
                    report = schema.apply_retention(conn, policy, vacuum=bool(payload.get('vacuum')))
                except (schema.RetentionError, schema.DbError, sqlite3.Error) as exc:
                    raise ValueError(str(exc)) from None
        return report.to_dict()

    def restore_preview_view(self, payload):
        payload = payload or {}
        source = str(payload.get('source') or '').strip()
        target = str(payload.get('target') or '').strip()
        if not source or not target:
            raise ValueError('Укажите, откуда восстанавливать и в какую папку.')
        try:
            return schema.restore_preview(source, target, reason='restore').to_dict()
        except schema.BackupError as exc:
            raise ValueError(str(exc)) from None
        except (OSError, schema.DbError) as exc:
            raise ValueError('Не удалось прочитать источник: %s' % exc) from None

    def restore_apply(self, payload):
        payload = payload or {}
        source = str(payload.get('source') or '').strip()
        target = str(payload.get('target') or '').strip()
        if not source or not target:
            raise ValueError('Укажите, откуда восстанавливать и в какую папку.')
        with self.mutex:
            if self.running():
                raise ValueError('Сначала остановите текущую операцию.')
            try:
                report = schema.restore(source, target, apply=True, reason='restore')
            except schema.BackupError as exc:
                raise ValueError(str(exc)) from None
            except (OSError, schema.DbError) as exc:
                raise ValueError('Не удалось восстановить: %s' % exc) from None
        return report.to_dict()

    def data_path_migrate(self, payload):
        """Move the data folder to a new place, with a backup of the old one."""
        payload = payload or {}
        new_path = str(payload.get('target') or '').strip()
        if not new_path:
            raise ValueError('Укажите новую папку данных.')
        apply = bool(payload.get('apply'))
        with self.mutex:
            if self.running():
                raise ValueError('Сначала остановите текущую операцию.')
            try:
                report = schema.migrate_data_path(self.data, new_path, apply=apply)
            except schema.BackupError as exc:
                raise ValueError(str(exc)) from None
            except (OSError, schema.DbError) as exc:
                raise ValueError('Не удалось перенести папку данных: %s' % exc) from None
        body = report.preview.to_dict()
        body['applied'] = bool(report.preview.applied)
        if apply:
            body['note'] = getattr(report, 'note', None)
            backup = getattr(report, 'backup', None)
            body['backup_path'] = str(getattr(backup, 'path', '') or '')
        return body

    # --- diagnostics (F10) -------------------------------------------------
    #
    # `diagnostics.py` knows how to read a run as a funnel -- what entered
    # each stage, what was lost and whose fault it was -- and how to explain an
    # empty result with one code and one action.  None of it had a route, so
    # the page had a progress bar and a log tail and the words "0 results"
    # with nothing behind them.  The routes below are thin: the counting, the
    # codes and the redaction are the module's.

    #: How many result rows the funnel reads.  The counters only need one row
    #: per address, and a sweep of 100k payloads would block the page.
    DIAGNOSTIC_ROWS = 20000
    #: Where a saved bundle may go: inside the data folder, by name.
    BUNDLE_NAME = re.compile(r'^[A-Za-z0-9._-]{1,80}\.json$')

    def diagnostic_rows(self, limit=None):
        """Stored result payloads, newest profile row per address, bounded."""
        limit = self.DIAGNOSTIC_ROWS if limit is None else max(0, int(limit))
        if limit <= 0:
            return []
        conn = self.read_connection()
        if conn is None:
            return []
        rows = []
        try:
            for (payload,) in conn.execute(
                    'SELECT payload FROM results ORDER BY checked_at DESC LIMIT ?', (limit,)):
                try:
                    parsed = json.loads(payload)
                except (TypeError, ValueError):
                    continue
                if isinstance(parsed, dict):
                    rows.append(parsed)
        except sqlite3.Error:
            rows = []
        finally:
            with suppress(sqlite3.Error):
                conn.close()
        return rows

    def diagnostic_funnel(self, rows=None, status=None, lang=None):
        """The run as counters, plus the one sentence that says what to do.

        `lang` reaches `explain_zero` as well as the loss list: without it the
        summary and the action came out in the *terminal* language while the
        line around them was in the page's, so a Russian page could read
        "The job ended with an error / Действие:".
        """
        from . import diagnostics as diag
        rows = self.diagnostic_rows() if rows is None else rows
        status = self.export_status() if status is None else status
        funnel = diag.build_funnel(rows, status=status)
        zero = diag.explain_zero(funnel, lang=lang)
        losses = [{'stage': stage, 'code': code, 'count': count,
                   'title': diag.code_title(code, lang), 'action': diag.code_action(code, lang)}
                  for stage, code, count in funnel.losses()[:12]]
        return funnel, zero, losses
    def diagnostics_view(self, query=None):
        """Counters of the last run, and an action for every lost address."""
        from . import diagnostics as diag
        query = query or {}
        lang = str((query.get('lang') or [''])[0] or '')[:2] or None
        rows = self.diagnostic_rows()
        status = self.export_status()
        funnel, zero, losses = self.diagnostic_funnel(rows, status, lang)
        known = []
        for code in {item['code'] for item in losses} | ({zero.code} if zero else set()):
            if not code or not diag.is_known_code(code):
                continue
            known.append({'code': code, 'stage': diag.code_stage(code),
                          'title': diag.code_title(code, lang), 'action': diag.code_action(code, lang)})
        return dict(
            stages=[{'stage': stage, **counters.to_dict()}
                    for stage, counters in funnel.stages.items()
                    if counters.entered or counters.lost],
            terminal=dict(funnel.terminal), verdicts=dict(funnel.verdicts),
            attribution=dict(funnel.attribution), totals=dict(funnel.totals),
            set_state=funnel.set_state, stop_reason=funnel.stop_reason,
            entry=funnel.entry(), lost=funnel.lost_total(),
            zero=zero.to_dict() if zero is not None else None,
            zero_text=zero.render(lang) if zero is not None else None,
            losses=losses, codes=known,
            rows_total=len(rows), sampled=min(len(rows), self.DIAGNOSTIC_ROWS),
            truncated=len(rows) > self.DIAGNOSTIC_ROWS,
            health=diag.health_report(
                version=PRODUCT_VERSION, schema_version=self.schema_version(),
                scope=dataclass_as_dict(self.snapshot_scope(status)),
                profile=self.active_profile_id() or None,
            ).to_dict(),
            environment=diag.local_environment(),
        )

    def schema_version(self):
        """`user_version` of the local database, or None when there is none."""
        conn = self.read_connection()  # a scalar read needs no row factory
        if conn is None:
            return None
        try:
            row = conn.execute('PRAGMA user_version').fetchone()
        except sqlite3.Error:
            return None
        finally:
            with suppress(sqlite3.Error):
                conn.close()
        return int(row[0]) if row else None

    def diagnostic_needles(self):
        """Every live secret of this process, for the canary sweep.

        The bundle is built from stored rows, and the stored form of a
        credential is a vault reference, not the value -- but "the schema does
        not hold it" is not the same proof as "the package does not carry it",
        so the finished payload is swept for these strings and refused if any
        of them survives.  The check is mechanical and runs on every save.
        """
        needles = {str(self.token), str(self.gateway_token)}
        conn = self.read_connection()
        if conn is not None:
            try:
                for (reference,) in conn.execute('SELECT secret_ref FROM accesses'):
                    if reference:
                        needles.add(str(reference))
            except sqlite3.Error:
                pass
            finally:
                with suppress(sqlite3.Error):
                    conn.close()
        return sorted(needles)

    def diagnostics_bundle(self, payload):
        """Preview, then save, the local diagnostic bundle. Never leaves the machine."""
        from . import diagnostics as diag
        from . import sourcedesk
        payload = payload or {}
        action = str(payload.get('action') or 'preview')
        rows = self.diagnostic_rows()
        status = self.export_status()
        funnel, zero, _losses = self.diagnostic_funnel(rows, status)
        try:
            bundle = diag.build_bundle(status=status, rows=rows, funnel=funnel, zero=zero)
        except (diag.ActionableError, ValueError, TypeError) as exc:
            raise ValueError('Не удалось собрать диагностический пакет: %s' % exc) from None
        leaks = sourcedesk.find_secret_leaks(bundle.payload, self.diagnostic_needles())
        if leaks:
            # A live secret in the package is the one failure mode that must
            # not produce a file.  The path is reported; the value is not.
            raise ValueError('Пакет содержит секрет и не сохранён. Путь: %s'
                             % ', '.join(sorted({path for path, _needle in leaks})))
        text = bundle.to_json()
        edited = payload.get('text')
        if action == 'save':
            if isinstance(edited, str) and edited.strip():
                # The user may delete anything before saving.  What comes back
                # is swept again, so an edit cannot smuggle a secret in either.
                try:
                    candidate = json.loads(edited)
                except json.JSONDecodeError as exc:
                    raise ValueError('Отредактированный пакет не является JSON: %s' % exc) from None
                recheck = sourcedesk.find_secret_leaks(candidate, self.diagnostic_needles())
                if recheck:
                    raise ValueError('Отредактированный пакет содержит секрет; сохранение отменено.')
                text = edited if edited.endswith('\n') else edited + '\n'
            name = str(payload.get('name') or '').strip()
            if not self.BUNDLE_NAME.fullmatch(name or ''):
                raise ValueError('Имя файла пакета: латиница, цифры, «-», «_» или «.», оканчивается на .json.')
            target = self.data/'diagnostics'/name
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding='utf-8')
            except OSError as exc:
                raise ValueError('Не удалось записать пакет: %s' % exc) from None
            return dict(saved=True, name=name, path=str(target), bytes=len(text.encode('utf-8')),
                        redactions=[note.to_dict() for note in bundle.redactions],
                        sample=bundle.sample_size, total=bundle.total_size,
                        truncated=bundle.truncated, text=text, canary_clean=True)
        return dict(saved=False, preview=bundle.preview(), text=text,
                    redactions=[note.to_dict() for note in bundle.redactions],
                    sample=bundle.sample_size, total=bundle.total_size,
                    truncated=bundle.truncated, describe=bundle.describe(),
                    canary_clean=True, canary_checked=len(self.diagnostic_needles()))

    # --- jobs (F11) --------------------------------------------------------
    #
    # `jobs.py` keeps a durable job, its items, structured progress and a
    # gapless event stream per job.  None of it had a route: the page showed
    # the worker log, so "пауза" was a word in a text box and the counters a
    # regex over it.  Everything below is read from the store, never parsed
    # out of a line of text.

    def job_store(self, workbench):
        return workbench.jobs()

    def jobs_view(self, query=None):
        """Every job with its structured progress, and one job's events."""
        query = query or {}
        wanted = str((query.get('id') or [''])[0] or '')
        after = str((query.get('after') or ['0'])[0] or '0')
        try:
            after_seq = max(0, int(after))
        except (TypeError, ValueError):
            after_seq = 0
        with core.Workbench(self.data) as workbench:
            store = self.job_store(workbench)
            rows = []
            for job in store.jobs(limit=50):
                progress = None
                try:
                    progress = store.progress(job.id).to_json()
                except (sqlite3.Error, ValueError, KeyError):
                    progress = None
                rows.append(dict(id=job.id, kind=job.kind, state=job.state,
                                 created_at=job.created_at, started_at=job.started_at,
                                 finished_at=job.finished_at, idempotency_key=job.idempotency_key,
                                 scope=job.scope.to_json() if hasattr(job.scope, 'to_json') else {},
                                 progress=progress))
            body = dict(jobs=rows, selected=wanted or None, events=[],
                        cursor=after_seq, states=jobs_state_names())
            if wanted:
                try:
                    body['detail'] = store.progress(wanted).to_json()
                except Exception:  # noqa: BLE001 - an unknown id is a message, not a crash
                    body['detail'] = None
                try:
                    body['items'] = [item.to_json() for item in store.items(wanted)[:200]]
                except Exception:  # noqa: BLE001
                    body['items'] = []
                body['events'] = [event.to_json() for event in store.events(wanted, after_seq=after_seq)]
                if body['events']:
                    body['cursor'] = body['events'][-1]['seq']
        return body

    def job_action(self, payload):
        """Pause, resume, cancel or retry one job, with the state it reached."""
        from . import jobs as jobs_module
        payload = payload or {}
        job_id = str(payload.get('id') or '').strip()
        action = str(payload.get('action') or '').strip()
        if action != 'recover' and not job_id:
            raise ValueError('Укажите задание.')
        with core.Workbench(self.data) as workbench:
            store = self.job_store(workbench)
            try:
                if action == 'pause':
                    job = store.pause(job_id, reason_code=str(payload.get('reason') or '') or None)
                elif action == 'resume':
                    job = store.resume(job_id)
                elif action == 'cancel':
                    job = store.cancel(job_id, reason_code=str(payload.get('reason') or '') or None)
                elif action == 'retry':
                    job = store.retry(job_id)
                elif action == 'recover':
                    report = store.recover()
                    return dict(action=action, recovery=report.to_json(),
                                jobs=[dict(id=item.id, kind=item.kind, state=item.state)
                                      for item in store.jobs(limit=50)])
                else:
                    raise ValueError('Неизвестное действие с заданием: ' + (action or '—'))
                progress = store.progress(job.id).to_json()
            except jobs_module.JobError as exc:
                raise ValueError(str(exc) or 'Задание отклонило действие.') from None
        return dict(action=action, id=job_id, state=job.state, progress=progress,
                    job=dict(id=job.id, kind=job.kind, state=job.state,
                             started_at=job.started_at, finished_at=job.finished_at))

    # --- desktop layer (F22) -----------------------------------------------
    #
    # `desktop.py` runs the menu bar, the single instance, the login item and
    # sleep/wake, and it keeps a journal of what it did.  The interface had no
    # route to any of it, so a user whose application was already running, or
    # who wanted it to start at login, had no way to see or change that from
    # where everything else is done.  The tokens in the instance record are
    # never read here: they are the control channel, not page content.

    def desktop_layout(self):
        from . import desktop
        try:
            return desktop.resolve_layout()
        except (desktop.DesktopError, OSError):
            return None

    def desktop_view(self):
        """What the background layer is, what it did and how it is set up."""
        from . import desktop
        from . import scheduler as scheduler_module
        body = dict(available=False, instance=None, autostart=None, journal=[],
                    environment=None, power=None, frozen=desktop.frozen())
        layout = self.desktop_layout()
        if layout is None:
            body['error'] = 'Фоновый слой на этой платформе недоступен.'
            return body
        body['available'] = True
        body['layout'] = layout.as_dict()
        # The interface can be pointed at a different folder with `--data`
        # than the one the background layer resolves.  Saying so is honest;
        # quietly reading another folder's journal would not be.
        body['layout_matches_interface'] = Path(layout.data).resolve() == self.data.resolve()
        with suppress(Exception):
            body['environment'] = desktop.describe_environment(layout)
        with suppress(Exception):
            body['autostart'] = dataclass_as_dict(desktop.autostart_status(layout))
        with suppress(Exception):
            instance = desktop.read_instance(layout)
            # `Instance` carries the control token and the control address:
            # they are how a second launch reaches this process, and a page
            # has no business holding either.
            body['instance'] = dict(pid=instance.pid, url=instance.url,
                                    started_at=instance.started_at, tray_pid=instance.tray_pid,
                                    version=instance.version, platform=instance.platform) \
                if instance is not None else None
        with suppress(Exception):
            body['journal'] = [dict(item) for item in desktop.read_journal(layout, limit=40)]
        with suppress(Exception):
            # Sleep, wake and the network are what the background layer exists
            # for; the identity of the current path is what it reacts to.
            body['power'] = scheduler_module.read_system_signal().to_dict()
            body['network'] = list(desktop.network_fingerprint())
        return body

    def desktop_action(self, payload):
        """Turn the login item on or off -- the only write here, and explicit."""
        from . import desktop
        payload = payload or {}
        action = str(payload.get('action') or '').strip()
        layout = self.desktop_layout()
        if layout is None:
            raise ValueError('Фоновый слой на этой платформе недоступен.')
        if action not in ('autostart-on', 'autostart-off'):
            raise ValueError('Неизвестное действие с фоновым слоем: ' + (action or '—'))
        try:
            if action == 'autostart-on':
                status = desktop.enable_autostart(layout)
            else:
                status = desktop.disable_autostart(layout)
        except (desktop.DesktopError, OSError) as exc:
            raise ValueError(str(exc) or 'Не удалось изменить автозапуск.') from None
        with suppress(Exception):
            desktop.journal(layout, 'gui.autostart', enabled=bool(status.enabled))
        return dict(action=action, autostart=dataclass_as_dict(status))

    def close(self):
        self.stop()
        if self._key_manager is not None:
            with suppress(sqlite3.Error, OSError):
                self._key_manager.conn.close()
            self._key_manager = None
        self._import_plans = []
        process = self.process
        if process and hasattr(process, 'wait'):
            try:
                process.wait(timeout=30)
            except (subprocess.TimeoutExpired, OSError):
                if hasattr(process, 'terminate'):
                    with suppress(OSError):
                        process.terminate()
                try:
                    process.wait(timeout=10)
                except (subprocess.TimeoutExpired, OSError):
                    # A worker stuck in native I/O must not keep the GUI lock forever.
                    if hasattr(process, 'kill'):
                        with suppress(OSError):
                            process.kill()
                        with suppress(subprocess.TimeoutExpired, OSError):
                            process.wait(timeout=5)
        with suppress(OSError):
            (self.data/'gui-selection.json').unlink(missing_ok=True)
        self.instance_lock.close()


class Handler(BaseHTTPRequestHandler):
    server_version = f'{PRODUCT_ID}/{PRODUCT_VERSION}'

    def log_message(self, *args):
        pass

    @property
    def app(self):
        return self.server.app

    # Answers a browser repolls twice a second: an unchanged state is a hash
    # the client revalidates with If-None-Match, so an idle bench costs the
    # server a digest and crosses the wire as an empty 304, and large
    # compressible answers ride gzip instead of raw bytes.
    COMPRESSIBLE = frozenset(('application/json', 'text/html', 'text/javascript', 'text/css'))

    def respond(self, code, payload, mime='application/json; charset=utf-8', *, etag=None, cache='no-store'):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload, ensure_ascii=False).encode()
        if etag and self.etag_matches(etag):
            return self.not_modified(etag, cache)
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Cache-Control', cache)
        if etag:
            self.send_header('ETag', f'"{etag}"')
            self.send_header('Vary', 'Accept-Encoding')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        body = payload
        if (len(payload) >= 1024 and mime.split(';')[0].strip() in self.COMPRESSIBLE
                and 'gzip' in (self.headers.get('Accept-Encoding') or '')):
            body = gzip.compress(payload, 6)
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def etag_matches(self, etag):
        header = self.headers.get('If-None-Match')
        if not header:
            return False
        for candidate in header.split(','):
            candidate = candidate.strip()
            if candidate == '*' or candidate.removeprefix('W/').strip('"') == etag:
                return True
        return False

    def not_modified(self, etag, cache='no-cache'):
        self.send_response(304)
        self.send_header('Cache-Control', cache)
        self.send_header('ETag', f'"{etag}"')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def serve_static(self, source, mime, *, substitutions=()):
        """A UI file with a content ETag: reloads revalidate instead of resending."""
        content = source.read_bytes()
        for old, new in substitutions:
            content = content.replace(old.encode(), new.encode())
        etag = hashlib.sha1(content).hexdigest()[:24]
        return self.respond(200, content, mime, etag=etag, cache='no-cache')

    def allowed(self, auth=True):
        host = self.headers.get('Host', '')
        expected = f'127.0.0.1:{self.server.server_port}'
        if host != expected:
            self.respond(403, dict(error='Неверный адрес приложения.'))
            return False
        origin = self.headers.get('Origin')
        if origin and origin != 'http://'+expected:
            self.respond(403, dict(error='Запрос с другого сайта отклонён.'))
            return False
        if auth and not secrets.compare_digest(self.headers.get('X-Workbench-Token',''), self.app.token):
            self.respond(403, dict(error='Обновите страницу приложения.'))
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path)
        # Language packs are public static strings (no user data) and are loaded
        # via a plain <script> tag, which cannot carry the session header.
        public = path.path in ('/', '/app.js', '/style.css', '/favicon.ico') or path.path.startswith('/i18n/')
        if not self.allowed(auth=not public):
            return
        try:
            if path.path == '/':
                content = (ROOT/'ui/index.html').read_text(encoding='utf-8').replace('__TOKEN__', self.app.token)
                content = content.replace('__PRODUCT_VERSION__', PRODUCT_VERSION)
                content = inject_source_compare(content)
                content = append_source_compare_script(content)
                # Asset URLs carry their file mtime: a changed style.css/app.js
                # produces a new URL, so browsers never serve a stale copy.
                try:
                    style_v = str(int((ROOT/'ui/style.css').stat().st_mtime))
                    js_v = str(int((ROOT/'ui/app.js').stat().st_mtime))
                except OSError:
                    style_v = js_v = PRODUCT_VERSION
                content = content.replace('__STYLE_V__', style_v).replace('__JS_V__', js_v)
                return self.respond(200, content.encode(), 'text/html; charset=utf-8')
            if path.path.startswith('/i18n/'):
                # Lazy-loaded UI language packs: ui/i18n/<code>.js.
                # The code is validated strictly, so no path parts can escape ui/i18n.
                code = path.path[len('/i18n/'):-3] if path.path.endswith('.js') else ''
                if not re.fullmatch(r'[a-z]{2,3}(-[A-Za-z]{2,4})?', code or ''):
                    return self.respond(404, dict(error='Нет такого языкового пакета.'))
                pack = ROOT/'ui'/'i18n'/(code + '.js')
                if not pack.is_file():
                    return self.respond(404, dict(error='Нет такого языкового пакета.'))
                return self.serve_static(pack, 'text/javascript; charset=utf-8')
            if path.path == '/app.js':
                return self.serve_static(ROOT/'ui'/'app.js', 'text/javascript; charset=utf-8',
                                         substitutions=(('__PRODUCT_VERSION__', PRODUCT_VERSION),))
            if path.path == '/style.css':
                return self.serve_static(ROOT/'ui'/'style.css', 'text/css; charset=utf-8')
            if path.path == '/favicon.ico':
                return self.respond(204, b'')
            if path.path == '/api/settings':
                return self.respond(200, self.app.settings())
            if path.path == '/api/geoip':
                return self.respond(200, self.app.geo_status())
            if path.path == '/api/defaults':
                return self.respond(200, defaults())
            if path.path == '/api/state':
                payload = json.dumps(self.app.state(), ensure_ascii=False).encode()
                etag = hashlib.sha1(payload).hexdigest()[:24]
                return self.respond(200, payload, etag=etag)
            query = parse_qs(path.query)
            if path.path == '/api/results':
                return self.respond(200, self.app.results(query))
            if path.path == '/api/results/matrix':
                return self.respond(200, self.app.matrix(query))
            if path.path == '/api/events':
                return self.respond(200, self.app.events(query))
            if path.path == '/api/annotations':
                return self.respond(200, dict(entries=self.app.annotation_state(),
                                              tags=self.app.tags_in_use()))
            if path.path == '/api/views':
                return self.respond(200, dict(views=self.app.saved_views()))
            if path.path == '/api/history':
                # The undo descriptor is internal: the page needs to know that
                # an action can be reversed, not how.
                entries = [{key: value for key, value in item.items() if key != 'undo'}
                           for item in (self.app.history.read().get('entries') or [])]
                return self.respond(200, dict(entries=entries[::-1][:20]))
            if path.path == '/api/result-detail':
                proxy = query.get('proxy', [''])[0]
                return self.respond(200, self.app.detail(proxy))
            if path.path == '/api/source-catalog':
                return self.respond(200, self.app.source_view(query))
            if path.path == '/api/collections':
                return self.respond(200, self.app.collections(query))
            if path.path == '/api/collections/members':
                return self.respond(200, self.app.collection_members(
                    {'collection': query.get('collection', [''])[0]}))
            if path.path == '/api/service-catalog':
                return self.respond(200, self.app.catalog(query))
            if path.path == '/api/service-catalog/set':
                return self.respond(200, self.app.service_set_detail(
                    {'set': query.get('set', [''])[0]}))
            if path.path.startswith('/api/source-catalog/'):
                source_id = path.path[len('/api/source-catalog/'):]
                return self.respond(200, self.app.source_row(source_id))
            if path.path == '/api/sources/scope':
                return self.respond(200, self.app.scope_exclusions(query))
            if path.path == '/api/sources/compare':
                return self.respond(200, self.app.source_comparison(self.query_body(query)))
            if path.path == '/api/sources/update-status':
                return self.respond(200, self.app.catalog_update_status())
            if path.path == '/api/api-keys':
                return self.respond(200, self.app.keys_view(
                    {'admin_secret': (query.get('admin_secret') or [''])[0]}))
            if path.path == '/api/import/batches':
                return self.respond(200, self.app.import_batches(query))
            if path.path == '/api/pools':
                return self.respond(200, self.app.pools_view(query))
            if path.path == '/api/schedules':
                return self.respond(200, self.app.schedules_view(query))
            if path.path == '/api/gateway/options':
                return self.respond(200, self.app.gateway_options())
            if path.path == '/api/diagnostics':
                return self.respond(200, self.app.diagnostics_view(query))
            if path.path == '/api/jobs':
                return self.respond(200, self.app.jobs_view(query))
            if path.path == '/api/desktop':
                return self.respond(200, self.app.desktop_view())
            if path.path == '/api/maintenance/cleanup-preview':
                return self.respond(200, self.app.cleanup_preview_view())
            if path.path == '/api/maintenance/retention-preview':
                return self.respond(200, self.app.retention_preview_view(self.query_body(query)))
            if path.path.startswith('/api/download/'):
                name = path.path.rsplit('/', 1)[1]
                if name not in DOWNLOADS:
                    return self.respond(404, dict(error='Файл не найден.'))
                # Stream exports so a large JSON does not fill server memory.
                # Defect 5: no writer lock here.  The generation is resolved once
                # and the file is immutable, so a concurrent publication cannot
                # mix two snapshots and a running scan cannot block the download.
                snapshot = self.app.snapshot()
                status = self.app.export_status(snapshot)
                if not status or not status.get('published'):
                    return self.respond(409, dict(error='Нет опубликованного снимка экспорта.'))
                if status.get('state') == 'error':
                    return self.respond(409, dict(error='Снимок экспорта завершился ошибкой.'))
                empty_at_publish = (status.get('empty_export') is True and status.get('exported') == 0)
                if status.get('stale') and not (empty_at_publish and name in EMPTY_SAFE_DOWNLOADS):
                    return self.respond(410, dict(error='Снимок экспорта устарел; выполните перепроверку.'))
                try:
                    target = core.export_file(self.app.data/'exports', name, generation=snapshot.generation)
                    with target.open('rb') as handle:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/octet-stream')
                        self.send_header('Content-Length', str(os.fstat(handle.fileno()).st_size))
                        self.send_header('Content-Disposition', f'attachment; filename="{name}"')
                        self.send_header('Cache-Control', 'no-store')
                        self.end_headers()
                        while chunk := handle.read(DOWNLOAD_CHUNK):
                            self.wfile.write(chunk)
                except OSError:
                    return self.respond(409, dict(error='Файл снимка недоступен; выполните проверку заново.'))
                return
            self.respond(404, dict(error='Не найдено.'))
        except ValueError as exc:
            # A rejected control is a message with an action in it, not a
            # generic "try again" (F25).
            message = str(exc) if str(exc) and not str(exc).startswith(('0x', 'not')) else \
                'Неверные параметры запроса.'
            self.respond(400, dict(error=message))
        except (OSError, sqlite3.Error):
            self.respond(400, dict(error='Не удалось прочитать данные. Повторите после завершения операции.'))
        except (apikeys.ApiKeyError, schema.DbError) as exc:
            self.respond(400, dict(error=str(exc) or 'Операция отклонена.'))
        except (KeyError, TypeError, AttributeError, IndexError, RecursionError,
                OverflowError) as exc:
            # A row the reader could not build is a reported error, not a
            # dropped connection.  The GET handler named only ValueError, so a
            # stored NULL or a malformed field raised past every handler, the
            # thread died mid-response and the client saw a protocol error
            # instead of a sentence it could show.
            self.respond(400, dict(error='Не удалось прочитать данные: %s'
                                   % (type(exc).__name__,)))

    def query_body(self, query):
        """Query parameters as a payload, for the reads that take options.

        Every value is a list because `parse_qs` says so; the first one is the
        only one a single-valued option can have, and a repeated one is the
        caller's mistake rather than something to guess at.
        """
        body = {}
        for name, values in (query or {}).items():
            if not isinstance(values, (list, tuple)) or not values:
                continue
            text = values[0]
            if text in ('true', 'false'):
                body[name] = text == 'true'
            elif text.lstrip('-').isdigit():
                body[name] = int(text)
            else:
                body[name] = text
        return body

    def do_POST(self):
        if not self.allowed():
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_BODY:
                return self.respond(413, dict(error='Слишком большой запрос.'))
            payload = json.loads(self.rfile.read(length))
            path = urlsplit(self.path).path
            if path == '/api/settings':
                return self.respond(200, self.app.save(payload))
            if path == '/api/settings/export':
                return self.respond(200, self.app.export_settings(payload))
            if path == '/api/start':
                return self.respond(200, self.app.start(payload))
            if path == '/api/sources/prune':
                return self.respond(200, self.app.prune_sources(payload))
            if path == '/api/collections':
                return self.respond(200, self.app.create_collection(payload))
            if path == '/api/collections/members':
                return self.respond(200, self.app.add_collection_members(payload))
            if path == '/api/collections/member-remove':
                return self.respond(200, self.app.remove_collection_member(payload))
            if path == '/api/service-catalog/apply':
                return self.respond(200, self.app.apply_service_set(payload))
            if path == '/api/service-catalog/save':
                return self.respond(200, self.app.save_service_set(payload))
            if path == '/api/service-catalog/delete':
                return self.respond(200, self.app.delete_service_set(payload))
            if path == '/api/service-catalog/update':
                return self.respond(200, self.app.update_service_set(payload))
            if path == '/api/sources/update':
                return self.respond(200, self.app.update_sources(payload))
            if path == '/api/sources/set':
                return self.respond(200, self.app.apply_set(payload))
            if path == '/api/sources/toggle':
                return self.respond(200, self.app.toggle_source(payload))
            if path == '/api/sources/select':
                return self.respond(200, self.app.select_source(payload))
            if path == '/api/sources/remove':
                return self.respond(200, self.app.remove_source(payload))
            if path == '/api/sources/recover':
                return self.respond(200, self.app.recover_source(payload))
            if path == '/api/sources/add':
                return self.respond(200, self.app.add_source_url(payload))
            if path in ('/api/sources/preview', '/api/sources/check'):
                return self.respond(200, self.app.preview_source(payload))
            if path == '/api/sources/exclude-scope':
                return self.respond(200, self.app.exclude_source_scope(payload))
            if path == '/api/sources/scope/clear':
                return self.respond(200, self.app.clear_scope_exclusions(payload))
            if path == '/api/sources/refresh':
                return self.respond(200, self.app.refresh_catalog(payload))
            if path == '/api/geoip/update':
                return self.respond(200, self.app.update_geo())
            if path == '/api/clear-data':
                return self.respond(200, self.app.clear_data())
            if path == '/api/stop':
                return self.respond(200, self.app.stop())
            if path == '/api/test-proxy':
                return self.respond(200, self.app.test_proxy(payload))
            if path == '/api/denylist/add':
                return self.respond(200, self.app.add_denylist(payload))
            if path == '/api/results/bulk':
                return self.respond(200, self.app.bulk(payload))
            if path == '/api/views':
                return self.respond(200, self.app.store_view(payload))
            if path == '/api/views/delete':
                return self.respond(200, self.app.delete_view(payload))
            if path == '/api/history/undo':
                return self.respond(200, self.app.undo_history(payload))
            if path == '/api/gateway/start':
                return self.respond(200, self.app.start_gateway())
            if path == '/api/gateway/stop':
                return self.respond(200, self.app.stop_gateway())
            if path == '/api/gateway/config':
                return self.respond(200, self.app.gateway_configure(payload))
            if path == '/api/api-keys':
                return self.respond(200, self.app.key_create(payload))
            if path == '/api/api-keys/bootstrap':
                return self.respond(200, self.app.key_bootstrap(payload))
            if path == '/api/api-keys/action':
                return self.respond(200, self.app.key_action(payload))
            if path == '/api/import/preview':
                return self.respond(200, self.app.import_preview(payload))
            if path == '/api/import/commit':
                return self.respond(200, self.app.import_commit(payload))
            if path == '/api/pools/create':
                return self.respond(200, self.app.pool_create(payload))
            if path == '/api/pools/action':
                return self.respond(200, self.app.pool_action(payload))
            if path == '/api/schedules/action':
                return self.respond(200, self.app.schedule_action(payload))
            if path == '/api/diagnostics/bundle':
                return self.respond(200, self.app.diagnostics_bundle(payload))
            if path == '/api/jobs/action':
                return self.respond(200, self.app.job_action(payload))
            if path == '/api/desktop/action':
                return self.respond(200, self.app.desktop_action(payload))
            if path == '/api/maintenance/cleanup':
                return self.respond(200, self.app.cleanup_apply(payload))
            if path == '/api/maintenance/retention':
                return self.respond(200, self.app.retention_apply(payload))
            if path == '/api/maintenance/restore-preview':
                return self.respond(200, self.app.restore_preview_view(payload))
            if path == '/api/maintenance/restore':
                return self.respond(200, self.app.restore_apply(payload))
            if path == '/api/maintenance/data-path':
                return self.respond(200, self.app.data_path_migrate(payload))
            self.respond(404, dict(error='Не найдено.'))
        except (ValueError, KeyError, TypeError, AttributeError, IndexError,
                RecursionError, OverflowError) as exc:
            message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else 'Проверьте поля настроек.'
            self.respond(400, dict(error=message))
        except (importer.ImportProblem, apikeys.ApiKeyError, schema.DbError) as exc:
            # The modules speak in stable codes; the page needs the sentence,
            # not the code, but it must never see a bare exception class.
            self.respond(400, dict(error=str(exc) or getattr(exc, 'code', '') or
                                   'Операция отклонена.'))
        except sqlite3.Error as exc:
            self.respond(400, dict(error='База данных занята другой операцией: %s' % exc))
        except OSError:
            self.respond(500, dict(error='Не удалось записать настройки. Проверьте доступ к папке data.'))


def make_server(data, port=0):
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    try:
        server.app = App(data)
    except Exception:
        server.server_close()
        raise
    return server


def main(argv=None):
    utf8_output()
    parser = argparse.ArgumentParser(description=tr(f'Локальный интерфейс {PRODUCT_NAME}', f'{PRODUCT_NAME} local interface'))
    parser.add_argument('--version', action='version', version=f'{PRODUCT_NAME} {PRODUCT_VERSION}')
    parser.add_argument('--data', type=Path, default=paths.default_data())
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--api-port', type=int, default=api.DEFAULT_PORT,
                        help=tr('порт локального API для своих программ (только этот компьютер)', 'port of the local API for your programs (this computer only)'))
    parser.add_argument('--no-api', action='store_true', help=tr('не запускать локальное API', 'do not start the local API'))
    parser.add_argument('--gateway-port', type=int, default=gateway.DEFAULT_PORT,
                        help=tr('порт ротирующего прокси (только этот компьютер)', 'port of the rotating proxy (this computer only)'))
    parser.add_argument('--gateway-host', default=os.environ.get('PROXY_WORKBENCH_GATEWAY_HOST', '127.0.0.1'),
                        help=tr('адрес ротирующего прокси; по умолчанию только этот компьютер',
                                'rotating proxy bind address; this computer only by default'))
    parser.add_argument('--lan', action='store_true',
                        help=tr('открыть ротирующий прокси в локальной сети для телефона (явное действие)',
                                'expose the rotating proxy on the local network for a phone (explicit opt-in)'))
    parser.add_argument('--gateway-interface', default=os.environ.get('PROXY_WORKBENCH_GATEWAY_INTERFACE'),
                        help=tr('адрес LAN-адаптера для --lan; без --lan он отвергается, а не игнорируется',
                                'LAN adapter address for --lan; without --lan it is refused, not ignored'))
    parser.add_argument('--gateway-token', default=os.environ.get('PROXY_WORKBENCH_GATEWAY_TOKEN'),
                        help=tr('пароль ротирующего прокси; свой для каждого запуска, не пароль интерфейса',
                                'rotating proxy password; its own per run, never the interface password'))
    parser.add_argument('--no-gateway', action='store_true', help=tr('не запускать ротирующий прокси', 'do not start the rotating proxy'))
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        server = make_server(args.data, args.port)
    except OSError:
        old = read_json(args.data/'gui-address.json', {})
        if old.get('port') and type(old['port']) is int and 1 <= old['port'] <= 65535:
            url = f"http://127.0.0.1:{old['port']}/"
            try:
                import httpx
                response = httpx.get(url, timeout=2, trust_env=False)
                if response.status_code == 200 and PRODUCT_NAME in response.text:
                    print(tr(f'Приложение уже запущено: {url}', f'The application is already running: {url}'), flush=True)
                    if not args.no_browser:
                        webbrowser.open(url)
                    return
            except httpx.HTTPError:
                pass
        raise SystemExit('Не удалось открыть интерфейс: папка data или порт уже используются.')
    core.atomic(args.data/'gui-address.json', json.dumps(dict(port=server.server_port)))
    url = f'http://127.0.0.1:{server.server_port}/'
    print(tr(f'{PRODUCT_NAME} {PRODUCT_VERSION}: {url}\nНе закрывайте это окно, пока работает приложение. Ctrl+C — закрыть.', f'{PRODUCT_NAME} {PRODUCT_VERSION}: {url}\nKeep this window open while you use the application. Ctrl+C closes it.'), flush=True)
    api_server = None
    if not args.no_api:
        try:
            api_server = api.make_api_server(args.data, '127.0.0.1', args.api_port)
        except (OSError, ValueError):
            print(tr(f'Локальное API не запущено: порт {args.api_port} занят.', f'Local API not started: port {args.api_port} is busy.'), flush=True)
        else:
            server.app.api_url = f'http://127.0.0.1:{api_server.server_port}'
            threading.Thread(target=api_server.serve_forever, daemon=True).start()
            # The old line named the deprecated `/proxies` and said nothing about
            # `/v1` or about the key that door needs, so a user who launched the
            # application without arguments was never told how to get in (F29).
            print(tr(f'API для своих программ: {server.app.api_url}/v1 '
                     f'(ключ — на странице «Ключи» в интерфейсе, {url}#keys)',
                     f'API for your programs: {server.app.api_url}/v1 '
                     f'(get a key on the Keys page, {url}#keys)'), flush=True)
            for line in server.app.api_key_banner():
                print(line, flush=True)
    if not args.no_gateway:
        server.app.gateway_bind = dict(host=args.gateway_host, port=args.gateway_port)
        # `--lan` used to print a warning and then die in the argument list:
        # the listener never heard about it, so a phone could not connect and
        # the flag was a lie.  Both values now travel to `Background`, which
        # is what `start_gateway` does too, so the button on the connect page
        # and the command line build the same listener.
        server.app.gateway_lan = bool(args.lan) or not api.is_loopback(args.gateway_host)
        server.app.gateway_interface = args.gateway_interface or None
        if server.app.gateway_interface and not server.app.gateway_lan:
            print(tr('Интерфейс LAN задан без --lan; интерфейс игнорируется.',
                     'A LAN interface was given without --lan; it is ignored.'), flush=True)
            server.app.gateway_interface = None
        gateway_token = args.gateway_token
        if not gateway_token:
            # The gateway password is generated per GUI instance and is never
            # persisted in settings, files or logs.  It is a different identity
            # from the GUI session token, so a phone profile and a QR code can
            # never carry the secret that administers this interface.
            gateway_token = server.app.gateway_token
        if args.lan and api.is_loopback(args.gateway_host):
            # LAN mode is an explicit opt-in with a visible state (defect 18).
            print(tr('Внимание: ротирующий прокси открыт в локальной сети; пароль доступа есть в QR.',
                     'Warning: the rotating proxy is open on the local network; its password is in the QR.'), flush=True)
        try:
            server.app.gateway = gateway.Background(args.data, args.gateway_host, args.gateway_port,
                                                    token=gateway_token,
                                                    bind=server.app.gateway_binding(),
                                                    lan=server.app.gateway_lan,
                                                    interface=server.app.gateway_interface)
        except (OSError, ValueError) as exc:
            print(tr(f'Ротирующий прокси не запущен: {exc}. Проверьте порт и адрес.',
                     f'Rotating proxy not started: {exc}. Check the port and bind address.'), flush=True)
        else:
            gateway_state = server.app.gateway_state()
            print(tr(f'Ротирующий прокси: {gateway_state["address"]} (HTTP и SOCKS5, только TCP)',
                     f'Rotating proxy: {gateway_state["address"]} (HTTP and SOCKS5, TCP only)'), flush=True)
            if gateway_state.get('lan'):
                print(tr(f'Слушает на {gateway_state["listen_host"]}; доступен в локальной сети.',
                         f'Listening on {gateway_state["listen_host"]}; reachable on the local network.'), flush=True)
            if gateway_state['mobile_ready']:
                print(tr('LAN-шлюз включён: QR содержит пароль; разрешите порт в брандмауэре.',
                         'LAN gateway enabled: the QR contains the password; allow the port in the firewall.'), flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if api_server:
            api_server.shutdown()
            api_server.server_close()
        if getattr(server.app, 'gateway', None):
            server.app.gateway.close()
        server.app.close()
        server.server_close()


if __name__ == '__main__':
    main()
