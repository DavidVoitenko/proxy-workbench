"""Local proxy denylists and optional DNSBL reputation checks."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import ipaddress
import json
import re
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit

from .anonymity import allows as anonymity_allows
from . import probes
from .probes import DNSBL_ACCESS, DNSBL_ERROR, DNSBL_QUOTA, DNSBL_TIMEOUT

MAX_ZONES = 12
MAX_TIMEOUT = 30.0
_ZONE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")


class Denylist:
    """Parsed local rules without any network or filesystem side effects."""

    def __init__(self, *, ips=(), networks=(), proxies=(), invalid=(), error=None):
        self.ips = set(ips)
        self.networks = set(networks)
        self.proxies = set(proxies)
        self.invalid = list(invalid)
        self.error = error
        keys = [f"ip:{ip}" for ip in self.ips]
        keys += [f"cidr:{network}" for network in self.networks]
        keys += [f"proxy:{proxy}" for proxy in self.proxies]
        self._digest = hashlib.sha256("\n".join(sorted(keys)).encode()).hexdigest()

    @classmethod
    def empty(cls):
        return cls()

    @classmethod
    def from_text(cls, text, normalizer=None):
        if not isinstance(text, str):
            return cls(error="INVALID_TEXT")
        ips, networks, proxies, invalid = [], [], [], []
        for raw in text.splitlines():
            value = raw.strip()
            if not value or value.startswith("#"):
                continue
            parsed = None
            try:
                if "/" in value:
                    parsed = ("cidr", ipaddress.ip_network(value, strict=False))
                else:
                    parsed = ("ip", ipaddress.ip_address(value))
            except ValueError:
                try:
                    normalized = normalizer(value) if normalizer else None
                except (TypeError, ValueError):
                    normalized = None
                if normalized:
                    parsed = ("proxy", normalized)
            if parsed is None:
                invalid.append(value[:160])
            elif parsed[0] == "ip":
                ips.append(parsed[1])
            elif parsed[0] == "cidr":
                networks.append(parsed[1])
            else:
                proxies.append(parsed[1])
        return cls(ips=ips, networks=networks, proxies=proxies, invalid=invalid)

    @classmethod
    def from_file(cls, path, normalizer=None):
        path = Path(path)
        try:
            return cls.from_text(path.read_text(encoding="utf-8"), normalizer=normalizer)
        except FileNotFoundError:
            return cls()
        except OSError:
            return cls(error="READ_ERROR")
        except UnicodeError:
            return cls(error="ENCODING_ERROR")

    @property
    def digest(self):
        return self._digest

    @property
    def rules(self):
        return len(self.ips) + len(self.networks) + len(self.proxies)

    def match(self, proxy):
        if not isinstance(proxy, str) or not proxy:
            return None
        if proxy in self.proxies:
            return "proxy"
        try:
            parsed = urlsplit(proxy)
            address = ipaddress.ip_address(parsed.hostname)
        except (TypeError, ValueError):
            return None
        if address in self.ips:
            return "ip"
        if any(address in network for network in self.networks):
            return "cidr"
        return None

    def public(self):
        return {"rules": self.rules, "invalid": len(self.invalid), "digest": self.digest, "error": self.error}


def normalize_zones(values):
    if values is None:
        return []
    if isinstance(values, str):
        values = re.split(r"[\s,]+", values.strip())
    if not isinstance(values, (list, tuple)):
        raise ValueError("DNSBL-зоны должны быть списком.")
    zones = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("DNSBL-зона должна быть строкой.")
        zone = value.strip().lower().rstrip(".")
        if not zone or len(zone) > 253 or '..' in zone or not _ZONE_RE.fullmatch(zone):
            raise ValueError("Некорректная DNSBL-зона.")
        if any(len(label) > 63 for label in zone.split('.')):
            raise ValueError("Некорректная DNSBL-зона.")
        if zone not in zones:
            zones.append(zone)
    if len(zones) > MAX_ZONES:
        raise ValueError(f"Максимум DNSBL-зон: {MAX_ZONES}.")
    return zones


def make_policy(settings=None, denylist=None, *, local_override=None, dnsbl_override=None,
                zones_override=None, timeout_override=None, strict_override=None):
    denylist = denylist or Denylist.empty()
    incoming = settings if isinstance(settings, dict) else {}
    local_enabled = incoming.get("local_enabled", True)
    if local_override is not None:
        local_enabled = local_override
    dnsbl_enabled = incoming.get("dnsbl_enabled", False)
    zones = incoming.get("dnsbl_zones", [])
    timeout = incoming.get("timeout", 2.5)
    strict = incoming.get("strict", False)
    if dnsbl_override is not None:
        dnsbl_enabled = dnsbl_override
    if zones_override is not None:
        zones = zones_override
    if timeout_override is not None:
        timeout = timeout_override
    if strict_override is not None:
        strict = strict_override
    if type(local_enabled) is not bool or type(dnsbl_enabled) is not bool or type(strict) is not bool:
        raise ValueError("Настройки проверки чистоты должны быть логическими.")
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        raise ValueError("Таймаут DNSBL должен быть числом.") from None
    if not 0.1 <= timeout <= MAX_TIMEOUT:
        raise ValueError("Таймаут DNSBL должен быть от 0.1 до 30 секунд.")
    zones = normalize_zones(zones)
    if dnsbl_enabled and not zones:
        dnsbl_enabled = False
    policy = {
        "local_enabled": local_enabled,
        "dnsbl_enabled": dnsbl_enabled,
        "dnsbl_zones": zones,
        "timeout": round(timeout, 3),
        "strict": strict,
        "denylist_digest": denylist.digest,
        "denylist_rules": denylist.rules,
        "denylist_invalid": len(denylist.invalid),
        "denylist_error": denylist.error,
    }
    policy["digest"] = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]
    return policy


def reverse_ip(address):
    """DNSBL query prefix for one address.

    IPv4 is reversed octet by octet.  IPv6 is reversed nibble by nibble into
    the 32 dotted labels ``ip6.arpa`` expects (defect 14): the previous code
    reversed ``IPv6Address.exploded`` with the colons still inside it, which
    produces ``0:0`` labels no zone can ever answer, so every IPv6 lookup came
    back as a false "clean".  The single implementation lives in ``probes``.
    """
    return probes.reverse_ip(address)


async def _lookup_dnsbl(query, timeout):
    """System resolver.  A missing name is NXDOMAIN, not an error."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(
            socket.getaddrinfo, query, None, type=socket.SOCK_STREAM), timeout)
    except socket.gaierror as exc:
        if exc.errno in (getattr(socket, 'EAI_NONAME', None), getattr(socket, 'EAI_NODATA', None)):
            return []
        raise probes.DnsQueryError(_dnsbl_error_code(exc)) from None
    except (TimeoutError, asyncio.TimeoutError):
        raise probes.DnsQueryError(DNSBL_TIMEOUT) from None
    except OSError as exc:
        raise probes.DnsQueryError(_dnsbl_error_code(exc)) from None


