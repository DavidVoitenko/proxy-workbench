"""Offline country lookup for proxy addresses.

Uses the free DB-IP "IP to Country Lite" CSV (CC BY 4.0, https://db-ip.com):
lines of ``first_ip,last_ip,CC`` for IPv4 and IPv6. The file is downloaded
only on explicit request and stored in the local data folder; lookups never
touch the network.
"""
from __future__ import annotations

from array import array
from bisect import bisect_right
import csv
import datetime
import gzip
import io
import ipaddress
import re
from pathlib import Path

DB_NAME = 'dbip-country-lite.csv.gz'
DOWNLOAD_URL = 'https://download.db-ip.com/free/dbip-country-lite-{month}.csv.gz'
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
ATTRIBUTION = 'IP geolocation by DB-IP (https://db-ip.com), CC BY 4.0'
COUNTRY_CODE = re.compile(r'[A-Z]{2}')


def default_path(data):
    return Path(data) / 'geoip' / DB_NAME


def parse_countries(value):
    """Normalize "de, NL" or ["de", "nl"] to a sorted tuple of ISO codes."""
    if value in (None, ''):
        return ()
    items = value.split(',') if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        raise ValueError('Страны: ожидается список ISO-кодов, например DE,NL.')
    codes = set()
    for item in items:
        code = str(item).strip().upper()
        if not code:
            continue
        if not COUNTRY_CODE.fullmatch(code):
            raise ValueError('Страны: используйте двухбуквенные ISO-коды, например DE,NL.')
        codes.add(code)
    return tuple(sorted(codes))


def proxy_host(proxy):
    """Host part of a proxy, in a URL or in a bare ``host:port`` form.

    A row that lost its scheme (``5.9.1.2:8080``, ``[2001:db8::1]:1080``)
    used to produce an empty host and therefore a silent ``unknown`` country,
    which is the one thing a country filter must never do.  The bracketed IPv6
    form keeps its brackets off.
    """
    text = str(proxy or '').strip()
    if '://' in text:
        text = text.partition('://')[2]
    # Credentials before the host must not become the host.
    if '@' in text:
        text = text.rpartition('@')[2]
    if text.startswith('['):
        end = text.find(']')
        return text[1:end] if end > 0 else text.lstrip('[')
    if ':' in text:
        head, _, tail = text.rpartition(':')
        # A bare IPv6 literal has more than one colon and no port.
        if head.count(':') or not tail.isdigit():
            return text
        return head
    return text


class CountryDB:
    """Sorted IP ranges with binary search; IPv4 kept in compact arrays."""

    def __init__(self, rows=(), source=None):
        v4, v6 = [], []
        for first, last, code in rows:
            (v4 if first.version == 4 else v6).append((int(first), int(last), code))
        v4.sort()
        v6.sort()
        self.v4_first = array('L', (r[0] for r in v4))
        self.v4_last = array('L', (r[1] for r in v4))
        self.v4_code = [r[2] for r in v4]
        self.v6_first = [r[0] for r in v6]
        self.v6_last = [r[1] for r in v6]
        self.v6_code = [r[2] for r in v6]
        self.source = source
        self.size = len(v4) + len(v6)

    @classmethod
    def from_file(cls, path):
        path = Path(path)
        opener = gzip.open if path.suffix == '.gz' else open
        with opener(path, 'rt', encoding='utf-8', newline='') as handle:
            return cls(_parse_rows(handle), source=path)

    @classmethod
    def load_optional(cls, path):
        """The database, or None when the file does not exist."""
        return cls.from_file(path) if path and Path(path).is_file() else None

    def lookup(self, address):
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return None
        value = int(ip)
        first, last, codes = ((self.v4_first, self.v4_last, self.v4_code) if ip.version == 4
                              else (self.v6_first, self.v6_last, self.v6_code))
        index = bisect_right(first, value) - 1
        if index >= 0 and value <= last[index]:
            return codes[index]
        return None

    def country_of(self, proxy):
        return self.lookup(proxy_host(proxy))


def _parse_rows(handle):
    for record in csv.reader(handle):
        if len(record) < 3:
            continue
        code = record[2].strip().upper()
        if not COUNTRY_CODE.fullmatch(code) or code == 'ZZ':
            continue
        try:
            first, last = ipaddress.ip_address(record[0].strip()), ipaddress.ip_address(record[1].strip())
        except ValueError:
            continue
        if first.version == last.version and int(first) <= int(last):
            yield first, last, code


def candidate_months(today=None):
    """The current and previous month; DB-IP publishes at the start of a month."""
    today = today or datetime.date.today()
    previous = (today.replace(day=1) - datetime.timedelta(days=1))
    return [today.strftime('%Y-%m'), previous.strftime('%Y-%m')]


