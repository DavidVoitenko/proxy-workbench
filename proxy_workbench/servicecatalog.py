"""Service catalog: versioned service presets and the sets that combine them.

Closes F06 (docs/requirements/MASTER-PROMPT.ru.md:178) and defect 24
(MASTER-PROMPT.ru.md:136).  A preset states exactly what one HTTP probe proves.
Three rules are enforced by the parser, not left to the author:

* ``not_proved`` is mandatory and never empty, so no set title can promise calls,
  4K or a whole service.
* Capability names are distinct (status endpoint / homepage / API / WebSocket /
  media / long connection).  A preset that claims a capability this engine cannot
  measure is rejected instead of being published as a passing check.
* Every set declares the complete field inventory of a scenario.  Anything not
  declared is a user field and is never touched, so switching from an Elite
  scenario to a video one cannot leave a judge, a strict flag or an anonymity
  requirement behind (defect 24).

The catalog is data only.  It opens no socket, reads no database and writes no
file: measurement belongs to ``probes.py`` and persistence to ``profiles.py``.
A pinned snapshot (:class:`PinnedSet`) carries its own copy of the preset
definitions, so shipping a new manifest can never change an already saved
profile revision.

Public API
----------
``load_catalog``, ``parse_catalog``, ``search_presets``, ``select_presets``,
``build_targets``, ``pin_set``, ``preview_update``, ``upgrade_set``,
``apply_scenario``, ``new_user_set``, ``SCENARIO_DEFAULTS``.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field as dc_field
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from .i18n import tr

SCHEMA_VERSION = 1
"""Manifest schema this module can read.  A newer file is refused, not patched."""

DEFAULT_MANIFEST_PATH = Path(__file__).with_name('data') / 'service_sets.json'

# The scanner validates probe headers against this list (proxytool.py:43); a preset
# may not smuggle an Authorization header past the catalog.
SAFE_PROBE_HEADERS = frozenset({'accept', 'accept-encoding', 'accept-language', 'cache-control',
                                'pragma', 'user-agent', 'x-client-version', 'x-request-id'})

# The surfaces cap a check at 20 targets (gui.py:175), so a multi-selection larger
# than that cannot be stored and is refused here instead of failing later.
MAX_TARGETS_PER_SELECTION = 20

# Words that turn a probe into a promise.  Checked against every title, because
# defect 24 is exactly a title that promises more than the probe measures.
FORBIDDEN_PROMISE_PATTERNS = (
    r'\b4k\b', r'\b1080p\b', r'\bultra ?hd\b', r'\bзвонк\w*', r'\bголосов\w*', r'\bполностью\b',
    r'\bвесь сервис\b', r'\bполный сервис\b', r'\bвсё сервиса\b', r'\bработает как\b',
    r'\bгарантиру\w*', r'\bfull service\b', r'\bwhole service\b', r'\bcomplete service\b',
    r'\bguarantee[sd]?\b', r'\bworks completely\b', r'\bhd\b',
)

# Descriptions get a narrower rule.  A set must be allowed to say "does not prove
# 4K": that sentence is the required disclosure, not a promise.  What stays
# forbidden there is a claim on the whole service.
FORBIDDEN_SERVICE_CLAIM_PATTERNS = (
    r'\bвесь сервис\b', r'\bвсё сервиса\b', r'\bполный сервис\b', r'\bполная работа\b',
    r'\bработает полностью\b', r'\bworks completely\b', r'\bfull service\b',
)

# Canonical "off" value per scenario field.  This is the explicit reset policy of
# defect 24: a set that writes null for a field resets it to exactly this value
# instead of letting the previous scenario's value survive.  tests/test_servicecatalog_scenario.py
# compares this table with gui.defaults() so a surface-side default change fails loudly.
SCENARIO_DEFAULTS = {
    'attempts': 3,
    'connect_timeout': 4.0,
    'timeout': 8.0,
    'max_bytes': 1048576,
    'min_success': 2 / 3,
    'fail_fast': True,
    'request_profile': 'workbench',
    'protocol': 'all',
    'prefilter': 512,
    'anonymity.judge_url': '',
    'min_anonymity': 'any',
    'reputation.local_enabled': True,
    'reputation.dnsbl_enabled': False,
    'reputation.dnsbl_zones': [],
    'reputation.strict': False,
    'speedtest.url': '',
    'speedtest.max_bytes': 5_000_000,
}

COMBINATION_IDS = ('all', 'any', 'at_least', 'none')
OWNERSHIP_IDS = ('scenario', 'user')
VERIFICATION_IDS = ('documented', 'legacy_definition', 'unverified_live')
ORIGIN_IDS = ('legacy_app_js', 'researched', 'user')

_ISO_DATE = re.compile(r'\d{4}-\d{2}-\d{2}')
_SHA256 = re.compile(r'[a-fA-F0-9]{64}')
_ID = re.compile(r'[a-z0-9][a-z0-9._-]{0,63}')


class CatalogError(ValueError):
    """Base class for every catalog rejection, so a caller can catch one type."""


class ManifestError(CatalogError):
    """The manifest is unreadable, of a different schema, or internally inconsistent."""


def _fail(message):
    raise ManifestError(message)


def _require(mapping, key, kind, where):
    if not isinstance(mapping, dict) or key not in mapping:
        _fail(f'{where}: нет поля {key!r}')
    value = mapping[key]
    if not isinstance(value, kind) or isinstance(value, bool) and kind is not bool:
        _fail(f'{where}.{key}: ожидается {getattr(kind, "__name__", kind)}')
    return value


def _require_text(mapping, key, where, *, allow_empty=False):
    value = _require(mapping, key, str, where)
    if not allow_empty and not value.strip():
        _fail(f'{where}.{key}: ожидается непустая строка')
    return value


def _require_id(value, where):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _fail(f'{where}: идентификатор {value!r} должен быть в нижнем регистре latin[a-z0-9-]')
    return value


def _require_text_list(value, where, *, allow_empty=True):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        _fail(f'{where}: ожидается список непустых строк')
    if not allow_empty and not value:
        _fail(f'{where}: список не может быть пустым')
    return list(value)


def _require_date(value, where, *, allow_none=False):
    if value is None:
        if allow_none:
            return None
        _fail(f'{where}: дата обязательна')
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        _fail(f'{where}: ожидается дата в формате YYYY-MM-DD')
    try:
        date.fromisoformat(value)
    except ValueError:
        _fail(f'{where}: {value!r} не является датой')
    return value


def _no_promise(*texts, narrow=False):
    """Reject a user visible string that promises more than the probe measures."""
    patterns = FORBIDDEN_SERVICE_CLAIM_PATTERNS if narrow else FORBIDDEN_PROMISE_PATTERNS
    for text in texts:
        if not isinstance(text, str):
            continue
        for pattern in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                _fail(tr(f'название или описание обещает больше, чем проверяет проба: {pattern}',
                         f'title or description promises more than the probe measures: {pattern}'))


def _digest(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:20]


def _copy(value):
    return copy.deepcopy(value)


@dataclass(frozen=True)
class Probe:
    """One HTTP request with an explicit pass condition.

    The field set is exactly what ``proxytool.validate_targets`` accepts
    (proxytool.py:947-967).  There is no ``not_contains``: a field the engine
    ignores would be exactly the silent parameter drop F07 forbids.
    """

    probe_id: str
    url: str
    method: str
    statuses: tuple
    contains: str | None
    sha256: str | None
    headers: tuple
    description_ru: str
    description_en: str

    def to_target(self, name: str) -> dict:
        """Return the target dict the scanner validates, named for the UI."""
        return {'name': name, 'url': self.url, 'method': self.method, 'statuses': list(self.statuses),
                'contains': self.contains, 'sha256': self.sha256, 'headers': dict(self.headers)}

    def pass_condition_ru(self) -> str:
        parts = [f'HTTP {"/".join(str(code) for code in self.statuses)} на {self.url}']
        if self.contains is not None:
            parts.append(f'тело содержит {self.contains!r}')
        if self.sha256 is not None:
            parts.append('SHA-256 тела совпадает')
        return '; '.join(parts)

    def to_dict(self) -> dict:
        return {'id': self.probe_id, 'url': self.url, 'method': self.method, 'statuses': list(self.statuses),
                'contains': self.contains, 'sha256': self.sha256, 'headers': dict(self.headers),
                'description_ru': self.description_ru, 'description_en': self.description_en}


@dataclass(frozen=True)
class Cost:
    """What one pass of this preset costs, so a set can be compared with a budget."""

    requests_per_pass: int
    max_bytes: int
    budget_weight: float
    rate_limit_ru: str
    rate_limit_en: str

    def to_dict(self) -> dict:
        return {'requests_per_pass': self.requests_per_pass, 'max_bytes': self.max_bytes,
                'budget_weight': self.budget_weight, 'rate_limit_ru': self.rate_limit_ru,
                'rate_limit_en': self.rate_limit_en}


@dataclass(frozen=True)
class Capability:
    """A distinct kind of evidence.  Names are never reused for a different kind."""

    capability_id: str
    title_ru: str
    title_en: str
    proves_ru: str
    proves_en: str
    not_proved_ru: str
    not_proved_en: str
    probeable: bool
    not_probeable_reason_ru: str | None = None
    not_probeable_reason_en: str | None = None


@dataclass(frozen=True)
class Category:
    category_id: str
    title_ru: str
    title_en: str
    description_ru: str
    description_en: str

    def to_dict(self) -> dict:
        # ``Catalog.summary()`` is the description the GUI, the CLI and the API
        # read, so a category has to be as serialisable as every other record.
        return {'id': self.category_id, 'title_ru': self.title_ru, 'title_en': self.title_en,
                'description_ru': self.description_ru, 'description_en': self.description_en}


@dataclass(frozen=True)
class Preset:
    """A single service check: id, version, maintainer, probes and what it does not prove."""

    preset_id: str
    version: int
    maintainer: str
    title_ru: str
    title_en: str
    aliases: tuple
    categories: tuple
    capability: str
    origin: str
    probes: tuple
    pass_condition_ru: str
    pass_condition_en: str
    not_proved_ru: tuple
    not_proved_en: tuple
    limitations_ru: tuple
    limitations_en: tuple
    cost: Cost
    definition_checked_on: str
    live_checked_on: str | None
    verification: str
    definition_source: dict
    contract_source: str
    deprecated: bool
    deprecated_reason_ru: str | None
    user_owned: bool

    @property
    def digest(self) -> str:
        return _digest(self.definition())

    @property
    def probeable(self) -> bool:
        return bool(self.probes)

    def definition(self) -> dict:
        """Everything a measurement depends on.  A change here must not be silent."""
        return {'id': self.preset_id, 'version': self.version, 'maintainer': self.maintainer,
                'title_ru': self.title_ru, 'title_en': self.title_en, 'aliases': list(self.aliases),
                'categories': list(self.categories), 'capability': self.capability, 'origin': self.origin,
                'probes': [probe.to_dict() for probe in self.probes],
                'pass_condition_ru': self.pass_condition_ru, 'pass_condition_en': self.pass_condition_en,
                'not_proved_ru': list(self.not_proved_ru), 'not_proved_en': list(self.not_proved_en),
                'limitations_ru': list(self.limitations_ru), 'limitations_en': list(self.limitations_en),
                'cost': self.cost.to_dict(), 'definition_checked_on': self.definition_checked_on,
                'live_checked_on': self.live_checked_on, 'verification': self.verification,
                'definition_source': dict(self.definition_source), 'contract_source': self.contract_source,
                'deprecated': self.deprecated, 'deprecated_reason_ru': self.deprecated_reason_ru,
                'user_owned': self.user_owned}

    def describe(self) -> str:
        return (f'{self.title_ru} [{self.capability}] — {self.pass_condition_ru}. '
                f'Не доказывает: {"; ".join(self.not_proved_ru)}')

    def to_dict(self) -> dict:
        payload = self.definition()
        payload['digest'] = self.digest
        return payload


@dataclass(frozen=True)
class ServiceSet:
    """A named combination of presets plus the complete field inventory it applies."""

    set_id: str
    version: int
    maintainer: str
    title_ru: str
    title_en: str
    description_ru: str
    description_en: str
    required: tuple
    optional: tuple
    combination: str
    min_passes: int
    scenario: dict
    origin: str

    @property
    def preset_ids(self) -> tuple:
        return tuple(self.required) + tuple(self.optional)

    @property
    def digest(self) -> str:
        return _digest(self.definition())

    def definition(self) -> dict:
        return {'id': self.set_id, 'version': self.version, 'maintainer': self.maintainer,
                'title_ru': self.title_ru, 'title_en': self.title_en,
                'description_ru': self.description_ru, 'description_en': self.description_en,
                'required': list(self.required), 'optional': list(self.optional),
                'combination': self.combination, 'min_passes': self.min_passes,
                'scenario': _copy(self.scenario), 'origin': self.origin}

    def to_dict(self) -> dict:
        payload = self.definition()
        payload['digest'] = self.digest
        return payload


@dataclass(frozen=True)
class Catalog:
    """An immutable, validated manifest.  Use :func:`load_catalog` to build one."""

    schema_version: int
    manifest_version: int
    maintainer: str
    generated_at: str
    categories: tuple
    capabilities: tuple
    presets: tuple
    service_sets: tuple
    field_scope: tuple

    def __post_init__(self):
        object.__setattr__(self, '_preset_index',
                           {preset.preset_id: preset for preset in self.presets})
        object.__setattr__(self, '_set_index',
                           {item.set_id: item for item in self.service_sets})
        object.__setattr__(self, '_category_index',
                           {item.category_id: item for item in self.categories})
        object.__setattr__(self, '_capability_index',
                           {item.capability_id: item for item in self.capabilities})

    @property
    def preset_index(self) -> dict:
        return dict(self._preset_index)

    @property
    def set_index(self) -> dict:
        return dict(self._set_index)

    @property
    def category_index(self) -> dict:
        return dict(self._category_index)

    @property
    def capability_index(self) -> dict:
        return dict(self._capability_index)

    @property
    def digest(self) -> str:
        return _digest({preset.preset_id: preset.digest for preset in self.presets})

    def scenario_fields(self) -> tuple:
        return tuple(item['field'] for item in self.field_scope if item['ownership'] == 'scenario')

    def derived_fields(self) -> tuple:
        return tuple(item['field'] for item in self.field_scope
                     if item['ownership'] == 'scenario' and item.get('derived'))

    def user_fields(self) -> tuple:
        return tuple(item['field'] for item in self.field_scope if item['ownership'] == 'user')

    def preset(self, preset_id: str) -> Preset:
        try:
            return self._preset_index[preset_id]
        except KeyError:
            raise CatalogError(tr(f'в каталоге нет сервиса {preset_id!r}',
                                 f'no service {preset_id!r} in the catalog')) from None

    def service_set(self, set_id: str) -> ServiceSet:
        try:
            return self._set_index[set_id]
        except KeyError:
            raise CatalogError(tr(f'в каталоге нет набора {set_id!r}',
                                 f'no set {set_id!r} in the catalog')) from None

    def capability(self, capability_id: str) -> Capability:
        try:
            return self._capability_index[capability_id]
        except KeyError:
            raise CatalogError(tr(f'неизвестная способность {capability_id!r}',
                                 f'unknown capability {capability_id!r}')) from None

    def presets_of_set(self, set_id: str) -> tuple:
        item = self.service_set(set_id)
        return tuple(self.preset(preset_id) for preset_id in item.preset_ids)

    def resolve(self, set_id: str) -> 'PinnedSet':
        return pin_set(self, set_id)

    def summary(self) -> dict:
        """Machine readable description of the catalog for GUI/CLI/API surfaces."""
        return {'schema_version': self.schema_version, 'manifest_version': self.manifest_version,
                'maintainer': self.maintainer, 'generated_at': self.generated_at, 'digest': self.digest,
                'categories': [item.to_dict() for item in self.categories],
                'capabilities': [{'id': item.capability_id, 'title_ru': item.title_ru,
                                  'title_en': item.title_en, 'proves_ru': item.proves_ru,
                                  'not_proved_ru': item.not_proved_ru, 'probeable': item.probeable}
                                 for item in self.capabilities],
                'presets': [preset.to_dict() for preset in self.presets],
                'service_sets': [item.to_dict() for item in self.service_sets],
                'field_scope': [dict(item) for item in self.field_scope]}

    def to_dict(self) -> dict:
        return self.summary()


def _parse_capability(raw, where) -> Capability:
    if not isinstance(raw, dict):
        _fail(f'{where}: ожидается объект')
    probeable = _require(raw, 'probeable', bool, where)
    reason_ru = raw.get('not_probeable_reason_ru')
    reason_en = raw.get('not_probeable_reason_en')
    if not probeable and not (isinstance(reason_ru, str) and reason_ru.strip()):
        _fail(f'{where}: для не измеряемой способности нужна причина not_probeable_reason_ru')
    return Capability(
        capability_id=_require_id(_require(raw, 'id', str, where), where),
        title_ru=_require_text(raw, 'title_ru', where),
        title_en=_require_text(raw, 'title_en', where),
        proves_ru=_require_text(raw, 'proves_ru', where),
        proves_en=_require_text(raw, 'proves_en', where),
        not_proved_ru=_require_text(raw, 'not_proved_ru', where),
        not_proved_en=_require_text(raw, 'not_proved_en', where),
        probeable=probeable,
        not_probeable_reason_ru=reason_ru,
        not_probeable_reason_en=reason_en)


def _parse_category(raw, where) -> Category:
    if not isinstance(raw, dict):
        _fail(f'{where}: ожидается объект')
    return Category(
        category_id=_require_id(_require(raw, 'id', str, where), where),
        title_ru=_require_text(raw, 'title_ru', where),
        title_en=_require_text(raw, 'title_en', where),
        description_ru=_require_text(raw, 'description_ru', where),
        description_en=_require_text(raw, 'description_en', where))


def _parse_probe(raw, where) -> Probe:
    if not isinstance(raw, dict):
        _fail(f'{where}: ожидается объект')
    url = _require_text(raw, 'url', where)
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        _fail(f'{where}.url: нужен http(s) URL без userinfo')
    try:
        port = parsed.port
    except ValueError:
        _fail(f'{where}.url: некорректный порт')
    if port is not None and not 1 <= port <= 65535:
        _fail(f'{where}.url: некорректный порт')
    method = _require_text(raw, 'method', where).upper()
    if method not in ('GET', 'HEAD'):
        _fail(f'{where}.method: поддерживаются GET и HEAD')
    statuses = raw.get('statuses')
    if (not isinstance(statuses, list) or not statuses
            or any(type(code) is not int or not 100 <= code <= 599 for code in statuses)):
        _fail(f'{where}.statuses: ожидается непустой список HTTP-кодов')
    headers = raw.get('headers', {})
    if not isinstance(headers, dict) or any(
            not isinstance(name, str) or not isinstance(value, str) or '\n' in name + value or '\r' in name + value
            for name, value in headers.items()):
        _fail(f'{where}.headers: ожидается объект со строковыми значениями')
    if any(name.lower() not in SAFE_PROBE_HEADERS for name in headers):
        _fail(tr(f'{where}.headers: только безопасные заголовки без credentials',
                 f'{where}.headers: safe headers only, no credentials'))
    contains = raw.get('contains')
    if contains is not None and not isinstance(contains, str):
        _fail(f'{where}.contains: ожидается строка')
    sha256 = raw.get('sha256')
    if sha256 is not None and (not isinstance(sha256, str) or not _SHA256.fullmatch(sha256)):
        _fail(f'{where}.sha256: ожидается хеш из 64 hex символов')
    known = {'id', 'url', 'method', 'statuses', 'headers', 'contains', 'sha256',
             'description_ru', 'description_en'}
    unknown = sorted(set(raw) - known)
    if unknown:
        _fail(tr(f'{where}: неизвестные поля {unknown}; молча игнорировать параметры запрещено',
                 f'{where}: unknown fields {unknown}; silently ignoring a parameter is forbidden'))
    return Probe(
        probe_id=_require_id(_require(raw, 'id', str, where), where),
        url=url, method=method, statuses=tuple(statuses), contains=contains, sha256=sha256,
        headers=tuple(sorted(headers.items())),
        description_ru=raw.get('description_ru') or '',
        description_en=raw.get('description_en') or '')


def _parse_cost(raw, where) -> Cost:
    if not isinstance(raw, dict):
        _fail(f'{where}: ожидается объект')
    requests = _require(raw, 'requests_per_pass', int, where)
    if requests < 1:
        _fail(f'{where}.requests_per_pass: минимум 1')
    max_bytes = _require(raw, 'max_bytes', int, where)
    if max_bytes < 0:
        _fail(f'{where}.max_bytes: не может быть отрицательным')
    weight = _require(raw, 'budget_weight', (int, float), where)
    if not 0 < weight <= 10:
        _fail(f'{where}.budget_weight: от 0 до 10')
    return Cost(requests_per_pass=requests, max_bytes=max_bytes, budget_weight=float(weight),
                rate_limit_ru=_require_text(raw, 'rate_limit_ru', where),
                rate_limit_en=_require_text(raw, 'rate_limit_en', where))


def _parse_preset(raw, where, categories, capabilities) -> Preset:
    if not isinstance(raw, dict):
        _fail(f'{where}: ожидается объект')
    known = {'id', 'version', 'maintainer', 'title_ru', 'title_en', 'aliases', 'categories', 'capability',
             'origin', 'probes', 'pass_condition_ru', 'pass_condition_en', 'not_proved_ru', 'not_proved_en',
             'limitations_ru', 'limitations_en', 'cost', 'definition_checked_on', 'live_checked_on',
             'verification', 'definition_source', 'contract_source', 'deprecated', 'deprecated_reason_ru',
             'user_owned'}
    unknown = sorted(set(raw) - known)
    if unknown:
        _fail(tr(f'{where}: неизвестные поля {unknown}; молча игнорировать параметры запрещено',
                 f'{where}: unknown fields {unknown}; silently ignoring a parameter is forbidden'))
    version = _require(raw, 'version', int, where)
    if version < 1:
        _fail(f'{where}.version: положительное целое')
    origin = _require_text(raw, 'origin', where)
    if origin not in ORIGIN_IDS:
        _fail(f'{where}.origin: ожидается одно из {ORIGIN_IDS}')
    verification = _require_text(raw, 'verification', where)
    if verification not in VERIFICATION_IDS:
        _fail(f'{where}.verification: ожидается одно из {VERIFICATION_IDS}')
    capability_id = _require_text(raw, 'capability', where)
    if capability_id not in capabilities:
        _fail(f'{where}.capability: способность {capability_id!r} не объявлена')
    if not capabilities[capability_id].probeable:
        _fail(tr(f'{where}: способность {capability_id!r} не измеряется этим каталогом; '
                 f'причина: {capabilities[capability_id].not_probeable_reason_ru}',
                 f'{where}: capability {capability_id!r} is not measurable by this catalog; '
                 f'reason: {capabilities[capability_id].not_probeable_reason_en}'))
    preset_categories = tuple(_require_text_list(raw.get('categories'), f'{where}.categories', allow_empty=False))
    unknown_categories = sorted(set(preset_categories) - set(categories))
    if unknown_categories:
        _fail(f'{where}.categories: неизвестные категории {unknown_categories}')
    not_proved_ru = _require_text_list(raw.get('not_proved_ru'), f'{where}.not_proved_ru', allow_empty=False)
    not_proved_en = _require_text_list(raw.get('not_proved_en'), f'{where}.not_proved_en', allow_empty=False)
    title_ru = _require_text(raw, 'title_ru', where)
    title_en = _require_text(raw, 'title_en', where)
    _no_promise(title_ru, title_en)
    deprecated = _require(raw, 'deprecated', bool, where)
    deprecated_reason = raw.get('deprecated_reason_ru')
    if deprecated and not (isinstance(deprecated_reason, str) and deprecated_reason.strip()):
        _fail(tr(f'{where}: deprecated=true требует причину deprecated_reason_ru',
                 f'{where}: deprecated=true requires a deprecated_reason_ru'))
    probes_raw = raw.get('probes')
    if not isinstance(probes_raw, list) or not probes_raw:
        _fail(f'{where}.probes: нужен непустой список проб')
    probes = tuple(_parse_probe(item, f'{where}.probes[{index}]') for index, item in enumerate(probes_raw))
    probe_ids = [probe.probe_id for probe in probes]
    if len(set(probe_ids)) != len(probe_ids):
        _fail(f'{where}.probes: повторяющиеся идентификаторы проб')
    source = raw.get('definition_source')
    if not isinstance(source, dict) or 'kind' not in source or 'ref' not in source:
        _fail(f'{where}.definition_source: ожидается объект с kind и ref')
    return Preset(
        preset_id=_require_id(_require(raw, 'id', str, where), where),
        version=version,
        maintainer=_require_text(raw, 'maintainer', where),
        title_ru=title_ru, title_en=title_en,
        aliases=tuple(_require_text_list(raw.get('aliases', []), f'{where}.aliases')),
        categories=preset_categories, capability=capability_id, origin=origin, probes=probes,
        pass_condition_ru=_require_text(raw, 'pass_condition_ru', where),
        pass_condition_en=_require_text(raw, 'pass_condition_en', where),
        not_proved_ru=tuple(not_proved_ru), not_proved_en=tuple(not_proved_en),
        limitations_ru=tuple(_require_text_list(raw.get('limitations_ru', []), f'{where}.limitations_ru')),
        limitations_en=tuple(_require_text_list(raw.get('limitations_en', []), f'{where}.limitations_en')),
        cost=_parse_cost(raw.get('cost'), f'{where}.cost'),
        definition_checked_on=_require_date(raw.get('definition_checked_on'), f'{where}.definition_checked_on'),
        live_checked_on=_require_date(raw.get('live_checked_on'), f'{where}.live_checked_on', allow_none=True),
        verification=verification, definition_source=_copy(source),
        contract_source=_require_text(raw, 'contract_source', where),
        deprecated=deprecated, deprecated_reason_ru=deprecated_reason,
        user_owned=bool(raw.get('user_owned', False)))


def _parse_field_scope(raw, where) -> dict:
    if not isinstance(raw, dict):
        _fail(f'{where}: ожидается объект')
    ownership = _require_text(raw, 'ownership', where)
    if ownership not in OWNERSHIP_IDS:
        _fail(f'{where}.ownership: ожидается одно из {OWNERSHIP_IDS}')
    entry = {'field': _require_text(raw, 'field', where), 'ownership': ownership,
             'derived': bool(raw.get('derived', False)),
             'label_ru': _require_text(raw, 'label_ru', where),
             'label_en': _require_text(raw, 'label_en', where)}
    if entry['derived'] and ownership != 'scenario':
        _fail(f'{where}: derived бывает только у поля сценария')
    return entry


def _parse_service_set(raw, where, presets, scenario_fields, derived_fields, user_fields) -> ServiceSet:
    if not isinstance(raw, dict):
        _fail(f'{where}: ожидается объект')
    known = {'id', 'version', 'maintainer', 'title_ru', 'title_en', 'description_ru', 'description_en',
             'required', 'optional', 'combination', 'min_passes', 'scenario', 'origin'}
    unknown = sorted(set(raw) - known)
    if unknown:
        _fail(tr(f'{where}: неизвестные поля {unknown}; молча игнорировать параметры запрещено',
                 f'{where}: unknown fields {unknown}; silently ignoring a parameter is forbidden'))
    required = _require_text_list(raw.get('required'), f'{where}.required', allow_empty=False)
    optional = _require_text_list(raw.get('optional', []), f'{where}.optional')
    if not set(required).isdisjoint(optional):
        _fail(f'{where}: сервис не может быть одновременно обязательным и дополнительным')
    for preset_id in required + optional:
        if preset_id not in presets:
            _fail(f'{where}: сервис {preset_id!r} не объявлен в presets')
    combination = _require_text(raw, 'combination', where)
    if combination not in COMBINATION_IDS:
        _fail(f'{where}.combination: ожидается одно из {COMBINATION_IDS}')
    min_passes = _require(raw, 'min_passes', int, where)
    if combination == 'at_least':
        if not 1 <= min_passes <= len(optional):
            _fail(tr(f'{where}.min_passes: от 1 до {len(optional)} для at_least; '
                     f'K=0 и K>размера дают проход без измерений (F05)',
                     f'{where}.min_passes: 1..{len(optional)} for at_least; '
                     f'K=0 and K>size would pass without measurements (F05)'))
    elif combination == 'any' and not optional:
        _fail(tr(f'{where}: any при пустом дополнительном наборе даёт проход без измерений',
                 f'{where}: any with an empty optional set would pass without measurements'))
    elif combination == 'none' and optional:
        _fail(f'{where}: none при непустом дополнительном наборе противоречив')
    elif combination != 'at_least' and min_passes != 0:
        _fail(f'{where}.min_passes: для правила {combination!r} ожидается 0')
    scenario_raw = raw.get('scenario')
    if not isinstance(scenario_raw, dict):
        _fail(f'{where}.scenario: ожидается объект с полным составом полей')
    expected = set(scenario_fields) - set(derived_fields)
    missing = sorted(expected - set(scenario_raw))
    if missing:
        _fail(tr(f'{where}.scenario: не заданы поля {missing}; набор обязан объявлять полный состав '
                 f'(дефект 24), иначе условия прошлого сценария останутся',
                 f'{where}.scenario: fields {missing} are not declared; a set must declare the full '
                 f'field inventory (defect 24), otherwise the previous scenario survives'))
    extra = sorted(set(scenario_raw) - expected)
    if extra:
        _fail(f'{where}.scenario: поля {extra} либо производные, либо принадлежат пользователю')
    if sorted(set(scenario_raw) & set(user_fields)):
        _fail(f'{where}.scenario: поля пользователя не задаются набором')
    for field in expected:
        if field not in SCENARIO_DEFAULTS:
            _fail(f'{where}.scenario: у поля {field!r} нет значения сброса в SCENARIO_DEFAULTS')
    origin = raw.get('origin', 'researched')
    if origin not in ORIGIN_IDS:
        _fail(f'{where}.origin: ожидается одно из {ORIGIN_IDS}')
    title_ru = _require_text(raw, 'title_ru', where)
    title_en = _require_text(raw, 'title_en', where)
    description_ru = _require_text(raw, 'description_ru', where)
    description_en = _require_text(raw, 'description_en', where)
    _no_promise(title_ru, title_en)
    _no_promise(description_ru, description_en, narrow=True)
    return ServiceSet(
        set_id=_require_id(_require(raw, 'id', str, where), where),
        version=_require(raw, 'version', int, where),
        maintainer=_require_text(raw, 'maintainer', where),
        title_ru=title_ru, title_en=title_en,
        description_ru=description_ru, description_en=description_en,
        required=tuple(required), optional=tuple(optional), combination=combination,
        min_passes=min_passes, scenario=_copy(scenario_raw), origin=origin)


def parse_catalog(data) -> Catalog:
    """Validate a decoded manifest and return an immutable :class:`Catalog`.

    Raises :class:`ManifestError` with the exact path of the offending field.
    Nothing is written and no network access happens.
    """
    if not isinstance(data, dict):
        _fail(tr('манифест: ожидается объект', 'manifest: expected an object'))
    schema_version = _require(data, 'schema_version', int, 'manifest')
    if schema_version > SCHEMA_VERSION:
        _fail(tr(f'манифест версии {schema_version} новее этой программы ({SCHEMA_VERSION})',
                 f'manifest version {schema_version} is newer than this program ({SCHEMA_VERSION})'))
    if schema_version != SCHEMA_VERSION:
        _fail(f'manifest.schema_version: ожидается {SCHEMA_VERSION}, получено {schema_version}')
    categories_raw = data.get('categories')
    if not isinstance(categories_raw, list) or not categories_raw:
        _fail('manifest.categories: нужен непустой список')
    categories = {}
    for index, item in enumerate(categories_raw):
        parsed = _parse_category(item, f'manifest.categories[{index}]')
        if parsed.category_id in categories:
            _fail(f'manifest.categories: повтор {parsed.category_id!r}')
        categories[parsed.category_id] = parsed
    capabilities_raw = data.get('capabilities')
    if not isinstance(capabilities_raw, list) or not capabilities_raw:
        _fail('manifest.capabilities: нужен непустой список')
    capabilities = {}
    for index, item in enumerate(capabilities_raw):
        parsed = _parse_capability(item, f'manifest.capabilities[{index}]')
        if parsed.capability_id in capabilities:
            _fail(f'manifest.capabilities: повтор {parsed.capability_id!r}')
        capabilities[parsed.capability_id] = parsed
    scope_raw = data.get('field_scope')
    if not isinstance(scope_raw, list) or not scope_raw:
        _fail('manifest.field_scope: нужен непустой список')
    field_scope = []
    seen_fields = set()
    for index, item in enumerate(scope_raw):
        entry = _parse_field_scope(item, f'manifest.field_scope[{index}]')
        if entry['field'] in seen_fields:
            _fail(f'manifest.field_scope: повтор поля {entry["field"]!r}')
        seen_fields.add(entry['field'])
        field_scope.append(entry)
    presets_raw = data.get('presets')
    if not isinstance(presets_raw, list) or not presets_raw:
        _fail('manifest.presets: нужен непустой список')
    presets = {}
    for index, item in enumerate(presets_raw):
        parsed = _parse_preset(item, f'manifest.presets[{index}]', categories, capabilities)
        if parsed.preset_id in presets:
            _fail(f'manifest.presets: повтор {parsed.preset_id!r}')
        presets[parsed.preset_id] = parsed
    scenario_fields = [entry['field'] for entry in field_scope if entry['ownership'] == 'scenario']
    derived_fields = [entry['field'] for entry in field_scope
                      if entry['ownership'] == 'scenario' and entry['derived']]
    user_fields = [entry['field'] for entry in field_scope if entry['ownership'] == 'user']
    if 'targets' not in derived_fields:
        _fail(tr('manifest.field_scope: targets обязан быть производным полем сценария',
                 'manifest.field_scope: targets must be a derived scenario field'))
    sets_raw = data.get('service_sets')
    if not isinstance(sets_raw, list) or not sets_raw:
        _fail('manifest.service_sets: нужен непустой список')
    service_sets = []
    seen_sets = set()
    for index, item in enumerate(sets_raw):
        parsed = _parse_service_set(item, f'manifest.service_sets[{index}]', presets,
                                    scenario_fields, derived_fields, user_fields)
        if parsed.set_id in seen_sets:
            _fail(f'manifest.service_sets: повтор {parsed.set_id!r}')
        seen_sets.add(parsed.set_id)
        service_sets.append(parsed)
    _require_date(data.get('generated_at'), 'manifest.generated_at')
    return Catalog(
        schema_version=schema_version,
        manifest_version=_require(data, 'manifest_version', int, 'manifest'),
        maintainer=_require_text(data, 'maintainer', 'manifest'),
        generated_at=data['generated_at'],
        categories=tuple(categories[key] for key in categories),
        capabilities=tuple(capabilities[key] for key in capabilities),
        presets=tuple(presets[key] for key in presets),
        service_sets=tuple(service_sets),
        field_scope=tuple(field_scope))


def load_manifest(path=None) -> dict:
    """Read and decode the manifest file without validating it."""
    manifest_path = Path(path) if path is not None else DEFAULT_MANIFEST_PATH
    try:
        raw = manifest_path.read_text(encoding='utf-8')
    except OSError as exc:
        raise CatalogError(tr(f'не читается файл каталога {manifest_path}: {exc.strerror}',
                             f'cannot read catalog file {manifest_path}: {exc.strerror}')) from None
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ManifestError(tr(f'файл каталога {manifest_path} не является JSON: {exc}',
                               f'catalog file {manifest_path} is not valid JSON: {exc}')) from None


def load_catalog(path=None) -> Catalog:
    """Load and validate the shipped catalog (or a caller supplied manifest)."""
    return parse_catalog(load_manifest(path))


def search_presets(catalog: Catalog, query: str = '', category: str | None = None,
                   capability: str | None = None, include_deprecated: bool = False) -> tuple:
    """Search by free text, category and capability.  All terms must match.

    Matching is case and layout insensitive so a Russian alias finds an English
    id, and it covers ids, both titles, aliases, category titles and the host of
    every probe URL.
    """
    terms = _search_terms(query)
    if category is not None:
        category = _require_id(category, 'search.category')
        if category not in catalog.category_index:
            raise CatalogError(tr(f'неизвестная категория {category!r}', f'unknown category {category!r}'))
    if capability is not None:
        if capability not in catalog.capability_index:
            raise CatalogError(tr(f'неизвестная способность {capability!r}',
                                 f'unknown capability {capability!r}'))
    found = []
    for preset in catalog.presets:
        if preset.deprecated and not include_deprecated:
            continue
        if category is not None and category not in preset.categories:
            continue
        if capability is not None and preset.capability != capability:
            continue
        if terms and not terms.issubset(_search_terms(' '.join(_preset_haystack(catalog, preset)))):
            continue
        found.append(preset)
    return tuple(found)


def _preset_haystack(catalog: Catalog, preset: Preset) -> list:
    parts = [preset.preset_id, preset.title_ru, preset.title_en, preset.capability, *preset.aliases]
    parts.extend(catalog.category_index[key].title_ru for key in preset.categories)
    parts.extend(catalog.category_index[key].title_en for key in preset.categories)
    parts.extend(urlsplit(probe.url).hostname or '' for probe in preset.probes)
    return [part for part in parts if part]


def _search_terms(text: str) -> frozenset:
    if not isinstance(text, str):
        raise CatalogError(tr('поисковый запрос должен быть строкой', 'search query must be a string'))
    normalized = ''.join(char if char.isalnum() else ' ' for char in text.casefold())
    return frozenset(token for token in normalized.split() if token)


def select_presets(catalog: Catalog, preset_ids, *, limit: int = MAX_TARGETS_PER_SELECTION) -> tuple:
    """Multi-selection: keep the caller's order, drop repeats, refuse unknowns."""
    if isinstance(preset_ids, str) or not hasattr(preset_ids, '__iter__'):
        raise CatalogError(tr('ожидается список идентификаторов сервисов',
                             'expected a list of service ids'))
    selected = []
    seen = set()
    for raw_id in preset_ids:
        if not isinstance(raw_id, str):
            raise CatalogError(tr(f'идентификатор сервиса должен быть строкой, получено {raw_id!r}',
                                 f'service id must be a string, got {raw_id!r}'))
        preset_id = raw_id.strip()
        if preset_id in seen:
            continue
        if preset_id not in catalog.preset_index:
            raise CatalogError(tr(f'в каталоге нет сервиса {preset_id!r}',
                                 f'no service {preset_id!r} in the catalog'))
        seen.add(preset_id)
        selected.append(catalog.preset(preset_id))
    if not selected:
        raise CatalogError(tr('не выбран ни один сервис', 'no service selected'))
    if len(selected) > limit:
        raise CatalogError(tr(f'выбрано {len(selected)} сервисов, максимум {limit}',
                             f'{len(selected)} services selected, maximum is {limit}'))
    return tuple(selected)