def _dnsbl_error_code(exc):
    """Tell a quota/access refusal from a plain resolver failure (defect 14).

    A resolver that answers "no such name" is NXDOMAIN and proves the address
    is not listed.  A resolver that answers "too many queries", "refused" or
    "not authorised" has told us nothing about the address, and the honest
    result is ``unknown`` with a code that says which case it was.
    """
    message = str(exc).lower()
    for needle, code in (('quota', DNSBL_QUOTA), ('rate limit', DNSBL_QUOTA), ('ratelimit', DNSBL_QUOTA),
                         ('too many', DNSBL_QUOTA), ('refused', DNSBL_ACCESS), ('not authori', DNSBL_ACCESS),
                         ('unauthori', DNSBL_ACCESS), ('denied', DNSBL_ACCESS), ('denied by', DNSBL_ACCESS),
                         ('blocked', DNSBL_ACCESS), ('refused by', DNSBL_ACCESS), ('forbidden', DNSBL_ACCESS)):
        if needle in message:
            return code
    if isinstance(exc, socket.gaierror):
        err = getattr(exc, 'errno', None)
        if err in (getattr(socket, 'EAI_AGAIN', None), getattr(socket, 'EAI_FAIL', None)):
            return DNSBL_ERROR
    return DNSBL_ERROR


def _legacy_records(value):
    """Pull address strings out of a ``getaddrinfo``-shaped answer."""
    found = []
    stack = list(value)
    while stack:
        item = stack.pop(0)
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return tuple(dict.fromkeys(found))


def _legacy_answer(value):
    """Adapt a legacy resolver seam to :class:`probes.DnsAnswer`.

    The historical seam (``socket.getaddrinfo`` results) carries no rcode, so
    an *empty* answer means "the resolver has no such name" — NXDOMAIN, which
    is the zone's ``clear``.  A seam that speaks the explicit ``{'rcode': …}``
    form is passed through unchanged, and there an empty ``NOERROR`` is the
    unknown it really is.
    """
    if value is None:
        return probes.DnsAnswer('NXDOMAIN', ())
    if isinstance(value, (list, tuple, set)):
        if not value:
            return probes.DnsAnswer('NXDOMAIN', ())
        return probes.DnsAnswer('NOERROR', _legacy_records(value))
    return probes.DnsAnswer.coerce(value)


def _zone_dict(outcome):
    value = {"zone": outcome.zone, "status": outcome.status, "address": outcome.address}
    if outcome.code:
        value["error"] = outcome.code
    if not outcome.queried:
        value["queried"] = False
    return value


