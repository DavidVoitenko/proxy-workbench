"""Geography and endpoint characteristics on top of the existing country picker.

The picker itself stays where it is: ``geoip.parse_countries`` for codes and the
combobox in ``ui/app.js`` for names.  This module is the one place that decides
whether an endpoint answers a country criterion, and it keeps apart the four
things F08 refuses to merge into one "Country" column:

* **endpoint country** - the country of the address the client connects to;
* **observed exit country** - the country of the address a judge saw;
* **origin of the knowledge** - which source produced the code, and when;
* **unknown** - a fact we do not have, which is never reported as "not in NL".

Two rules are load-bearing and are exercised by the tests:

* a hostname has no geography of its own.  Its country comes from the address
  that was actually used, together with the time that address was used, so a
  hostname without a recorded resolution is unknown rather than a guess;
* the hosting flag is a regexp over the ASN organisation name.  It is reported
  as a heuristic with its basis attached and never as proof of residential or
  mobile access - both of those stay ``None`` and are described as unmeasured.

Nothing in this module opens a network connection.  Country and provider data
come from the local databases of ``geoip``, and installing an update is a file
operation that keeps the last working database when a new file fails
validation.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field, replace
import csv
import gzip
import hashlib
import ipaddress
import json
import os
import time
from pathlib import Path

from . import geoip

# --- Provenance of a country code -------------------------------------------------

SOURCE_GEOIP = 'geoip'                # offline country database
SOURCE_SOURCE = 'source'              # metadata published by the list the endpoint came from
SOURCE_PROVIDER = 'provider'          # provider claim, not a measurement
SOURCE_RESOLVED = 'resolved'          # hostname -> the address that was really used
SOURCE_OBSERVED_EXIT = 'observed_exit'  # the address a judge saw
SOURCE_UNRESOLVED = 'unresolved'      # hostname with no recorded resolution

# Later entries win over earlier ones when two sources disagree.
SOURCE_PRECEDENCE = (SOURCE_RESOLVED, SOURCE_OBSERVED_EXIT, SOURCE_GEOIP, SOURCE_SOURCE, SOURCE_PROVIDER)

# --- What the country is compared against ---------------------------------------

BASIS_ENDPOINT = 'endpoint'
BASIS_EXIT = 'exit'
BASIS_EITHER = 'either'
BASES = (BASIS_ENDPOINT, BASIS_EXIT, BASIS_EITHER)

# --- What happens to a row whose compared country is unknown --------------------

UNKNOWN_EXCLUDE = 'exclude'                    # drop it: an unknown country is not the wanted one
UNKNOWN_INCLUDE = 'include_unverified'         # keep it, visibly unconfirmed
UNKNOWN_REQUIRE = 'require_measurement'        # keep it and ask for a judge probe
UNKNOWN_POLICIES = (UNKNOWN_EXCLUDE, UNKNOWN_INCLUDE, UNKNOWN_REQUIRE)

# Legacy behaviour of the picker: an endpoint without a known country did not
# match a country list.  Kept as the documented default of the parsing edge so
# "DE,NL" keeps meaning what it means today; the value is carried inside the
# criterion and is meant to be shown to the user rather than assumed.
DEFAULT_UNKNOWN_POLICY = UNKNOWN_EXCLUDE

# --- Reason codes ----------------------------------------------------------------
# Stable machine codes (CONTRACTS §5.4 style).  Text lives with i18n.tr, not here.

REASON_NO_CRITERION = 'geo_no_country_criterion'
REASON_MATCHED = 'geo_country_matched'
REASON_NOT_INCLUDED = 'geo_country_not_included'
REASON_EXCLUDED = 'geo_country_excluded'
REASON_UNKNOWN_EXCLUDED = 'geo_country_unknown_excluded'
REASON_UNKNOWN_INCLUDED = 'geo_country_unknown_included'
REASON_UNKNOWN_NEEDS_MEASUREMENT = 'geo_country_unknown_needs_measurement'

PLAN_READY = 'geo_plan_ready'
PLAN_JUDGE_FOR_EXIT = 'geo_plan_judge_for_exit_country'
PLAN_JUDGE_FOR_UNKNOWN = 'geo_plan_judge_for_unknown_country'
PLAN_DROP = 'geo_plan_drop'

# --- ASN, organisation, CIDR -----------------------------------------------------

# The hosting flag is a name heuristic.  It is always reported with this basis
# so no consumer can present it as a measurement.
HOSTING_BASIS_ORG_NAME = 'org_name_heuristic'
HOSTING_BASIS_NONE = 'none'
# Machine-readable stand-in for "we cannot say": a row that is not hosting is
# not a proof of residential or mobile access.
HOSTING_CLAIM_NOT_RESIDENTIAL = 'org_name_heuristic_is_not_residential_evidence'
UNMEASURED = 'unmeasured'

# --- GeoIP databases -------------------------------------------------------------

COUNTRY_DATABASE = 'country'
ASN_DATABASE = 'asn'
# DB-IP publishes a new month at the start of each month; a third of a month is
# the point where "current" starts to be worth reporting as stale.
DEFAULT_DATABASE_MAX_AGE = 45 * 24 * 3600.0
META_SUFFIX = '.meta.json'

# --- Pool quotas -----------------------------------------------------------------
# Storage and refill live in pools.py; this module only defines what a quota is
# counted from, because "endpoints" and "confirmed unique exits" need different
# evidence and an unknown exit cannot confirm anything.
QUOTA_ENDPOINTS = 'endpoints'
QUOTA_EXIT_IPS = 'exit_ips'

REASON_QUOTA_EXIT_CONFIRMED = 'geo_quota_exit_ips_confirmed'
REASON_QUOTA_EXIT_UNKNOWN = 'geo_quota_exit_ips_unknown'
REASON_QUOTA_ENDPOINTS = 'geo_quota_endpoints'


@dataclass(frozen=True)
class CountryInfo:
    """One entry of the country list shared by the picker and the backend."""

    code: str
    name_en: str
    name_ru: str
    region: str
    popular: bool = False

    def as_dict(self):
        return {'code': self.code, 'name_en': self.name_en, 'name_ru': self.name_ru,
                'region': self.region, 'popular': self.popular}


# One list for the backend and for the picker: the same 64 codes with the same
# RU/EN names, so a country typed in the GUI and a country sent to /v1 mean the
# same thing.  tests/test_geo_picker_parity.py checks this list against
# COUNTRIES_LIST in ui/app.js; that test is the guard against silent divergence.
COUNTRIES = tuple(
    CountryInfo(code, name_en, name_ru, region, popular)
    for code, name_en, name_ru, region, popular in (
        ('US', 'United States', 'США', 'na', True),
        ('DE', 'Germany', 'Германия', 'eu', True),
        ('NL', 'Netherlands', 'Нидерланды', 'eu', True),
        ('GB', 'United Kingdom', 'Великобритания', 'eu', True),
        ('FR', 'France', 'Франция', 'eu', True),
        ('RU', 'Russia', 'Россия', 'cis', True),
        ('PL', 'Poland', 'Польша', 'eu', True),
        ('UA', 'Ukraine', 'Украина', 'cis', True),
        ('KZ', 'Kazakhstan', 'Казахстан', 'cis', True),
        ('JP', 'Japan', 'Япония', 'asia', True),
        ('SG', 'Singapore', 'Сингапур', 'asia', True),
        ('CA', 'Canada', 'Канада', 'na', True),
        ('CH', 'Switzerland', 'Швейцария', 'eu', True),
        ('SE', 'Sweden', 'Швеция', 'eu', True),
        ('FI', 'Finland', 'Финляндия', 'eu', True),
        ('NO', 'Norway', 'Норвегия', 'eu', True),
        ('IT', 'Italy', 'Италия', 'eu', True),
        ('ES', 'Spain', 'Испания', 'eu', True),
        ('TR', 'Turkey', 'Турция', 'asia', True),
        ('KR', 'South Korea', 'Южная Корея', 'asia', True),
        ('HK', 'Hong Kong', 'Гонконг', 'asia', True),
        ('TW', 'Taiwan', 'Тайвань', 'asia', False),
        ('IN', 'India', 'Индия', 'asia', True),
        ('BR', 'Brazil', 'Бразилия', 'sa', True),
        ('AU', 'Australia', 'Австралия', 'other', False),
        ('AT', 'Austria', 'Австрия', 'eu', False),
        ('BE', 'Belgium', 'Бельгия', 'eu', False),
        ('CZ', 'Czech Republic', 'Чехия', 'eu', False),
        ('RO', 'Romania', 'Румыния', 'eu', False),
        ('BG', 'Bulgaria', 'Болгария', 'eu', False),
        ('DK', 'Denmark', 'Дания', 'eu', False),
        ('IE', 'Ireland', 'Ирландия', 'eu', False),
        ('PT', 'Portugal', 'Португалия', 'eu', False),
        ('GR', 'Greece', 'Греция', 'eu', False),
        ('HU', 'Hungary', 'Венгрия', 'eu', False),
        ('SK', 'Slovakia', 'Словакия', 'eu', False),
        ('EE', 'Estonia', 'Эстония', 'eu', False),
        ('LV', 'Latvia', 'Латвия', 'eu', False),
        ('LT', 'Lithuania', 'Литва', 'eu', False),
        ('CY', 'Cyprus', 'Кипр', 'eu', False),
        ('IL', 'Israel', 'Израиль', 'asia', False),
        ('AE', 'United Arab Emirates', 'ОАЭ', 'asia', False),
        ('TH', 'Thailand', 'Таиланд', 'asia', False),
        ('VN', 'Vietnam', 'Вьетнам', 'asia', False),
        ('ID', 'Indonesia', 'Индонезия', 'asia', False),
        ('MY', 'Malaysia', 'Малайзия', 'asia', False),
        ('CN', 'China', 'Китай', 'asia', False),
        ('AR', 'Argentina', 'Аргентина', 'sa', False),
        ('MX', 'Mexico', 'Мексика', 'na', False),
        ('CL', 'Chile', 'Чили', 'sa', False),
        ('CO', 'Colombia', 'Колумбия', 'sa', False),
        ('ZA', 'South Africa', 'ЮАР', 'other', False),
        ('EG', 'Egypt', 'Египет', 'other', False),
        ('BY', 'Belarus', 'Беларусь', 'cis', False),
        ('GE', 'Georgia', 'Грузия', 'cis', False),
        ('AM', 'Armenia', 'Армения', 'cis', False),
        ('AZ', 'Azerbaijan', 'Азербайджан', 'cis', False),
        ('UZ', 'Uzbekistan', 'Узбекистан', 'cis', False),
        ('MD', 'Moldova', 'Молдова', 'cis', False),
        ('RS', 'Serbia', 'Сербия', 'eu', False),
        ('HR', 'Croatia', 'Хорватия', 'eu', False),
        ('IS', 'Iceland', 'Исландия', 'eu', False),
        ('LU', 'Luxembourg', 'Люксембург', 'eu', False),
        ('NZ', 'New Zealand', 'Новая Зеландия', 'other', False),
    )
)

COUNTRY_BY_CODE = {item.code: item for item in COUNTRIES}


def country_list():
    """The picker list for a GUI or an API that wants to render it."""
    return [item.as_dict() for item in COUNTRIES]


def country(code):
    """CountryInfo for an ISO code, or None."""
    return COUNTRY_BY_CODE.get(normalize_code(code) or '')


def region_codes(region):
    """Codes of one region, in list order ('top' is not a region, use ``popular_codes``)."""
    key = str(region or '').strip().lower()
    return tuple(item.code for item in COUNTRIES if item.region == key)


def popular_codes():
    return tuple(item.code for item in COUNTRIES if item.popular)


def search_countries(query, lang='ru'):
    """Countries whose ISO code or name in ``lang`` contains ``query``."""
    needle = str(query or '').strip().lower()
    if not needle:
        return country_list()
    field_name = 'name_ru' if lang == 'ru' else 'name_en'
    return [item.as_dict() for item in COUNTRIES
            if needle in item.code.lower() or needle in getattr(item, field_name).lower()
            or needle in item.name_en.lower() or needle in item.name_ru.lower()]


def normalize_code(value):
    """Upper-case a two-letter code, or None when the value is not one."""
    text = str(value or '').strip().upper()
    return text if geoip.COUNTRY_CODE.fullmatch(text) else None


# --- Endpoint addresses ----------------------------------------------------------


@dataclass(frozen=True)
class HostInfo:
    """What the host part of an endpoint actually is."""

    host: str | None
    ip_version: int | None      # 4, 6 or None for a hostname
    address: str | None         # set when the host is an IP literal
    is_hostname: bool


def endpoint_host(proxy):
    """Host of an endpoint.  Delegated to the existing normalizer in geoip."""
    text = str(proxy or '').strip()
    host = geoip.proxy_host(text)
    if not host:
        host = text
    if host.startswith('[') and ']' in host:
        host = host[1:host.index(']')]
    return host


def classify_host(proxy):
    """Split an endpoint into a hostname or an IP literal, without resolving anything."""
    host = endpoint_host(proxy)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return HostInfo(host or None, None, None, bool(host))
    return HostInfo(str(address), address.version, str(address), False)


def endpoint_ip_version(proxy):
    """4, 6, or None when the endpoint host is a hostname.

    This is the address family of the endpoint.  It says nothing about which
    address families the endpoint can reach as a destination - see
    ``TargetCapabilities``.
    """
    return classify_host(proxy).ip_version


# --- Country knowledge and its origin -------------------------------------------


@dataclass(frozen=True)
class CountryFact:
    """One country's origin: which source, when, and which database version."""

    code: str | None
    source: str
    at: float | None = None
    database_version: str | None = None
    address: str | None = None

    @classmethod
    def of(cls, code, source, at=None, database_version=None, address=None):
        """Build a fact from stored data.  Anything that is not an ISO code is unknown."""
        return cls(normalize_code(code), source, at=at, database_version=database_version, address=address)

    @classmethod
    def unknown(cls, source=SOURCE_UNRESOLVED, address=None, at=None):
        """No code, but the address and the moment it was used are still facts.

        A hostname resolved to an address the database cannot classify has no
        country, yet "which address, and when" is exactly what makes the gap
        explainable - F08 asks for a hostname's geography to rest on the
        address that was really used and the time it was used.
        """
        return cls(None, source, at=at, address=address)

    @property
    def known(self):
        return self.code is not None

    def age_seconds(self, now):
        """Age of the knowledge, or None when it was not dated."""
        if self.at is None or now is None:
            return None
        return max(0.0, float(now) - float(self.at))

    def is_stale(self, now, max_age_seconds):
        """True when the knowledge may no longer be trusted.

        No ``max_age_seconds`` means the caller did not ask for freshness, and
        the age is then not a reason to doubt the code.  With a limit, an
        undated fact is stale: "when did we learn this" is part of the fact.
        """
        if not self.known:
            return True
        if max_age_seconds is None:
            return False
        age = self.age_seconds(now)
        return age is None or age > float(max_age_seconds)

    def as_dict(self, now=None, max_age_seconds=None):
        return {'country': self.code, 'country_source': self.source, 'country_at': self.at,
                'country_age_seconds': self.age_seconds(now),
                'country_stale': self.is_stale(now, max_age_seconds),
                'country_database_version': self.database_version,
                'country_address': self.address}