def build_targets(presets) -> list:
    """Flatten presets into the scanner target list, one entry per probe."""
    targets = []
    for preset in presets:
        for probe in preset.probes:
            targets.append(probe.to_target(preset.title_ru))
    if not targets:
        raise CatalogError(tr('у выбранных сервисов нет проб', 'the selected services have no probes'))
    if len(targets) > MAX_TARGETS_PER_SELECTION:
        raise CatalogError(tr(f'получилось {len(targets)} проб, а поверхности хранят максимум '
                             f'{MAX_TARGETS_PER_SELECTION} целей',
                             f'{len(targets)} probes produced, surfaces store at most '
                             f'{MAX_TARGETS_PER_SELECTION} targets'))
    return targets


def estimated_cost(presets) -> dict:
    """Aggregate pass cost of a selection, for the budget of a profile revision."""
    presets = tuple(presets)
    return {'presets': len(presets),
            'requests_per_pass': sum(preset.cost.requests_per_pass for preset in presets),
            'max_bytes': max((preset.cost.max_bytes for preset in presets), default=0),
            'budget_weight': round(sum(preset.cost.budget_weight for preset in presets), 3)}


@dataclass(frozen=True)
class PinnedSet:
    """A self-contained snapshot of a set and its presets.

    Targets are built from this object alone, so publishing a new manifest cannot
    change a profile revision that already pinned an older one.  The snapshot
    round-trips through JSON via :meth:`to_dict` and :meth:`from_dict`.
    """

    schema_version: int
    manifest_version: int
    set_id: str
    set_version: int
    set_digest: str
    title_ru: str
    title_en: str
    description_ru: str
    description_en: str
    required: tuple
    optional: tuple
    combination: str
    min_passes: int
    scenario: dict
    field_scope: tuple
    presets: dict
    origin: str = 'catalog'

    @property
    def preset_ids(self) -> tuple:
        return tuple(self.required) + tuple(self.optional)

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def preset(self, preset_id: str) -> Preset:
        try:
            payload = self.presets[preset_id]
        except KeyError:
            raise CatalogError(tr(f'в закреплённом наборе нет сервиса {preset_id!r}',
                                 f'no service {preset_id!r} in the pinned set')) from None
        return _preset_from_snapshot(payload)

    def ordered_presets(self) -> tuple:
        return tuple(self.preset(preset_id) for preset_id in self.preset_ids)

    def targets(self) -> list:
        """The scanner target list, derived from the snapshot only."""
        return build_targets(self.ordered_presets())

    def summary(self) -> dict:
        return {'set_id': self.set_id, 'set_version': self.set_version, 'set_digest': self.set_digest,
                'title_ru': self.title_ru, 'title_en': self.title_en,
                'description_ru': self.description_ru, 'description_en': self.description_en,
                'required': list(self.required), 'optional': list(self.optional),
                'combination': self.combination, 'min_passes': self.min_passes,
                'origin': self.origin,
                'presets': [self.preset(preset_id).to_dict() for preset_id in self.preset_ids]}

    def to_dict(self) -> dict:
        return {'schema_version': self.schema_version, 'manifest_version': self.manifest_version,
                'set_id': self.set_id, 'set_version': self.set_version, 'set_digest': self.set_digest,
                'title_ru': self.title_ru, 'title_en': self.title_en,
                'description_ru': self.description_ru, 'description_en': self.description_en,
                'required': list(self.required), 'optional': list(self.optional),
                'combination': self.combination, 'min_passes': self.min_passes,
                'scenario': _copy(self.scenario),
                'field_scope': [_copy(item) for item in self.field_scope],
                'presets': {key: _copy(value) for key, value in self.presets.items()},
                'origin': self.origin}

    @classmethod
    def from_dict(cls, payload) -> 'PinnedSet':
        if not isinstance(payload, dict):
            raise CatalogError(tr('ожидается сохранённый снимок набора',
                                 'expected a saved set snapshot'))
        schema_version = payload.get('schema_version')
        if schema_version != SCHEMA_VERSION:
            raise CatalogError(tr(f'снимок набора версии {schema_version} не читается этой программой',
                                 f'set snapshot version {schema_version} cannot be read by this program'))
        return cls(
            schema_version=schema_version,
            manifest_version=payload['manifest_version'],
            set_id=_require_id(payload['set_id'], 'set_snapshot.set_id'),
            set_version=payload['set_version'],
            set_digest=_require_text(payload, 'set_digest', 'set_snapshot'),
            title_ru=_require_text(payload, 'title_ru', 'set_snapshot'),
            title_en=_require_text(payload, 'title_en', 'set_snapshot'),
            # A user set may carry no description; a catalog set may not (checked in _parse_service_set).
            description_ru=_require_text(payload, 'description_ru', 'set_snapshot', allow_empty=True),
            description_en=_require_text(payload, 'description_en', 'set_snapshot', allow_empty=True),
            required=tuple(payload['required']),
            optional=tuple(payload.get('optional', ())),
            combination=_require_text(payload, 'combination', 'set_snapshot'),
            min_passes=payload['min_passes'],
            scenario=_copy(payload['scenario']),
            field_scope=tuple(_copy(item) for item in payload.get('field_scope', ())),
            presets={key: _copy(value) for key, value in payload['presets'].items()},
            origin=payload.get('origin', 'catalog'))