def _zone_contracts(zones):
    """Normalize the policy's zone names into full zone contracts.

    ``make_policy`` stores zone *names*; the answer contract (which 127/8 range
    means "blocked", whether NXDOMAIN means "clear") is declared per zone in
    :class:`probes.DnsblZone`.  Callers that know a zone's codes pass
    :class:`probes.DnsblZone` objects straight through.
    """
    contracts = []
    for zone in zones:
        contracts.append(zone if isinstance(zone, probes.DnsblZone) else probes.generic_zone(zone))
    return tuple(contracts)


async def check_dnsbl(address, zones, timeout, resolver=None, *, max_queries=None):
    """Query every zone and report a distinct outcome per zone (defect 14).

    Each zone answers one of ``listed`` / ``clear`` / ``unknown``, and every
    ``unknown`` carries the code that explains it: ``DNSBL_ACCESS`` for a zone
    that refused the query, ``DNSBL_QUOTA`` for an exhausted resolver quota,
    ``DNSBL_ERROR`` for a broken answer, ``DNSBL_TIMEOUT`` for a resolver that
    did not answer, ``BUDGET_EXHAUSTED`` for a zone we never asked.  Only a
    zone that really answered can be ``listed`` or ``clear``; nothing is
    silently "clean".
    """
    ipaddress.ip_address(str(address).strip())

    async def resolve(query):
        if resolver is None:
            return _legacy_answer(await _lookup_dnsbl(query, timeout))
        value = resolver(query)
        if inspect.isawaitable(value):
            value = await value
        return _legacy_answer(value)

    try:
        report = await probes.check_dnsbl(address, _zone_contracts(zones), resolve=resolve,
                                           max_queries=max_queries)
    except ValueError:
        return [{"zone": zone if isinstance(zone, str) else zone.name, "status": "unknown",
                 "error": "INVALID_PROXY"} for zone in zones]
    return [_zone_dict(item) for item in report.zones]


async def screen_proxy(proxy, policy, denylist, resolver=None, *, max_queries=None):
    """Local rules first, then the zones, then one honest rolled-up status.

    ``clean`` is reachable only when *every* configured zone really answered
    ``clear``: one zone that said "access denied" or "quota exhausted" leaves
    the address ``unknown``, because the address was never actually looked up
    (defect 14).  The roll-up code names the first reason.
    """
    denylist = denylist or Denylist.empty()
    verdict = {
        # A clean verdict means an actual DNSBL check succeeded.  With DNSBL
        # disabled, the local denylist alone cannot prove a remote address is
        # clean, so the honest result is unknown.
        "status": "clean" if policy.get("dnsbl_enabled") else "unknown",
        "checked_at": time.time(),
        "policy_digest": policy.get("digest"),
        "local_rule": None,
        "dnsbl": [],
    }
    if policy.get("local_enabled", True):
        if denylist.error:
            verdict.update(status="unknown", error=denylist.error)
            return verdict
        verdict["local_rule"] = denylist.match(proxy)
        if verdict["local_rule"]:
            verdict["status"] = "local_denied"
            return verdict
    if not policy.get("dnsbl_enabled"):
        return verdict
    zones = policy.get("dnsbl_zones", [])
    if not zones:
        verdict.update(status="unknown", error="NO_DNSBL_ZONES")
        return verdict
    try:
        address = ipaddress.ip_address(urlsplit(proxy).hostname)
        verdict["dnsbl"] = await check_dnsbl(address, zones, float(policy.get("timeout", 2.5)),
                                             resolver=resolver, max_queries=max_queries)
    except (TypeError, ValueError):
        verdict.update(status="unknown", error="INVALID_PROXY")
        return verdict
    if not verdict["dnsbl"]:
        verdict.update(status="unknown", error="NO_DNSBL_ZONES")
        return verdict
    statuses = {item["status"] for item in verdict["dnsbl"]}
    if "listed" in statuses:
        verdict["status"] = "listed"
    elif "unknown" in statuses:
        verdict["status"] = "unknown"
        # The first reason, so the row says why "clean" was not granted.
        for item in verdict["dnsbl"]:
            if item["status"] == "unknown":
                verdict["error"] = item.get("error") or "DNSBL_ERROR"
                break
    return verdict


def verdict_blocks(verdict, strict=False):
    if verdict is None:
        return bool(strict)
    status = verdict.get("status")
    return status in {"listed", "local_denied"} or (strict and status == "unknown")


def result_allowed(row, min_success, denylist=None, strict=False, min_anonymity="any"):
    try:
        reliability = float(row.get("min_target_reliability", 0))
    except (TypeError, ValueError):
        return False
    if not reliability > 0 or reliability + 1e-12 < min_success:
        return False
    if denylist is not None and denylist.match(row.get("proxy", "")):
        return False
    if not anonymity_allows(row, min_anonymity):
        return False
    return not verdict_blocks(row.get("reputation"), strict=strict)
