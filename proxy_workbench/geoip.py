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
    host = str(proxy).partition('://')[2].rpartition(':')[0]
    return host[1:-1] if host.startswith('[') else host


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