def _preset_from_snapshot(payload) -> Preset:
    """Rebuild a Preset from its own definition dict (used by a pinned snapshot)."""
    where = 'snapshot.preset'
    source = payload.get('definition_source', {'kind': 'user', 'ref': 'user-owned copy'})
    return Preset(
        preset_id=_require_id(payload['id'], where),
        version=int(payload['version']),
        maintainer=payload.get('maintainer', ''),
        title_ru=payload['title_ru'], title_en=payload.get('title_en', payload['title_ru']),
        aliases=tuple(payload.get('aliases', ())),
        categories=tuple(payload.get('categories', ())),
        capability=payload.get('capability', 'homepage'),
        origin=payload.get('origin', 'user'),
        probes=tuple(_parse_probe(item, f'{where}.probes[{index}]')
                     for index, item in enumerate(payload['probes'])),
        pass_condition_ru=payload['pass_condition_ru'],
        pass_condition_en=payload.get('pass_condition_en', payload['pass_condition_ru']),
        not_proved_ru=tuple(payload['not_proved_ru']),
        not_proved_en=tuple(payload.get('not_proved_en', payload['not_proved_ru'])),
        limitations_ru=tuple(payload.get('limitations_ru', ())),
        limitations_en=tuple(payload.get('limitations_en', ())),
        cost=_parse_cost(payload['cost'], f'{where}.cost'),
        definition_checked_on=payload.get('definition_checked_on', '1970-01-01'),
        live_checked_on=payload.get('live_checked_on'),
        verification=payload.get('verification', 'legacy_definition'),
        definition_source=source,
        contract_source=payload.get('contract_source', ''),
        deprecated=bool(payload.get('deprecated', False)),
        deprecated_reason_ru=payload.get('deprecated_reason_ru'),
        user_owned=bool(payload.get('user_owned', True)))