@dataclass(frozen=True)
class MergedCountry:
    """Result of combining facts from several sources."""

    fact: CountryFact | None
    others: tuple = ()          # the other sources, kept for the drawer
    conflict: str | None = None  # a code that disagrees with the chosen one

    @property
    def code(self):
        return self.fact.code if self.fact else None

    @property
    def known(self):
        return bool(self.fact and self.fact.known)


def merge_facts(facts, now=None, max_age_seconds=None):
    """Pick the most trustworthy fact and keep the others, with any conflict named.

    Precedence is by source, and a fact older than ``max_age_seconds`` is not
    used as knowledge.  Nothing is discarded silently: a disagreeing source is
    reported in ``conflict`` and stays visible in ``others``.
    """
    usable = [f for f in facts if f is not None and f.known and not f.is_stale(now, max_age_seconds)]
    if not usable:
        return MergedCountry(None, tuple(f for f in facts if f is not None))
    unknown_source = len(SOURCE_PRECEDENCE)
    usable.sort(key=lambda f: SOURCE_PRECEDENCE.index(f.source) if f.source in SOURCE_PRECEDENCE
                else unknown_source)
    chosen = usable[0]
    others = tuple(f for f in usable[1:] if f.code != chosen.code)
    conflict = others[0].code if others else None
    return MergedCountry(chosen, tuple(f for f in facts if f is not None and f is not chosen), conflict)


