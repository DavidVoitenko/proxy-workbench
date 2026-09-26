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
``unknown``      the judge request through the proxy failed, or the answer
                 cannot prove anything (empty body, CAPTCHA page, no address).

The machine's own IPs are kept in memory only and never written to the
database or exports.

Defect 13: this module delegates the verdict to :func:`probes.classify_echo`,
so ``elite`` is reachable only through an answer that actually proves
something.  The rules live in one place (``probes.py``) and this file keeps
the historical call shape for ``proxytool.py`` / ``api.py`` / ``gui.py``.
"""
from __future__ import annotations

import ipaddress
import re
import time
from urllib.parse import urlsplit

from . import probes

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


def extract_public_ips(text, *, global_only=True):
    """IPv4/IPv6 addresses mentioned in a judge response.

    ``global_only=True`` keeps the historical behaviour the public judge needs.
    A self-hosted judge echoes loopback or LAN addresses, so the caller can
    pass ``global_only=False``; :func:`probes.extract_addresses` is the same
    thing without a filter and is what :func:`classify` uses.
    """
    addresses = probes.extract_addresses(text)
    if not global_only:
        return set(addresses)
    found = set()
    for item in addresses:
        try:
            if ipaddress.ip_address(item).is_global:
                found.add(item)
        except ValueError:
            continue
    return found


def classify_detail(body, own_ips, *, judge_verified=True, spec=None):
    """Classify one judge echo through the one classifier (defect 13).

    ``own_ips`` is the bootstrap set from the direct judge request.
    ``judge_verified`` must be ``False`` whenever that direct request did not
    actually show one of our addresses: an answer that does not leak is then
    no evidence at all, and the level stays ``unknown`` instead of ``elite``.

    Returns the level, the signals, the ``code`` that explains a non-verdict
    (``JUDGE_INVALID`` / ``JUDGE_CHALLENGE`` / ``JUDGE_UNVERIFIED``), whether
    the level is confirmed, and the exit address when the judge labelled one.
    """
    outcome = probes.classify_echo(body, own_ips, judge_verified=judge_verified, spec=spec)
    value = {"level": outcome.level, "signals": list(outcome.signals), "code": outcome.code,
             "confirmed": outcome.confirmed}
    if outcome.exit_ip:
        value["exit_ip"] = outcome.exit_ip
    return value


def classify(body, own_ips, *, judge_verified=True, spec=None):
    """The historical two-key verdict, now produced by the one classifier.

    ``classify`` keeps its old shape so every existing caller keeps working;
    :func:`classify_detail` is the same verdict plus the code that explains an
    ``unknown`` and the observed exit address.
    """
    detail = classify_detail(body, own_ips, judge_verified=judge_verified, spec=spec)
    return {"level": detail["level"], "signals": detail["signals"]}


_EXIT_IP = re.compile(r'(?:remote[-_ ]addr|client[-_ ]ip|"origin"|"ip")["\']?\s*(?:=>|[:=])\s*["\']?([0-9a-f:.]{3,45})', re.I)


def exit_ip(body, *, global_only=True):
    """The address the judge saw the request coming from, when it labels it.

    ``global_only=False`` is what a self-hosted judge needs: it sees and labels
    a loopback or LAN address for the request it received.
    """
    text = body.decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else str(body)
    for match in _EXIT_IP.findall(text):
        try:
            ip = ipaddress.ip_address(match.strip(".:" ))
        except ValueError:
            continue
        if global_only and not ip.is_global:
            continue
        return ip.compressed
    return None


def allows(row, minimum):
    """True when a result meets the minimum anonymity level."""
    if minimum in (None, "any"):
        return True
    level = (row.get("anonymity") or row).get("level", "unknown")
    return LEVEL_RANK.get(level, -1) >= LEVEL_RANK[minimum]


async def fetch_judge(client, url, headers, max_bytes=MAX_JUDGE_BYTES):
    """Stream a judge answer with a hard byte budget.

    An answer over the budget is an error, not a truncated page: a partial
    echo cannot prove that an address is missing from it.
    """
    async with client.stream("GET", url, headers=headers) as response:
        if not 200 <= response.status_code < 300:
            raise ValueError(f"HTTP_{response.status_code}")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ValueError("BODY_TOO_LARGE")
        return bytes(body)


def result(level, signals=(), error=None, started=None, exit_address=None, code=None):
    value = {"level": level, "signals": list(signals), "checked_at": time.time()}
    if exit_address:
        value["exit_ip"] = exit_address
    if code:
        value["code"] = code
    if error:
        value["error"] = error
    if started is not None:
        value["ms"] = round((time.monotonic() - started) * 1000, 2)
    return value