def pin_set(catalog: Catalog, set_id: str) -> PinnedSet:
    """Take the snapshot a saved profile revision stores instead of a reference."""
    service_set = catalog.service_set(set_id)
    return PinnedSet(
        schema_version=catalog.schema_version,
        manifest_version=catalog.manifest_version,
        set_id=service_set.set_id,
        set_version=service_set.version,
        set_digest=service_set.digest,
        title_ru=service_set.title_ru, title_en=service_set.title_en,
        description_ru=service_set.description_ru, description_en=service_set.description_en,
        required=service_set.required, optional=service_set.optional,
        combination=service_set.combination, min_passes=service_set.min_passes,
        scenario=_copy(service_set.scenario),
        field_scope=tuple(_copy(item) for item in catalog.field_scope),
        presets={preset.preset_id: preset.definition() for preset in catalog.presets_of_set(set_id)},
        origin=service_set.origin)


@dataclass(frozen=True)
class PresetChange:
    """One difference between a pinned preset and the current catalog version."""

    preset_id: str
    state: str  # updated | added | unchanged | removed | deprecated | undeprecated
    from_version: int | None
    to_version: int | None
    from_digest: str | None
    to_digest: str | None
    changed_fields: tuple
    deprecated_reason_ru: str | None = None

    @property
    def breaking(self) -> bool:
        """True when the change can alter a verdict, not only a description."""
        return self.state in ('updated', 'added', 'removed', 'deprecated', 'undeprecated')

    def to_dict(self) -> dict:
        return {'preset_id': self.preset_id, 'state': self.state,
                'from_version': self.from_version, 'to_version': self.to_version,
                'from_digest': self.from_digest, 'to_digest': self.to_digest,
                # A consumer has to know whether a change can move a verdict, so
                # the flag travels with the diff instead of staying a property.
                'breaking': self.breaking,
                'changed_fields': [list(item) for item in self.changed_fields],
                'deprecated_reason_ru': self.deprecated_reason_ru}