# --- The country criterion -------------------------------------------------------


@dataclass(frozen=True)
class CountryCriterion:
    """What the user asked for, in one object that GUI, API and export share.

    ``basis`` decides which country is compared.  ``exit`` compares the observed
    exit and never rejects an endpoint because of the endpoint's own country, so
    a German endpoint wanted for a Dutch exit survives.  ``exclude`` is a
    deliberate "never here" rule and applies to every known country, exit
    included.  ``unknown`` says what happens when the compared country is not
    known, and there is no implicit third meaning: it is always a value here.
    """

    include: tuple = ()
    exclude: tuple = ()
    basis: str = BASIS_ENDPOINT
    unknown: str = DEFAULT_UNKNOWN_POLICY
    max_age_seconds: float | None = None

    def __post_init__(self):
        object.__setattr__(self, 'include', tuple(sorted(set(self.include))))
        object.__setattr__(self, 'exclude', tuple(sorted(set(self.exclude))))
        if self.basis not in BASES:
            raise ValueError('Страны: основание отбора должно быть endpoint, exit или either.')
        if self.unknown not in UNKNOWN_POLICIES:
            raise ValueError('Страны: политика unknown должна быть exclude, include_unverified '
                             'или require_measurement.')
        if self.max_age_seconds is not None and float(self.max_age_seconds) <= 0:
            raise ValueError('Страны: срок знания должен быть положительным числом секунд.')
        overlap = set(self.include) & set(self.exclude)
        if overlap:
            raise ValueError('Страны: код %s одновременно включён и исключён.' % ', '.join(sorted(overlap)))

    @property
    def countries(self):
        """Tuple for the existing ``countries=`` argument, unchanged in meaning."""
        return self.include

    @property
    def active(self):
        return bool(self.include or self.exclude)

    def as_dict(self):
        return {'include': list(self.include), 'exclude': list(self.exclude), 'basis': self.basis,
                'unknown': self.unknown, 'max_age_seconds': self.max_age_seconds}

    @classmethod
    def from_dict(cls, value):
        data = dict(value or {})
        return cls(include=tuple(data.get('include') or ()), exclude=tuple(data.get('exclude') or ()),
                   basis=data.get('basis') or BASIS_ENDPOINT, unknown=data.get('unknown') or DEFAULT_UNKNOWN_POLICY,
                   max_age_seconds=data.get('max_age_seconds'))

    def digest(self):
        """Stable id of the criterion, so the three surfaces can prove they agree."""
        return hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()[:20]


