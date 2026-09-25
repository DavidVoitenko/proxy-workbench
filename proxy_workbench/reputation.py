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
    address = ipaddress.ip_address(address)
    if address.version == 4:
        return ".".join(reversed(str(address).split("."))) + "."
    return "".join(reversed(address.exploded)) + "."


def _addresses(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        value = value.get("addresses", [])
    if not isinstance(value, (list, tuple, set)):
        return []
    result = []
    for item in value:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, (list, tuple)):
            if item and isinstance(item[0], str):
                result.append(item[0])
            elif len(item) > 1 and isinstance(item[-1], str):
                result.append(item[-1])
            elif len(item) > 1 and isinstance(item[-1], (list, tuple)) and item[-1] and isinstance(item[-1][0], str):
                result.append(item[-1][0])
    return result


async def _lookup_dnsbl(query, timeout):
    try:
        return await asyncio.wait_for(asyncio.to_thread(
            socket.getaddrinfo, query, None, type=socket.SOCK_STREAM), timeout)
    except socket.gaierror as exc:
        if exc.errno in (getattr(socket, 'EAI_NONAME', None), getattr(socket, 'EAI_NODATA', None)):
            return []
        raise OSError('DNS_ERROR') from None
    except (TimeoutError, asyncio.TimeoutError):
        raise TimeoutError('DNS_TIMEOUT') from None
    except OSError:
        raise OSError('DNS_ERROR') from None


async def check_dnsbl(address, zones, timeout, resolver=None):
    results = []
    address = ipaddress.ip_address(address)
    for zone in zones:
        query = reverse_ip(address) + zone
        try:
            if resolver is None:
                value = await _lookup_dnsbl(query, timeout)
            else:
                value = resolver(query)
                if inspect.isawaitable(value):
                    value = await value
            addresses = _addresses(value)
            listed = any(item.startswith("127.") for item in addresses)
            results.append({"zone": zone, "status": "listed" if listed else "clear",
                            "address": addresses[0] if listed and addresses else None})
        except TimeoutError:
            results.append({"zone": zone, "status": "unknown", "error": "DNS_TIMEOUT"})
        except (OSError, ValueError):
            results.append({"zone": zone, "status": "unknown", "error": "DNS_ERROR"})
        except Exception:
            results.append({"zone": zone, "status": "unknown", "error": "DNS_ERROR"})
    return results


async def screen_proxy(proxy, policy, denylist, resolver=None):
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
        verdict["dnsbl"] = await check_dnsbl(address, zones, float(policy.get("timeout", 2.5)), resolver=resolver)
    except (TypeError, ValueError):
        verdict.update(status="unknown", error="INVALID_PROXY")
        return verdict
    statuses = {item["status"] for item in verdict["dnsbl"]}
    if "listed" in statuses:
        verdict["status"] = "listed"
    elif "unknown" in statuses:
        verdict["status"] = "unknown"
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