@dataclass(frozen=True)
class UpdatePreview:
    """What an explicit upgrade would change.  Nothing is applied by building it."""

    set_id: str
    from_set_version: int
    to_set_version: int
    set_state: str  # current | outdated | missing
    changes: tuple
    scenario_changes: tuple

    @property
    def has_changes(self) -> bool:
        return self.set_state in ('outdated', 'missing') or any(change.breaking for change in self.changes)

    @property
    def breaking_changes(self) -> tuple:
        return tuple(change for change in self.changes if change.breaking)

    def to_dict(self) -> dict:
        return {'set_id': self.set_id, 'from_set_version': self.from_set_version,
                'to_set_version': self.to_set_version, 'set_state': self.set_state,
                'changes': [change.to_dict() for change in self.changes],
                'scenario_changes': [list(item) for item in self.scenario_changes]}


def _definition_fields(preset: Preset) -> dict:
    """Flat view of a preset used to produce a field level diff."""
    return preset.definition()


def _diff_preset_fields(old: dict, new: dict) -> tuple:
    changed = []
    for key in sorted(set(old) | set(new)):
        before, after = old.get(key), new.get(key)
        if before == after:
            continue
        changed.append((key, before, after))
    return tuple(changed)


def preview_update(catalog: Catalog, pinned: PinnedSet) -> UpdatePreview:
    """Diff a pinned snapshot against the current catalog without touching it.

    This is the only way a preset update becomes visible: the caller shows the
    diff and then calls :func:`upgrade_set` on purpose.
    """
    set_state = 'current'
    if pinned.set_id not in catalog.set_index:
        set_state = 'missing'
    changes = []
    for preset_id in pinned.preset_ids:
        old_payload = pinned.presets[preset_id]
        old_digest = old_payload.get('digest') or _digest(old_payload)
        if preset_id not in catalog.preset_index:
            changes.append(PresetChange(preset_id, 'removed', old_payload['version'], None,
                                        old_digest, None, (), 'сервис удалён из каталога'))
            continue
        preset = catalog.preset(preset_id)
        if preset.deprecated and not old_payload.get('deprecated'):
            state = 'deprecated'
        elif not preset.deprecated and old_payload.get('deprecated'):
            state = 'undeprecated'
        elif preset.digest == old_digest:
            state = 'unchanged'
        else:
            state = 'updated'
        changes.append(PresetChange(
            preset_id, state, old_payload['version'], preset.version, old_digest, preset.digest,
            _diff_preset_fields(old_payload, _definition_fields(preset)),
            preset.deprecated_reason_ru if state == 'deprecated' else None))
    scenario_changes = ()
    if set_state != 'missing':
        current = catalog.service_set(pinned.set_id)
        scenario_changes = _diff_preset_fields(pinned.scenario, current.scenario)
        if current.version != pinned.set_version and set_state == 'current':
            set_state = 'outdated'
    return UpdatePreview(pinned.set_id, pinned.set_version,
                         catalog.service_set(pinned.set_id).version if set_state != 'missing' else -1,
                         set_state, tuple(changes), scenario_changes)