def _resolve_token(token):
    """One picker token to an ISO code: the code itself, or a RU/EN name."""
    text = str(token or '').strip()
    if not text:
        return None
    code = normalize_code(text)
    if code:
        return code
    needle = text.lower()
    for item in COUNTRIES:
        if needle in (item.name_en.lower(), item.name_ru.lower()):
            return item.code
    return None


# Keys a settings mapping or a query string may use for each part of the
# criterion, so a GUI form, an export invocation and /v1 all end up as one object.
_CRITERION_ALIASES = {
    'include': ('include', 'country', 'countries'),
    'exclude': ('exclude', 'country_exclude', 'countries_exclude'),
    'basis': ('basis', 'country_basis'),
    'unknown': ('unknown', 'country_unknown'),
    'max_age_seconds': ('max_age_seconds', 'country_max_age_seconds'),
}


def _from_mapping(value, basis, unknown):
    def _get(field_name):
        for key in _CRITERION_ALIASES[field_name]:
            if key in value and value[key] not in (None, ''):
                return value[key]
        return None

    return CountryCriterion.from_dict({
        'include': _as_code_tuple(_get('include')),
        'exclude': _as_code_tuple(_get('exclude')),
        'basis': _get('basis') or basis,
        'unknown': _get('unknown') or unknown,
        'max_age_seconds': _get('max_age_seconds'),
    })


def _as_code_tuple(value):
    """Codes from a query parameter: ``'DE,NL'``, ``['DE','NL']`` or ``['DE,NL']``."""
    if value is None:
        return ()
    items = value if isinstance(value, (list, tuple, set)) else [value]
    codes = []
    for item in items:
        for token in str(item).split(',') if isinstance(item, str) else [item]:
            if str(token).strip() == '':
                continue
            code = _resolve_token(token)
            if code is None:
                raise ValueError('Страны: «%s» не найдено. Используйте ISO-код, например DE,NL.'
                                 % str(token).strip())
            codes.append(code)
    return tuple(codes)


def parse_criterion(value, basis=BASIS_ENDPOINT, unknown=DEFAULT_UNKNOWN_POLICY, max_age_seconds=None,
                    include=None, exclude=None):
    """Build a criterion from what the user typed.

    Accepts the picker's string form (``"de,NL"``), the same list, or a mapping
    as it comes back from a query string or a settings file - the mapping keys
    are listed in ``_CRITERION_ALIASES``, so ``{'country': 'DE,NL',
    'country_basis': 'exit'}`` and ``{'include': ['DE', 'NL'], 'basis': 'exit'}``
    are the same criterion.  ``!`` and ``-`` before a token mean exclusion, so
    "DE,NL,!FR" is what the picker cannot express today.  ``include``/``exclude``
    carry the separate query parameters an API client sends.

    The default for ``unknown`` is ``exclude`` - what "DE,NL" does today - and
    is carried in the returned criterion rather than assumed by the consumer.
    """
    if isinstance(value, CountryCriterion):
        return value
    if isinstance(value, dict):
        return _from_mapping(value, basis, unknown)
    tokens = value.split(',') if isinstance(value, str) else list(value or ())
    included, excluded = [], []
    for token in tokens:
        text = str(token or '').strip()
        if not text:
            continue
        negative = text[0] in '!-'
        code = _resolve_token(text[1:] if negative else text)
        if code is None:
            raise ValueError('Страны: «%s» не найдено. Используйте ISO-код, например DE,NL.' % text)
        (excluded if negative else included).append(code)
    for group, extra in ((included, include), (excluded, exclude)):
        for token in list(extra or ()):
            code = _resolve_token(token)
            if code is None:
                raise ValueError('Страны: «%s» не найдено. Используйте ISO-код, например DE,NL.' % token)
            group.append(code)
    return CountryCriterion(include=tuple(included), exclude=tuple(excluded), basis=basis,
                            unknown=unknown, max_age_seconds=max_age_seconds)


@dataclass(frozen=True)
class CountryVerdict:
    """Why a row does or does not answer the criterion.  Nothing here is a guess."""

    matched: bool
    verified: bool
    reason: str
    basis: str
    compared: tuple = ()
    unknown: bool = False
    needs_measurement: bool = False
    conflict: str | None = None

    @property
    def country(self):
        """The code that was actually compared, or None when nothing was known."""
        for fact in self.compared:
            if fact.known:
                return fact.code
        return None

    def as_dict(self):
        return {'matched': self.matched, 'verified': self.verified, 'reason': self.reason,
                'basis': self.basis, 'country': self.country, 'unknown': self.unknown,
                'needs_measurement': self.needs_measurement, 'conflict': self.conflict}