def validate_download(body):
    """Check that a downloaded body is a gzip CSV with country ranges."""
    if len(body) > MAX_DOWNLOAD_BYTES:
        raise ValueError('GEOIP_TOO_LARGE')
    try:
        with gzip.open(io.BytesIO(body), 'rt', encoding='utf-8', newline='') as handle:
            head = [line for _, line in zip(range(50), handle)]
    except (OSError, EOFError, UnicodeDecodeError) as exc:
        raise ValueError('GEOIP_INVALID') from exc
    if not any(True for _ in _parse_rows(head)):
        raise ValueError('GEOIP_INVALID')


# Provider (ASN) database: DB-IP "IP to ASN Lite", lines of first_ip,last_ip,asn,organization.
ASN_NAME = 'dbip-asn-lite.csv.gz'
ASN_URL = 'https://download.db-ip.com/free/dbip-asn-lite-{month}.csv.gz'
# Organisations that rent servers rather than serve homes; sites block these ranges more often.
HOSTING = re.compile(
    r'host|cloud|server|data ?cent|datacamp|\bvps\b|dedicated|colo|amazon|\baws\b|google|microsoft|azure|'
    r'digitalocean|digital ocean|\bovh|hetzner|linode|akamai|vultr|choopa|contabo|alibaba|tencent|oracle|'
    r'leaseweb|\bm247\b|scaleway|ionos|hostinger|cdn77|g-core|gcore|psychz|quadranet|zenlayer|frantech|'
    r'buyvm|kamatera|upcloud|serverius|ipxo|cogent|stark industries|aeza|pq hosting|timeweb|selectel', re.I)


def asn_path(data):
    return Path(data) / 'geoip' / ASN_NAME


def is_hosting(organization):
    return bool(organization and HOSTING.search(organization))


class AsnDB:
    """Sorted IP ranges mapped to (AS number, organisation) with binary search."""

    def __init__(self, rows=(), source=None):
        v4, v6 = [], []
        self.organizations = []
        index = {}
        for first, last, asn, organization in rows:
            slot = index.setdefault(organization, len(index))
            if slot == len(self.organizations):
                self.organizations.append(organization)
            (v4 if first.version == 4 else v6).append((int(first), int(last), asn, slot))
        v4.sort()
        v6.sort()
        self.v4_first = array('L', (r[0] for r in v4))
        self.v4_last = array('L', (r[1] for r in v4))
        self.v4_asn = array('L', (r[2] for r in v4))
        self.v4_org = array('L', (r[3] for r in v4))
        self.v6_first = [r[0] for r in v6]
        self.v6_last = [r[1] for r in v6]
        self.v6_asn = array('L', (r[2] for r in v6))
        self.v6_org = array('L', (r[3] for r in v6))
        self.source = source
        self.size = len(v4) + len(v6)

    @classmethod
    def from_file(cls, path):
        path = Path(path)
        opener = gzip.open if path.suffix == '.gz' else open
        with opener(path, 'rt', encoding='utf-8', newline='') as handle:
            return cls(_parse_asn_rows(handle), source=path)

    @classmethod
    def load_optional(cls, path):
        return cls.from_file(path) if path and Path(path).is_file() else None

    def lookup(self, address):
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return None
        value = int(ip)
        if ip.version == 4:
            first, last, asns, orgs = self.v4_first, self.v4_last, self.v4_asn, self.v4_org
        else:
            first, last, asns, orgs = self.v6_first, self.v6_last, self.v6_asn, self.v6_org
        position = bisect_right(first, value) - 1
        if position >= 0 and value <= last[position]:
            organization = self.organizations[orgs[position]]
            return {'asn': asns[position], 'org': organization, 'hosting': is_hosting(organization)}
        return None

    def provider_of(self, proxy):
        return self.lookup(proxy_host(proxy))


def _parse_asn_rows(handle):
    for record in csv.reader(handle):
        if len(record) < 4 or not record[2].strip().isdigit():
            continue
        try:
            first, last = ipaddress.ip_address(record[0].strip()), ipaddress.ip_address(record[1].strip())
        except ValueError:
            continue
        asn = int(record[2])
        if first.version == last.version and int(first) <= int(last) and 0 < asn < 2 ** 32:
            yield first, last, asn, record[3].strip()[:200]


def validate_asn_download(body):
    """Check that a downloaded body is a gzip CSV with provider ranges."""
    if len(body) > MAX_DOWNLOAD_BYTES:
        raise ValueError('GEOIP_TOO_LARGE')
    try:
        with gzip.open(io.BytesIO(body), 'rt', encoding='utf-8', newline='') as handle:
            head = [line for _, line in zip(range(50), handle)]
    except (OSError, EOFError, UnicodeDecodeError) as exc:
        raise ValueError('GEOIP_INVALID') from exc
    if not any(True for _ in _parse_asn_rows(head)):
        raise ValueError('GEOIP_INVALID')