def upgrade_set(catalog: Catalog, preview: UpdatePreview) -> PinnedSet:
    """Take a new snapshot.  Only call this after the user saw the diff."""
    if preview.set_state == 'missing':
        raise CatalogError(tr(f'набор {preview.set_id!r} больше не в каталоге; прежний снимок остаётся '
                             f'в силе, обновление невозможно',
                             f'set {preview.set_id!r} is gone from the catalog; the pinned snapshot '
                             f'stays valid, no upgrade is possible'))
    return pin_set(catalog, preview.set_id)


@dataclass(frozen=True)
class AppliedScenario:
    """Result of applying a set's field inventory to current settings."""

    settings: dict
    set_fields: tuple
    cleared_fields: tuple
    unchanged_fields: tuple
    carried_over_fields: tuple
    targets: list = dc_field(default_factory=list)

    def report(self) -> dict:
        return {'set': [list(item) for item in self.set_fields],
                'cleared': list(self.cleared_fields),
                'unchanged': list(self.unchanged_fields),
                'carried_over': list(self.carried_over_fields)}


def _dig(settings: dict, field: str):
    node = settings
    parts = field.split('.')
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return None, None, None
    return node, parts[-1], '.'.join(parts)


def _assign(settings: dict, field: str, value):
    parts = field.split('.')
    node = settings
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = _copy(value)