def evaluate(criterion, endpoint=None, exit=None, now=None):
    """Decide one endpoint against one criterion.

    ``endpoint`` and ``exit`` are ``CountryFact`` (or None).  The result says
    whether the row matched, whether that match is a confirmed fact or only the
    absence of a contradiction, and which code was compared.
    """
    if not isinstance(criterion, CountryCriterion):
        criterion = parse_criterion(criterion)

    def _fresh(fact):
        if fact is None or not fact.known:
            return None
        return None if fact.is_stale(now, criterion.max_age_seconds) else fact

    endpoint_fresh, exit_fresh = _fresh(endpoint), _fresh(exit)
    if not criterion.active:
        return CountryVerdict(True, False, REASON_NO_CRITERION, criterion.basis,
                              tuple(f for f in (endpoint_fresh, exit_fresh) if f))

    # "never here" is a deliberate rule and applies to every country we know.
    for fact in (endpoint_fresh, exit_fresh):
        if fact and fact.code in criterion.exclude:
            return CountryVerdict(False, False, REASON_EXCLUDED, criterion.basis, (fact,))

    if criterion.basis == BASIS_ENDPOINT:
        candidates = (endpoint_fresh,)
    elif criterion.basis == BASIS_EXIT:
        candidates = (exit_fresh,)
    else:
        candidates = (endpoint_fresh, exit_fresh)
    known = tuple(f for f in candidates if f)

    if known:
        if not criterion.include:
            # Only exclusions were asked for, so any country that is not excluded answers it.
            return CountryVerdict(True, True, REASON_MATCHED, criterion.basis, known)
        hits = tuple(f for f in known if f.code in criterion.include)
        other = tuple(f.code for f in known if f.code not in criterion.include)
        conflict = other[0] if (len(known) > 1 and hits and other) else None
        if hits:
            return CountryVerdict(True, True, REASON_MATCHED, criterion.basis, known, conflict=conflict)
        return CountryVerdict(False, False, REASON_NOT_INCLUDED, criterion.basis, known)

    # Nothing is known for the compared basis: the criterion says what to do.
    if criterion.unknown == UNKNOWN_EXCLUDE:
        return CountryVerdict(False, False, REASON_UNKNOWN_EXCLUDED, criterion.basis, (), unknown=True)
    if criterion.unknown == UNKNOWN_INCLUDE:
        return CountryVerdict(True, False, REASON_UNKNOWN_INCLUDED, criterion.basis, (), unknown=True)
    return CountryVerdict(True, False, REASON_UNKNOWN_NEEDS_MEASUREMENT, criterion.basis, (), unknown=True,
                          needs_measurement=True)


# --- What a read-only filter may and may not decide ------------------------------


@dataclass(frozen=True)
class MeasurementPlan:
    """What a scan should do about a row the read-only filter could not settle."""

    verdict: CountryVerdict
    drop: bool
    judge_required: bool = False
    suggest_job: bool = False
    reason: str = PLAN_READY

    def as_dict(self):
        return {'drop': self.drop, 'judge_required': self.judge_required,
                'suggest_job': self.suggest_job, 'reason': self.reason,
                'verdict': self.verdict.as_dict()}


def plan_measurement(criterion, endpoint=None, exit=None, now=None):
    """Decide whether to drop a candidate or to measure it instead.

    A read-only filter never starts a network call; it only says which rows a
    judge probe could still turn into a fact.  An endpoint in Germany is never
    dropped for failing an exit-country criterion - the exit is simply unknown
    until a judge measures it.
    """
    verdict = evaluate(criterion, endpoint, exit, now)
    if verdict.verified or (verdict.matched and not verdict.unknown):
        return MeasurementPlan(verdict, drop=False)
    if verdict.unknown:
        if criterion.basis in (BASIS_EXIT, BASIS_EITHER):
            return MeasurementPlan(verdict, drop=False, judge_required=True, suggest_job=True,
                                   reason=PLAN_JUDGE_FOR_EXIT)
        return MeasurementPlan(verdict, drop=verdict.reason == REASON_UNKNOWN_EXCLUDED,
                               judge_required=criterion.unknown != UNKNOWN_EXCLUDE,
                               suggest_job=True, reason=PLAN_JUDGE_FOR_UNKNOWN)
    return MeasurementPlan(verdict, drop=True, reason=PLAN_DROP)


# --- ASN, organisation, CIDR -----------------------------------------------------


@dataclass(frozen=True)
class ProviderFact:
    """Provider characteristics of an address, with the basis of the hosting flag."""

    asn: int | None = None
    organization: str | None = None
    hosting: bool | None = None
    hosting_basis: str = HOSTING_BASIS_NONE
    cidr: str | None = None
    ip_version: int | None = None
    at: float | None = None
    database_version: str | None = None

    @classmethod
    def of(cls, organization, asn=None, at=None, database_version=None, address=None, cidr=None):
        """Build from what the ASN database reports, marking hosting as a heuristic."""
        hosting = geoip.is_hosting(organization) if organization else None
        return cls(asn=asn, organization=organization or None, hosting=hosting,
                   hosting_basis=HOSTING_BASIS_ORG_NAME if organization else HOSTING_BASIS_NONE,
                   cidr=cidr, ip_version=classify_host(address).ip_version if address else None,
                   at=at, database_version=database_version)

    def claims(self):
        """What may be said about this address, and what may not.

        ``residential`` and ``mobile`` are always None: the name heuristic
        proves neither, and a consumer that reads them as booleans would be
        reading a claim nobody measured.
        """
        return {'hosting': self.hosting, 'hosting_basis': self.hosting_basis,
                'residential': None, 'mobile': None, 'verified': False,
                'note': HOSTING_CLAIM_NOT_RESIDENTIAL}

    def as_dict(self):
        return {'asn': self.asn, 'provider': self.organization, 'hosting': self.hosting,
                'hosting_basis': self.hosting_basis, 'cidr': self.cidr,
                'ip_version': self.ip_version}


