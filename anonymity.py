"""Proxy anonymity classification through a user-selected judge URL.

A judge is any HTTP(S) endpoint that echoes the request it received: the
client IP and request headers (for example an ``httpbin``-style ``/get``
endpoint or an ``azenv``-style script). The workbench fetches the judge once
directly to learn which public IPs belong to this machine, then once through
each working proxy and classifies the echo:

``transparent``  the echo contains one of this machine's public IPs;
``anonymous``    no IP leak, but proxy-revealing headers (``Via``,
                 ``X-Forwarded-For``, ...) are present;
``elite``        neither the IP nor proxy headers are visible;
``unknown``      the judge request through the proxy failed.

The machine's own IPs are kept in memory only and never written to the
database or exports.
"""
from __future__ import annotations

import ipaddress
import re
import time
from urllib.parse import urlsplit

LEVELS = ("transparent", "anonymous", "elite")
MIN_LEVELS = ("any", "anonymous", "elite")
LEVEL_RANK = {"unknown": -1, "transparent": 0, "anonymous": 1, "elite": 2}
DEFAULT_JUDGE_TIMEOUT = 10.0
MAX_JUDGE_BYTES = 256 * 1024

# Request headers that proxies add and judges echo back. Normalized to
# lowercase with "-" separators; CGI-style names (HTTP_X_FORWARDED_FOR) match
# through the optional "http-" prefix.
PROXY_HEADERS = (
    "via",
    "x-forwarded-for",
    "x-forwarded",
    "forwarded-for",
    "forwarded",
    "x-real-ip",
    "client-ip",
    "x-client-ip",
    "x-originating-ip",
    "x-proxy-id",
    "proxy-connection",
    "x-bluecoat-via",
)
_HEADER_PATTERNS = tuple(
    (name, re.compile(r'(?<![a-z0-9-])(?:http-)?' + re.escape(name) + r'["\']?\s*[:=]'))
    for name in PROXY_HEADERS
)
_IPV4 = re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])')
_IPV6 = re.compile(r'(?<![0-9a-f:])[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7}(?![0-9a-f:])', re.I)


def validate_judge(config):
    """Return a normalized anonymity config or None when the check is off."""
    if config is None:
        return None
    if not isinstance(config, dict):
        raise ValueError("anonymity: ожидается объект")
    url = config.get("judge_url")
    if url in (None, ""):
        return None
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("anonymity.judge_url: ожидается http(s) URL")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("anonymity.judge_url: нужен http(s) URL без userinfo")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("anonymity.judge_url: некорректный порт") from None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("anonymity.judge_url: некорректный порт")
    return {"judge_url": url}


def validate_min_level(value):
    if value not in MIN_LEVELS:
        raise ValueError("Уровень анонимности: any, anonymous или elite.")
    return value


def extract_public_ips(text):
    """Global IPv4/IPv6 addresses mentioned in a judge response."""
    found = set()
    for pattern in (_IPV4, _IPV6):
        for match in pattern.findall(text):
            try:
                ip = ipaddress.ip_address(match)
            except ValueError:
                continue
            if ip.is_global:
                found.add(ip.compressed)
    return found


def classify(body, own_ips):
    """Classify one judge echo. ``own_ips`` must be non-empty."""
    text = body.decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else str(body)
    seen = extract_public_ips(text)
    if seen & set(own_ips):
        return {"level": "transparent", "signals": ["real_ip"]}
    normalized = text.lower().replace("_", "-")
    signals = [name for name, pattern in _HEADER_PATTERNS if pattern.search(normalized)]
    if signals:
        return {"level": "anonymous", "signals": signals}
    return {"level": "elite", "signals": []}


def allows(row, minimum):
    """True when a result meets the minimum anonymity level."""
    if minimum in (None, "any"):
        return True
    level = (row.get("anonymity") or {}).get("level", "unknown")
    return LEVEL_RANK.get(level, -1) >= LEVEL_RANK[minimum]


async def fetch_judge(client, url, headers, max_bytes=MAX_JUDGE_BYTES):
    async with client.stream("GET", url, headers=headers) as response:
        if not 200 <= response.status_code < 300:
            raise ValueError(f"HTTP_{response.status_code}")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ValueError("BODY_TOO_LARGE")
        return bytes(body)


def result(level, signals=(), error=None, started=None):
    value = {"level": level, "signals": list(signals), "checked_at": time.time()}
    if error:
        value["error"] = error
    if started is not None:
        value["ms"] = round((time.monotonic() - started) * 1000, 2)
    return value