def apply_scenario(settings: dict, scenario: dict, user_fields=(), targets=None) -> AppliedScenario:
    """Rebuild settings from a set's complete field inventory.

    Every field present in ``scenario`` is written: a ``None`` value resets it to
    its documented default from :data:`SCENARIO_DEFAULTS`, so no judge, strict
    flag or anonymity requirement from the previous scenario survives.  Fields
    listed in ``user_fields`` are never touched and are reported instead.
    """
    if not isinstance(settings, dict):
        raise CatalogError(tr('ожидаются текущие настройки в виде объекта',
                             'expected current settings as an object'))
    if not isinstance(scenario, dict):
        raise CatalogError(tr('ожидается состав полей набора', 'expected a set field inventory'))
    user_fields = set(user_fields or ())
    unknown = sorted(set(scenario) & user_fields)
    if unknown:
        raise CatalogError(tr(f'поля пользователя {unknown} нельзя задавать сценарием',
                             f'user fields {unknown} must not be set by a scenario'))
    missing_defaults = sorted(field for field, value in scenario.items()
                              if value is None and field not in SCENARIO_DEFAULTS)
    if missing_defaults:
        raise CatalogError(tr(f'у полей {missing_defaults} нет значения сброса в SCENARIO_DEFAULTS, '
                             f'сброс был бы молчаливым',
                             f'fields {missing_defaults} have no reset value in SCENARIO_DEFAULTS, '
                             f'the reset would be silent'))
    new_settings = _copy(settings)
    set_fields, cleared, unchanged = [], [], []
    for field in sorted(scenario):
        wanted = scenario[field]
        if wanted is None:
            wanted = SCENARIO_DEFAULTS[field]
        _assign(new_settings, field, wanted)
        node, key, path = _dig(settings, field)
        before = node.get(key) if isinstance(node, dict) else None
        entry = (field, before, wanted)
        if before is None or before == wanted:
            unchanged.append(field)
            continue
        set_fields.append(entry)
        if scenario[field] is None:
            cleared.append(field)
    if targets is not None:
        _assign(new_settings, 'targets', targets)
    carried = tuple(sorted(field for field in user_fields if _dig(new_settings, field)[0] is not None))
    return AppliedScenario(settings=new_settings, set_fields=tuple(set_fields),
                           cleared_fields=tuple(cleared), unchanged_fields=tuple(unchanged),
                           carried_over_fields=carried,
                           targets=list(targets) if targets is not None else [])