def range_cidr(first, last=None):
    """Smallest network that contains a published range.

    DB-IP Lite gives address ranges, not prefixes, so this is the smallest
    network that covers the range and not a routed prefix; it is reported under
    the name ``range_cidr`` wherever a prefix would be a false promise.  Accepts
    addresses or whole networks, and a single network on its own.
    """
    if last is None:
        if not isinstance(first, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
            first = ipaddress.ip_network(first, strict=False)
        return str(first)
    first = int(getattr(first, 'network_address', first))
    last = int(getattr(last, 'broadcast_address', last))
    if first > last:
        first, last = last, first
    version = ipaddress.ip_address(first).version
    for bits in range(32 if version == 4 else 128, -1, -1):
        candidate = ipaddress.ip_network((first, bits), strict=False)
        if int(candidate.network_address) <= first and int(candidate.broadcast_address) >= last:
            return str(candidate)
    raise ValueError('Диапазон адресов не помещается в одну сеть.')


class ProviderIndex:
    """ASN/organisation ranges with the CIDR of the range that matched.

    Takes the same ``(first, last, asn, organization)`` rows as ``geoip``'s ASN
    parser, so the same CSV file feeds both and neither becomes a second source
    of truth.  ``provider_of`` reads the existing ``AsnDB`` when a range is not
    needed.
    """

    def __init__(self, rows=(), database_version=None):
        self.v4, self.v6 = [], []
        for first, last, asn, organization in rows:
            (self.v4 if first.version == 4 else self.v6).append(
                (int(first), int(last), int(asn), (organization or '').strip()[:200]))
        self.v4.sort()
        self.v6.sort()
        self.v4_first = [r[0] for r in self.v4]
        self.v6_first = [r[0] for r in self.v6]
        self.database_version = database_version
        self.size = len(self.v4) + len(self.v6)

    @classmethod
    def from_file(cls, path, database_version=None):
        return cls(_read_asn_rows(Path(path)), database_version)

    def lookup(self, address):
        try:
            ip = ipaddress.ip_address(str(address).strip('[]'))
        except ValueError:
            return None
        rows, firsts = (self.v4, self.v4_first) if ip.version == 4 else (self.v6, self.v6_first)
        value = int(ip)
        position = bisect_right(firsts, value) - 1
        if position >= 0 and value <= rows[position][1]:
            first, last, asn, organization = rows[position]
            return ProviderFact.of(organization, asn=asn, address=str(ip),
                                   cidr=range_cidr(first, last), database_version=self.database_version)
        return None

    def provider_of(self, proxy):
        return self.lookup(endpoint_host(proxy))


def _read_asn_rows(path):
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8', newline='') as handle:
        for record in csv.reader(handle):
            if len(record) < 4 or not record[2].strip().isdigit():
                continue
            try:
                first = ipaddress.ip_address(record[0].strip())
                last = ipaddress.ip_address(record[1].strip())
            except ValueError:
                continue
            if first.version == last.version and int(first) <= int(last) and 0 < int(record[2]) < 2 ** 32:
                yield first, last, int(record[2]), record[3].strip()[:200]


# --- Destination capabilities ----------------------------------------------------


@dataclass(frozen=True)
class TargetCapabilities:
    """Which address families a destination was actually reached over.

    Separate from the endpoint's own family on purpose: an IPv6 endpoint can
    reach IPv4 destinations and the other way round, so the two are never
    collapsed into one field.  ``None`` means "not measured", not "no".
    """

    ipv4_destination: bool | None = None
    ipv6_destination: bool | None = None
    at: float | None = None
    source: str = UNMEASURED

    def as_dict(self):
        return {'ipv4_destination': self.ipv4_destination, 'ipv6_destination': self.ipv6_destination,
                'at': self.at, 'source': self.source}

    @classmethod
    def from_samples(cls, samples, at=None):
        """Read per-target samples that recorded the destination family they used."""
        seen = {}
        for sample in samples or ():
            if not isinstance(sample, dict):
                continue
            version = sample.get('ip_version')
            version = int(version) if isinstance(version, int) and version in (4, 6) else None
            if version is None:
                continue
            if version not in seen or sample.get('ok'):
                seen[version] = bool(sample.get('ok'))
        if not seen:
            return cls(at=at)
        return cls(ipv4_destination=seen.get(4), ipv6_destination=seen.get(6), at=at, source='measured')


# --- Pool quotas: the data interface, storage stays in pools.py ------------------


@dataclass(frozen=True)
class ExitObservation:
    """One observed exit: the address, its country and when it was seen."""

    address: str | None
    code: str | None = None
    at: float | None = None

    @property
    def known(self):
        return bool(self.address)


def _within(at, now, max_age_seconds):
    """True when a dated observation is recent enough; undated counts as fresh only
    when the caller asked for no limit, so a limit never passes on missing data."""
    if max_age_seconds is None:
        return True
    return at is not None and now is not None and 0 <= float(now) - float(at) <= float(max_age_seconds)


@dataclass(frozen=True)
class QuotaStatus:
    """How a pool quota stands, and whether the evidence is even sufficient."""

    basis: str
    desired: int
    confirmed: int
    unknown: int
    shortfall: int
    confirmable: bool
    satisfied: bool
    reason: str

    def as_dict(self):
        return {'basis': self.basis, 'desired': self.desired, 'confirmed': self.confirmed,
                'unknown': self.unknown, 'shortfall': self.shortfall, 'confirmable': self.confirmable,
                'satisfied': self.satisfied, 'reason': self.reason}


def quota_status(basis, desired, endpoints=(), exits=(), now=None, max_age_seconds=None):
    """Count a pool quota from endpoints or from confirmed unique exits.

    With ``exit_ips`` an unknown exit confirms nothing: the status stays
    ``confirmable=False`` until every counted item has a known exit, so a pool
    never reports its target reached on the strength of a guess.
    """
    if basis not in (QUOTA_ENDPOINTS, QUOTA_EXIT_IPS):
        raise ValueError('Квоты: основание должно быть endpoints или exit_ips.')
    wanted = max(0, int(desired or 0))
    if basis == QUOTA_ENDPOINTS:
        confirmed = len({str(item) for item in endpoints or () if item})
        return QuotaStatus(basis, wanted, confirmed, 0, max(0, wanted - confirmed), True,
                           confirmed >= wanted, REASON_QUOTA_ENDPOINTS)

    seen, unknown = set(), 0
    for item in exits or ():
        observation = item if isinstance(item, ExitObservation) else \
            ExitObservation(str(item or '') or None, at=now)
        if observation.known and _within(observation.at, now, max_age_seconds):
            seen.add(observation.address)
        else:
            unknown += 1
    confirmed = len(seen)
    confirmable = unknown == 0
    reason = REASON_QUOTA_EXIT_CONFIRMED if confirmable else REASON_QUOTA_EXIT_UNKNOWN
    return QuotaStatus(basis, wanted, confirmed, unknown, max(0, wanted - confirmed), confirmable,
                       confirmable and confirmed >= wanted, reason)


# --- One resolver for rows, GUI, API and export ----------------------------------


class Resolver:
    """Turn a proxy string or a stored row into facts, using local databases only.

    Wraps the existing ``geoip`` databases, so there is no second reader and no
    network access.  ``declared`` carries the country a source published for an
    endpoint (the ``candidate_meta.country`` of today), which is a claim of the
    list, not a measurement.
    """

    def __init__(self, country_db=None, provider_db=None, declared=None, now=None, database_version=None,
                 max_age_seconds=None):
        self.country_db = country_db
        self.provider_db = provider_db if isinstance(provider_db, ProviderIndex) else None
        self.asn_db = None if isinstance(provider_db, ProviderIndex) else provider_db
        self.declared = dict(declared or {})
        self.now = time.time() if now is None else float(now)
        self.database_version = database_version
        self.max_age_seconds = max_age_seconds

    def _country_of_address(self, address):
        if self.country_db is None:
            return None
        return self.country_db.lookup(address)

    def endpoint_fact(self, proxy, resolved_ip=None, resolved_at=None, declared=None):
        """Country of the address the client connects to.

        A hostname without a recorded resolution has no geography of its own:
        its country comes from the address that was really used, together with
        the time that address was used.  A country published by the source list
        is kept as a claim of that list, and stays undated for a hostname, so a
        caller that asks about freshness does not receive it as current.
        """
        declared = self.declared.get(proxy) if declared is None else declared
        info = classify_host(proxy)
        address, at, source = None, self.now, None
        if resolved_ip:
            address, at, source = str(resolved_ip), resolved_at, SOURCE_RESOLVED
        elif info.address:
            address, source = info.address, SOURCE_GEOIP
        facts = []
        if declared:
            facts.append(CountryFact.of(declared, SOURCE_SOURCE,
                                        at=at if source else None, address=address or info.host))
        if address:
            looked_up = self._country_of_address(address)
            if looked_up:
                facts.append(CountryFact.of(looked_up, source, at=at, address=address,
                                            database_version=self.database_version))
            elif not declared:
                facts.append(CountryFact.unknown(SOURCE_UNRESOLVED, address=address,
                                                 at=at if source == SOURCE_RESOLVED else None))
        else:
            facts.append(CountryFact.unknown(SOURCE_UNRESOLVED, address=info.host))
        merged = merge_facts(facts, self.now, self.max_age_seconds)
        return merged.fact or next((f for f in merged.others if not f.known), None) or merged.others[0]

    def exit_fact(self, exit_ip, at=None, declared=None):
        """Country of the address a judge saw."""
        if not exit_ip:
            return CountryFact.unknown(SOURCE_OBSERVED_EXIT)
        code = self._country_of_address(exit_ip) or declared
        return CountryFact.of(code, SOURCE_OBSERVED_EXIT, at=at, address=str(exit_ip),
                              database_version=self.database_version)

    def provider_fact(self, proxy):
        """ASN, organisation and the hosting heuristic for an address."""
        host = endpoint_host(proxy)
        if self.provider_db is not None:
            fact = self.provider_db.lookup(host)
            return fact if fact else ProviderFact(ip_version=classify_host(proxy).ip_version)
        if self.asn_db is not None:
            found = self.asn_db.lookup(host) or {}
            return ProviderFact.of(found.get('org'), asn=found.get('asn'), at=self.now,
                                   database_version=self.database_version, address=host)
        return ProviderFact(ip_version=classify_host(proxy).ip_version)

    def from_row(self, row):
        """``(endpoint, exit, provider)`` for a stored row, without touching the network.

        The row's own ``country`` is a claim of the list that published it, and
        it is read here as one.  It was not: a row carrying ``country='NL'``
        whose address is absent from the GeoIP database came out of this method
        as unknown and was dropped by every criterion, while
        ``proxytool.geo_country_verdict`` - the same criterion, the same row -
        matched it from the row.  The GUI list and the export disagreed about
        one filter, which is the parity F08 requires; both now compare the same
        claim with the same source and the same precedence.
        """
        row = row or {}
        proxy = row.get('proxy') or ''
        checked_at = row.get('checked_at')
        mapped = self.declared.get(proxy)
        endpoint = self.endpoint_fact(proxy, declared=mapped if mapped else (row.get('country') or None))
        if endpoint is not None and endpoint.source in (SOURCE_GEOIP, SOURCE_SOURCE) and checked_at:
            # The row was measured at checked_at, so that is when this knowledge was obtained.
            endpoint = replace(endpoint, at=float(checked_at))
        exit_ip = row.get('exit_ip') or ((row.get('anonymity') or {}).get('exit_ip')
                                        if isinstance(row.get('anonymity'), dict) else None)
        exit_claim = row.get('exit_country')
        exit_fact = self.exit_fact(exit_ip, at=checked_at) if exit_ip else \
            CountryFact.of(exit_claim, SOURCE_OBSERVED_EXIT, at=checked_at) if exit_claim else None
        return endpoint, exit_fact, self.provider_fact(proxy)

    def describe(self, row, criterion=None, now=None):
        """One row as the fields of CONTRACTS §4.4 plus the provenance F08 adds."""
        endpoint, exit_fact, provider = self.from_row(row)
        moment = self.now if now is None else float(now)
        verdict = evaluate(criterion, endpoint, exit_fact, moment) if criterion is not None else None
        judged = (row or {}).get('anonymity') or {}
        samples = (row or {}).get('samples') or ()
        description = {
            'proxy': (row or {}).get('proxy'),
            'ip_version': endpoint_ip_version((row or {}).get('proxy') or ''),
            'country': endpoint.code if endpoint else None,
            'exit_ip': (row or {}).get('exit_ip') or judged.get('exit_ip') or None,
            'exit_country': exit_fact.code if exit_fact else None,
            'asn': provider.asn,
            'provider': provider.organization,
            'hosting': provider.hosting,
            'hosting_basis': provider.hosting_basis,
            'cidr': provider.cidr,
            'target_capabilities': TargetCapabilities.from_samples(samples, at=(row or {}).get('checked_at')).as_dict(),
        }
        description.update(endpoint.as_dict(moment, self.max_age_seconds) if endpoint else
                          CountryFact.unknown(SOURCE_UNRESOLVED).as_dict(moment, self.max_age_seconds))
        if exit_fact:
            description.update({'exit_country_source': exit_fact.source, 'exit_country_at': exit_fact.at,
                                'exit_country_stale': exit_fact.is_stale(moment, self.max_age_seconds)})
        else:
            description.update({'exit_country_source': None, 'exit_country_at': None, 'exit_country_stale': True})
        if verdict is not None:
            description.update({'country_matched': verdict.matched, 'country_verified': verdict.verified,
                                'country_reason': verdict.reason, 'country_conflict': verdict.conflict,
                                'country_needs_measurement': verdict.needs_measurement})
        return description


@dataclass(frozen=True)
class FilterResult:
    """Outcome of applying one criterion to a set of rows.  Read-only, no network."""

    kept: tuple = ()
    dropped: tuple = ()
    reasons: dict = field(default_factory=dict)
    unknown: int = 0
    verified: int = 0
    total: int = 0
    criterion_digest: str = ''

    def as_dict(self):
        return {'kept': len(self.kept), 'dropped': len(self.dropped), 'total': self.total,
                'unknown': self.unknown, 'verified': self.verified, 'reasons': dict(self.reasons),
                'criterion_digest': self.criterion_digest}


def filter_rows(rows, criterion, resolver=None, now=None):
    """Apply one criterion to rows and report why each verdict came out.

    The same function backs the GUI list, the read-only API and the export, so
    one set of rows gives one answer in all three places.  Rows are not
    modified, and no network call happens here: an unknown exit is reported as
    unknown, never measured.

    Without a ``resolver`` the filter has no knowledge at all and says so
    instead of guessing - every row is compared as unknown.  A caller that has
    rows with a country in them passes a :class:`Resolver`; it reads the row's
    own ``country`` and ``exit_country`` as the claims they are, so the answer
    matches the export engine, which builds the same facts
    (``proxytool.geo_country_verdict``).
    """
    if not isinstance(criterion, CountryCriterion):
        criterion = parse_criterion(criterion)
    kept, dropped, reasons = [], [], {}
    unknown = verified = 0
    for row in rows or ():
        endpoint = exit_fact = None
        if resolver is not None:
            endpoint, exit_fact, _provider = resolver.from_row(row)
        verdict = evaluate(criterion, endpoint, exit_fact, now)
        reasons[verdict.reason] = reasons.get(verdict.reason, 0) + 1
        if verdict.unknown:
            unknown += 1
        if verdict.verified:
            verified += 1
        (kept if verdict.matched else dropped).append(row)
    return FilterResult(tuple(kept), tuple(dropped), reasons, unknown, verified, len(kept) + len(dropped),
                        criterion.digest())


# --- GeoIP database status, version and installation -----------------------------


@dataclass(frozen=True)
class DatabaseStatus:
    """What the local geodata is: present, which version, how old, and honest about gaps."""

    kind: str
    present: bool
    path: str | None = None
    version: str | None = None
    installed_at: float | None = None
    age_seconds: float | None = None
    bytes: int | None = None
    ranges: int | None = None
    stale: bool = False
    attribution: str = ''
    error: str | None = None

    def as_dict(self):
        return {'kind': self.kind, 'available': self.present, 'path': self.path, 'version': self.version,
                'installed_at': self.installed_at, 'age_seconds': self.age_seconds, 'bytes': self.bytes,
                'ranges': self.ranges, 'stale': self.stale, 'attribution': self.attribution,
                'error': self.error}

    def version_label(self):
        """What a user-facing status line shows, including the absence of a version."""
        if not self.present:
            return 'absent'
        return self.version or 'unknown-version'


def meta_path(path):
    """Sidecar that records which version of the geodata is installed."""
    path = Path(path)
    return path.with_name(path.name + META_SUFFIX)


def read_meta(path):
    """Recorded version and install time, or an empty mapping when there is none."""
    try:
        data = json.loads(meta_path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_meta(path, kind, version, at=None):
    """Record the installed version.  Written atomically, like the database itself."""
    payload = {'kind': kind, 'version': version, 'installed_at': time.time() if at is None else float(at)}
    target = meta_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, target)
    return payload


def database_status(path, kind=COUNTRY_DATABASE, now=None, database=None, max_age_seconds=DEFAULT_DATABASE_MAX_AGE):
    """Status of one local geodata file.

    ``database`` is the already loaded ``geoip`` object; passing it reports the
    number of ranges without reading the file twice.  A file without a recorded
    version is reported with ``version=None`` and ``stale=True``: an update from
    before version tracking was introduced is not a current database.
    """
    moment = time.time() if now is None else float(now)
    path = Path(path) if path else None
    meta = read_meta(path) if path else {}
    try:
        stat = path.stat() if path else None
    except OSError:
        stat = None
    if stat is None:
        return DatabaseStatus(kind, False, str(path) if path else None, attribution=geoip.ATTRIBUTION,
                              error='GEOIP_ABSENT')
    installed_at = meta.get('installed_at')
    if not isinstance(installed_at, (int, float)):
        installed_at = float(stat.st_mtime)
    age = max(0.0, moment - float(installed_at))
    version = meta.get('version') if isinstance(meta.get('version'), str) else None
    return DatabaseStatus(kind, True, str(path), version, float(installed_at), age, stat.st_size,
                          getattr(database, 'size', None), version is None or age > max_age_seconds,
                          geoip.ATTRIBUTION)


def install_database(path, body, kind, version, validate, at=None):
    """Put a downloaded database in place, keeping the last working one on failure.

    The download itself belongs to ``proxytool.download_geoip``; this function
    only validates and moves bytes, so a damaged update never leaves the user
    without a database.  Returns the recorded version.
    """
    validate(body)                      # raises before anything on disk is touched
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_bytes(body)
    os.replace(temporary, path)
    write_meta(path, kind, version, at=at)
    return version


def geo_status(data, now=None, country_db=None, asn_db=None):
    """Status of both databases, the shape a GUI or ``/v1`` endpoint can serve."""
    return {
        'country': database_status(geoip.default_path(data), COUNTRY_DATABASE, now, country_db).as_dict(),
        'asn': database_status(geoip.asn_path(data), ASN_DATABASE, now, asn_db).as_dict(),
        'attribution': geoip.ATTRIBUTION,
    }