def new_user_set(catalog: Catalog, set_id: str, title_ru: str, title_en: str, preset_ids,
                 *, combination: str = 'any', min_passes: int = 1, scenario: dict | None = None,
                 description_ru: str = '', description_en: str = '',
                 required_ids=None) -> PinnedSet:
    """Build a user owned set from a multi-selection and snapshot it immediately.

    The result is a :class:`PinnedSet` with ``origin='user'``: it carries its own
    copies of the presets, so a later catalog update leaves it untouched.  The
    caller owns persistence of the returned snapshot.  When ``required_ids`` is
    omitted the first selected service becomes mandatory, because a set with no
    mandatory probe could pass without any measurement (F05).
    """
    presets = select_presets(catalog, preset_ids)
    if combination not in COMBINATION_IDS:
        raise CatalogError(tr(f'неизвестное правило {combination!r}', f'unknown rule {combination!r}'))
    derived = set(catalog.derived_fields())
    fields = {name: _copy(SCENARIO_DEFAULTS[name]) for name in catalog.scenario_fields()
              if name not in derived}
    if scenario:
        extra = sorted(set(scenario) - set(fields))
        if extra:
            raise CatalogError(tr(f'в наборе пользователя поля {extra} не объявлены в field_scope '
                                 f'сценария', f'user set fields {extra} are not in the scenario field_scope'))
        fields.update(scenario)
    for field, value in fields.items():
        if value is None and field not in SCENARIO_DEFAULTS:
            raise CatalogError(tr(f'у поля {field!r} нет значения сброса в SCENARIO_DEFAULTS',
                                 f'field {field!r} has no reset value in SCENARIO_DEFAULTS'))
    if required_ids is None:
        # A set with no mandatory probe could pass without any measurement (F05).
        required = (presets[0].preset_id,)
        optional = tuple(preset.preset_id for preset in presets[1:])
    else:
        required = tuple(required_ids)
        optional = tuple(preset.preset_id for preset in presets if preset.preset_id not in set(required))
        if not required:
            raise CatalogError(tr('у набора должен быть хотя бы один обязательный сервис',
                                 'a set needs at least one mandatory service'))
        unknown_required = sorted(set(required) - {preset.preset_id for preset in presets})
        if unknown_required:
            raise CatalogError(tr(f'обязательные сервисы {unknown_required} не входят в выборку',
                                 f'mandatory services {unknown_required} are not in the selection'))
    if combination == 'at_least' and not 1 <= min_passes <= len(optional):
        raise CatalogError(tr(f'min_passes вне 1..{len(optional)} для at_least',
                             f'min_passes outside 1..{len(optional)} for at_least'))
    if combination == 'any' and not optional:
        raise CatalogError(tr('any требует хотя бы одного дополнительного сервиса',
                             'any requires at least one optional service'))
    if combination == 'none' and optional:
        raise CatalogError(tr('none несовместим с дополнительными сервисами',
                             'none is incompatible with optional services'))
    title_ru = title_ru.strip()
    title_en = (title_en or title_ru).strip()
    if not title_ru or not title_en:
        raise CatalogError(tr('название набора не может быть пустым', 'set title must not be empty'))
    _no_promise(title_ru, title_en)
    _no_promise(description_ru, description_en, narrow=True)
    snapshot = {preset.preset_id: _user_copy(preset) for preset in presets}
    min_passes = min_passes if combination == 'at_least' else 0
    body = {'id': set_id, 'version': 1, 'maintainer': 'user', 'title_ru': title_ru, 'title_en': title_en,
            'description_ru': description_ru, 'description_en': description_en,
            'required': list(required), 'optional': list(optional), 'combination': combination,
            'min_passes': min_passes, 'scenario': _copy(fields), 'origin': 'user'}
    return PinnedSet(
        schema_version=SCHEMA_VERSION, manifest_version=catalog.manifest_version,
        set_id=_require_id(set_id, 'user_set.id'), set_version=1,
        set_digest=_digest(body), title_ru=title_ru, title_en=title_en,
        description_ru=description_ru, description_en=description_en,
        required=required, optional=optional,
        combination=combination, min_passes=min_passes,
        scenario=_copy(fields), field_scope=catalog.field_scope, presets=snapshot, origin='user')


def _user_copy(preset: Preset) -> dict:
    """Snapshot a preset into a user owned copy that catalog updates cannot reach."""
    payload = preset.definition()
    payload['origin'] = 'user'
    payload['user_owned'] = True
    payload['maintainer'] = f'{preset.maintainer} (user copy of {preset.preset_id}@{preset.version})'
    return payload


__all__ = [
    'SCHEMA_VERSION', 'DEFAULT_MANIFEST_PATH', 'SAFE_PROBE_HEADERS', 'MAX_TARGETS_PER_SELECTION',
    'SCENARIO_DEFAULTS', 'COMBINATION_IDS', 'OWNERSHIP_IDS', 'VERIFICATION_IDS', 'ORIGIN_IDS',
    'CatalogError', 'ManifestError', 'Probe', 'Cost', 'Capability', 'Category', 'Preset', 'ServiceSet',
    'Catalog', 'PinnedSet', 'PresetChange', 'UpdatePreview', 'AppliedScenario',
    'load_manifest', 'load_catalog', 'parse_catalog', 'search_presets', 'select_presets',
    'build_targets', 'estimated_cost', 'pin_set', 'preview_update', 'upgrade_set', 'apply_scenario',
    'new_user_set',
]
