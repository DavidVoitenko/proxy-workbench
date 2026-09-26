"""Data-only source catalog loading and selection migration.

The catalog is deliberately a leaf module: it does not fetch URLs, open a
database, or execute anything from a response.  ``proxytool.collect`` remains
the only network and persistence boundary.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

CATALOG_SCHEMA_VERSION = 1
CATALOG_ID = "proxy-workbench.sources"
DEFAULT_REVISION = 2026092502
DEFAULT_PUBLISHED_AT = "2026-09-26T09:12:00Z"
DEFAULT_MINIMUM_APP_VERSION = "2.3.0"
ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}\Z")
HEX_RE = re.compile(r"[0-9a-f]{16,64}\Z")
SUPPORTED_PAYLOAD_ROLES = {
    "proxy_list", "documentation", "commercial_page", "self_hosted",
    "subscription_config", "unknown",
}
LEGACY_KINDS = {
    "http", "https", "socks4", "socks5", "socks5h", "auto", "text",
    "geonode", "http-fields",
}
NEW_ADAPTERS = {"json-records", "fields", "page-json", "html-table"}
ADAPTERS = NEW_ADAPTERS | {"line", "geonode", "http-fields", "auto", "text", "unsupported"}
# Formats a person may pick for their own list.  ``line`` is the plain-text
# reader; the remaining names are the legacy kinds kept for old settings.
USER_SOURCE_FORMATS = ("http", "https", "socks4", "socks5", "socks5h", "auto", "text", "geonode",
                       "http-fields", "line", "json-records", "fields", "page-json", "html-table")
ACCESS_KINDS = {
    "public", "fully_public_free", "permanent_free_quota", "temporary_trial",
    "paid", "free_with_api_key", "unknown", "own_infrastructure",
    "snapshot_unavailable",
}
EVIDENCE_STATES = {
    "found_in_docs": {"supported", "not_found", "unknown"},
    "url_reachable": {"http_2xx_nonempty", "http_error", "not_run"},
    "format_confirmed": {"confirmed", "partial", "no_valid_records", "unsupported", "invalid", "not_run"},
    "data_looks_refreshable": {"validator_present", "body_change_observed", "unknown"},
    "proxy_liveness": {"not_run"},
}
DEFAULT_QUICK = ("cur-02", "cur-04", "cur-46", "cur-51", "cur-55",
                  "new-024", "new-009", "new-045")
DEFAULT_EXTENDED = ()
DEFAULT_SETS = {
    "quick": {
        "id": "quick", "name": "Базовый быстрый", "kind": "system",
        "members": list(DEFAULT_QUICK), "auto_add_new": False,
    },
    "extended": {
        "id": "extended", "name": "Расширенный", "kind": "system",
        "members": list(DEFAULT_EXTENDED), "auto_add_new": False,
    },
    "experimental": {
        "id": "experimental", "name": "Экспериментальные", "kind": "system",
        "members": [], "auto_add_new": False,
    },
    "protocol:http": {
        "id": "protocol:http", "name": "HTTP", "kind": "protocol",
        "members": [], "auto_add_new": False,
    },
    "protocol:https": {
        "id": "protocol:https", "name": "HTTPS", "kind": "protocol",
        "members": [], "auto_add_new": False,
    },
    "protocol:socks4": {
        "id": "protocol:socks4", "name": "SOCKS4", "kind": "protocol",
        "members": [], "auto_add_new": False,
    },
    "protocol:socks5": {
        "id": "protocol:socks5", "name": "SOCKS5", "kind": "protocol",
        "members": [], "auto_add_new": False,
    },
    "custom": {
        "id": "custom", "name": "Пользовательские", "kind": "local",
        "members": [], "auto_add_new": False,
    },
}
#: The one set no catalog may fill: its members are the user's own lists, and a
#: published catalog that declared them could not be validated (``_validate_set``
#: requires catalog ids) and would make the set filter and the "custom" state
#: filter answer different questions.
CUSTOM_SET_ID = "custom"


def set_members(definition, custom_ids=()):
    """Members of one set, with the user's own lists as the custom set.

    Every other set is a list of catalog ids and therefore a fact about the
    catalog; the custom set is a fact about the user, so it is resolved where
    the user's own sources are known.
    """
    if definition.get('id') == CUSTOM_SET_ID:
        return [value for value in custom_ids if value]
    return list(definition.get('members') or [])


_ROOT_KEYS = {
    "schema_version", "catalog_id", "revision", "published_at",
    "minimum_app_version", "generated_at_utc", "sources", "sets",
}
_SOURCE_KEYS = {
    # Research fields are retained in the bundled catalog.  Canonical fields
    # below are additive and make the runtime contract explicit.
    "id", "name", "publisher", "family_id", "category", "homepage",
    "documentation_url", "terms_url", "data_urls", "fallback_urls",
    "protocols", "formats", "adapter", "access", "access_note",
    "account_required", "quota_text", "update_claim", "license_status",
    "metadata_fields", "sets", "enabled_by_default", "priority",
    "priority_reason", "relation_to_current", "verification", "unknowns",
    "legacy_specs", "research_refs", "payload_role", "endpoints", "adapter_kind",
    "adapter_profile", "adapter_config", "access_kind", "publisher_id",
    "publisher_name", "rights", "evidence", "limits", "maturity", "catalog_state",
    "tags", "collection_allowed", "family", "protocol_hints", "checked_at",
    "rights_approved", "rights_status",
    "verification_level", "load_settings", "source_sets", "dataset_group",
}
_SET_KEYS = {"id", "name", "kind", "catalog_revision", "members", "auto_add_new"}


class CatalogError(ValueError):
    """Invalid or incompatible catalog data."""


def bundled_path():
    """Where the packaged catalog lives, under either of the two names.

    One convention, stated once: ``source-catalog.json`` is the catalog and
    ``sources.json`` is the flat list of 55 URLs the app read before the
    catalog existed.  The integration keeps both, so a package that still ships
    only ``sources.json`` from an older build -- where that name *was* the
    catalog -- is still read correctly.  The two meanings are never mixed: this
    function never returns the flat list under the catalog's name, and
    :func:`load_bundled` says so plainly when the file it found is the list.
    """
    catalog_path = Path(__file__).with_name("source-catalog.json")
    if catalog_path.is_file():
        return catalog_path
    return Path(__file__).with_name("sources.json")


def _text(value, name, *, maximum=100_000, allow_empty=True):
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value.strip()):
        raise CatalogError(f"{name}: ожидается строка")
    return value


def _url(value, name, *, allow_unsafe=False):
    value = _text(value, name, maximum=4096, allow_empty=False).strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
        hostname = parsed.hostname
    except (TypeError, ValueError) as exc:
        raise CatalogError(f"{name}: некорректный URL") from exc
    if (parsed.scheme not in ("http", "https") or not hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment or '\\' in value
            or any(char.isspace() or ord(char) < 0x20 for char in value)):
        raise CatalogError(f"{name}: нужен HTTP(S) URL без credentials и fragment")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
        if hostname.lower().rstrip(".") in {"localhost", "metadata.google.internal"} or hostname.lower().endswith((".local", ".internal", ".lan")):
            if not allow_unsafe:
                raise CatalogError(f"{name}: private/metadata destination запрещён")
    if address is not None and not address.is_global and not allow_unsafe:
        raise CatalogError(f"{name}: private/metadata destination запрещён")
    if port is not None and not 1 <= port <= 65535:
        raise CatalogError(f"{name}: некорректный порт")
    return value


def _slug(value, fallback):
    value = re.sub(r"[^a-z0-9._-]+", "-", str(value).lower()).strip("-._")
    return value or fallback


def _access(value, raw):
    if isinstance(value, dict):
        access = deepcopy(value)
    else:
        access = {"kind": value or raw.get("access_kind") or "unknown"}
    kind = access.get("kind", "unknown")
    if kind not in ACCESS_KINDS:
        raise CatalogError(f"access.kind: неизвестное значение {kind!r}")
    access["kind"] = kind
    access.setdefault("account_required", bool(raw.get("account_required", False)))
    access.setdefault("quota_text", raw.get("quota_text", "unknown"))
    return access


def _publisher(value, raw):
    if isinstance(value, dict):
        result = deepcopy(value)
    else:
        name = value or raw.get("publisher_name") or "unknown"
        result = {"id": raw.get("publisher_id") or _slug(name, "unknown"), "name": name}
    result.setdefault("id", _slug(result.get("name", "unknown"), "unknown"))
    result.setdefault("name", "unknown")
    if not isinstance(result["id"], str) or not isinstance(result["name"], str):
        raise CatalogError("publisher: ожидается объект с id/name")
    return result


def _adapter(value, raw):
    if isinstance(value, dict):
        result = deepcopy(value)
    else:
        kind = value or raw.get("adapter_kind")
        result = {"kind": "line" if kind in LEGACY_KINDS - {"geonode", "http-fields", "auto", "text"} else kind,
                  "profile": raw.get("adapter_profile") or _profile_for(kind, raw.get("id", "")),
                  "config": deepcopy(raw.get("adapter_config") or {})}
        if kind in LEGACY_KINDS:
            result["legacy_kind"] = kind
    if isinstance(value, dict) and set(value) - {"kind", "profile", "config", "legacy_kind"}:
        raise CatalogError("adapter: неизвестные поля")
    kind = result.get("kind")
    if kind in LEGACY_KINDS - {"geonode", "http-fields", "auto", "text"}:
        result["legacy_kind"] = kind
        kind = "line"
    if kind == "geonode":
        result["legacy_kind"] = "geonode"
        kind = "page-json"
        result.setdefault("profile", "geonode-v1")
    if kind in ("http-fields", "auto", "text"):
        result["legacy_kind"] = kind
        kind = "line"
    if kind is None:
        kind = "unsupported"
        result.setdefault("profile", "none")
    elif kind not in ADAPTERS:
        raise CatalogError(f"adapter.kind: неизвестное значение {kind!r}")
    result["kind"] = kind
    result.setdefault("profile", _profile_for(kind, raw.get("id", "")))
    result.setdefault("config", {})
    if result.get("legacy_kind") and isinstance(result["config"], dict):
        result["config"].setdefault("legacy_kind", result["legacy_kind"])
        if result["legacy_kind"] in ("http", "https", "socks4", "socks5", "socks5h"):
            result["config"].setdefault("default_protocol", "socks5" if result["legacy_kind"] == "socks5h" else result["legacy_kind"])
    if "config" in result and not isinstance(result["config"], dict):
        raise CatalogError("adapter.config: ожидается объект")
    # Kind defaults fill in whatever the record did not state, so a config that
    # only carries a legacy kind still gets its pagination and field mapping.
    defaults = _adapter_config(str(raw.get("id", "")), kind)
    if isinstance(defaults, dict) and defaults:
        merged = dict(defaults)
        merged.update(result["config"])
        result["config"] = merged
    if not isinstance(result["profile"], str) or not result["profile"]:
        raise CatalogError("adapter.profile: ожидается непустая строка")
    if not isinstance(result["config"], dict):
        raise CatalogError("adapter.config: ожидается объект")
    if set(result["config"]) & {"import", "eval", "exec", "javascript", "js", "headers", "module", "callback"}:
        raise CatalogError("adapter.config: исполняемые поля запрещены")
    return result


def _adapter_config(source_id, kind):
    if kind == "html-table" and source_id == "new-010":
        return {"table_id": "table_proxies", "address_encoding": "base64",
                "path_prefix": "/freeproxy", "header_prefix": ["#", "ip", "port"]}
    if kind == "json-records" and source_id in ("new-085",):
        return {"address_fields": ["value", "proxy", "address"], "default_protocol": "http", "map_url_keys": True}
    if kind == "json-records" and source_id in ("new-014",):
        return {"default_protocol": "http"}
    if kind == "json-records" and source_id in ("new-079",):
        return {"address_fields": ["address", "proxy", "url"], "default_protocol": "http"}
    if kind == "json-records" and source_id in ("new-080", "new-081"):
        return {"map_url_keys": True, "default_protocol": "http"}
    if kind == "page-json":
        return {"mode": "page-number", "page_param": "page", "records_path": "data",
                "page_path": "page", "total_path": "total", "has_more_path": "has_more"}
    if kind == "fields":
        return {"header": True, "delimiter": "auto", "required": ["ip", "port", "protocol"],
                "ip_field": "ip", "port_field": "port", "protocol_field": "protocol",
                "metadata": {"country": "country", "anonymity": "anonymity", "last_checked": "last_checked"}}
    return {}


def _profile_for(kind, source_id):
    profiles = {
        "new-008": "xyzs-v1", "new-009": "proxio-v1", "new-010": "advanced-name-v1",
        "new-014": "a2u-v1", "new-023": "ip-port-v1", "new-024": "litport-v1",
        "new-026": "free-public-2026-v1", "new-045": "databay-v1",
        "new-046": "proxy-free-v1", "new-048": "thordata-v1", "new-049": "webunblocker-v1",
        "new-055": "mauricegift-v1", "new-057": "proxynova-v1", "new-063": "roundproxies-v1",
        "new-074": "proxifly-v1", "new-075": "proxyscrape-v1", "new-076": "relayglass-v1",
        "new-077": "monosans-v1", "new-078": "zevtyardt-v1", "new-079": "dinoz0rg-v1",
        "new-080": "protocol-map-v1", "new-081": "jetkai-v1", "new-084": "webunblocker-v1",
        "new-085": "address-map-v1", "new-086": "generic-v1",
    }
    if kind in ("page-json", "geonode"):
        return "geonode-v1" if kind == "geonode" else "page-number-v1"
    if kind == "fields":
        return "generic-fields-v1"
    if kind == "html-table":
        return "generic-html-v1"
    if kind == "json-records":
        return profiles.get(source_id, "generic-v1")
    return {"http": "http-v1", "https": "https-v1", "socks4": "socks4-v1",
            "socks5": "socks5-v1", "socks5h": "socks5h-v1", "auto": "auto-v1",
            "text": "text-v1", "http-fields": "http-fields-v1"}.get(kind, "line-v1")


def _evidence(raw):
    verification = raw.get("verification") if isinstance(raw.get("verification"), dict) else {}
    reachable = "not_run"
    if verification.get("reachable") and verification.get("http_code") in range(200, 300):
        reachable = "http_2xx_nonempty"
    elif verification.get("http_code"):
        reachable = "http_error"
    checked = verification.get("checked_at_utc")
    if not checked:
        checked = raw.get("checked_at")
    return {
        "found_in_docs": {"state": "supported" if raw.get("documentation_url") else "unknown",
                           "checked_at": checked, "refs": [raw.get("id")] if raw.get("id") else []},
        "url_reachable": {"state": reachable, "checked_at": checked},
        "format_confirmed": {"state": "not_run", "checked_at": checked},
        "data_looks_refreshable": {"state": "validator_present" if verification.get("etag") or verification.get("last_modified") else "unknown",
                                   "checked_at": checked},
        "proxy_liveness": {"state": "not_run", "checked_at": None},
    }


def _legacy_specs(raw):
    supplied = raw.get("legacy_specs")
    # An explicitly supplied list is a statement about the user's old settings
    # and is honoured as given, including an empty one: a source that never
    # existed in the flat list has no legacy spec to migrate from.
    if isinstance(supplied, list):
        return [str(value) for value in supplied]
    adapter = raw.get("adapter")
    kind = adapter if isinstance(adapter, str) else (adapter or {}).get("legacy_kind")
    if kind not in LEGACY_KINDS:
        return []
    urls = raw.get("data_urls") or [url.get("url") for url in raw.get("endpoints", []) if isinstance(url, dict)]
    result = []
    for url in urls:
        if not isinstance(url, str) or not url:
            continue
        # The old file used bare URLs for ordinary HTTP/HTTPS and an explicit
        # prefix for the other built-in kinds.  Preserve the exact old shape.
        prefix = kind if kind not in ("http", "https") else ""
        result.append((prefix + " " + url).strip())
    return result


def _string_list(value, name):
    """A list of non-empty strings; a bare string is not silently a character list."""
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise CatalogError(f"{name}: ожидается список строк")
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def _evidence_field(raw, source_id):
    """The evidence mapping, or a stated error when the record brought junk."""
    value = raw.get("evidence")
    if value is None or value == {}:
        return _evidence(raw)
    if not isinstance(value, dict):
        raise CatalogError(f"{source_id}.evidence: ожидается объект")
    for key, item in value.items():
        if not isinstance(item, dict):
            raise CatalogError(f"{source_id}.evidence.{key}: ожидается объект")
        allowed = EVIDENCE_STATES.get(key)
        state = item.get("state")
        # A key this project does not define, or a state outside that key, is a
        # claim the application cannot interpret and must not store silently.
        if allowed is None:
            raise CatalogError(f"{source_id}.evidence: неизвестный признак {key!r}")
        if state not in allowed:
            raise CatalogError(f"{source_id}.evidence.{key}: недопустимое состояние {state!r}")
    return deepcopy(value)


def normalize_source(raw):
    if not isinstance(raw, dict):
        raise CatalogError("sources: запись должна быть объектом")
    unknown = set(raw) - _SOURCE_KEYS
    if unknown:
        raise CatalogError(f"source: неизвестные поля: {', '.join(sorted(unknown))}")
    source_id = _text(raw.get("id"), "source.id", maximum=64, allow_empty=False).strip()
    if not ID_RE.fullmatch(source_id):
        raise CatalogError(f"source.id: недопустимый ID {source_id!r}")
    name = _text(raw.get("name"), f"{source_id}.name", maximum=500, allow_empty=False).strip()
    publisher = _publisher(raw.get("publisher"), raw)
    family = raw.get("family_id") or raw.get("family") or source_id
    if not isinstance(family, str) or not family:
        raise CatalogError(f"{source_id}.family_id: ожидается строка")
    # A publisher family is a claim about who publishes; a dataset group is a
    # claim about *what bytes come back*.  Two different publishers can serve
    # one identical list, and a research pass that compared full snapshots can
    # show that.  Counting such a pair as two independent observations is the
    # error the overlap view exists to catch, so the catalog states it as data
    # and not only as prose in ``priority_reason``.
    dataset_group = raw.get("dataset_group") or family
    if not isinstance(dataset_group, str) or not dataset_group:
        raise CatalogError(f"{source_id}.dataset_group: ожидается строка")
    urls = raw.get("data_urls")
    if urls is None:
        urls = [item.get("url") for item in raw.get("endpoints", []) if isinstance(item, dict)]
    if not isinstance(urls, list) or not urls:
        raise CatalogError(f"{source_id}.data_urls: нужен список URL")
    endpoints = []
    for index, value in enumerate(urls):
        endpoints.append({"id": "primary" if index == 0 else f"fallback-{index}",
                          "url": _url(value, f"{source_id}.data_urls[{index}]", allow_unsafe=True),
                          "role": "primary" if index == 0 else "fallback", "relation": "own_feed"})
    fallbacks = raw.get("fallback_urls")
    if fallbacks is None:
        fallbacks = []
    if not isinstance(fallbacks, list):
        raise CatalogError(f"{source_id}.fallback_urls: ожидается список")
    for index, value in enumerate(fallbacks):
        endpoints.append({"id": f"fallback-{index}", "url": _url(value, f"{source_id}.fallback_urls[{index}]", allow_unsafe=True),
                          "role": "fallback", "relation": "own_feed"})
    protocols = raw.get("protocols") or raw.get("protocol_hints") or []
    if not isinstance(protocols, list) or any(not isinstance(value, str) for value in protocols):
        raise CatalogError(f"{source_id}.protocols: ожидается список строк")
    formats = raw.get("formats") or []
    if not isinstance(formats, list) or any(not isinstance(value, str) for value in formats):
        raise CatalogError(f"{source_id}.formats: ожидается список строк")
    adapter = _adapter(raw.get("adapter"), raw)
    access = _access(raw.get("access"), raw)
    rights = raw.get("rights") if isinstance(raw.get("rights"), dict) else {
        "terms_url": raw.get("terms_url", ""), "code_license": "unknown",
        "data_license": "unknown", "checked_at": raw.get("checked_at"),
        "attribution": "",
    }
    rights = deepcopy(rights)
    rights.setdefault("terms_url", raw.get("terms_url", ""))
    rights.setdefault("data_license", "unknown")
    rights.setdefault("checked_at", raw.get("checked_at"))
    rights.setdefault("code_license", "unknown")
    if rights.get("data_license") in ("restricted", "unresolved"):
        access["collection_allowed"] = False
    verification = raw.get("verification") if isinstance(raw.get("verification"), dict) else {}
    rights_approved = rights.get("data_license") not in (None, "", "unknown", "unresolved", "restricted")
    limits = raw.get("limits") or {}
    if not isinstance(limits, dict):
        raise CatalogError(f"{source_id}.limits: ожидается объект")
    limit_maxima = {"max_bytes": 512 * 1024 * 1024, "max_records": 10_000_000,
                    "max_pages": 5000, "max_depth": 128, "max_string": 16 * 1024 * 1024}
    for key, value in limits.items():
        if key not in limit_maxima or isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > limit_maxima[key]:
            raise CatalogError(f"{source_id}.limits: неизвестное или расширяющее значение {key}")
    unsafe = False
    for endpoint in endpoints:
        try:
            _url(endpoint["url"], "endpoint", allow_unsafe=False)
        except CatalogError:
            unsafe = True
            break
    collection_allowed = bool(raw.get("collection_allowed", not unsafe)) and not access.get("account_required", False) and access["kind"] not in {
        "paid", "temporary_trial", "free_with_api_key", "own_infrastructure", "snapshot_unavailable"}
    return {
        "id": source_id, "name": name, "publisher": publisher,
        "family_id": family, "dataset_group": dataset_group,
        "category": raw.get("category", "unknown"),
        "homepage": raw.get("homepage", ""), "documentation_url": raw.get("documentation_url", ""),
        "terms_url": raw.get("terms_url", ""), "data_urls": [item["url"] for item in endpoints[:len(urls)]],
        "fallback_urls": [item["url"] for item in endpoints[len(urls):]],
        "endpoints": endpoints, "protocols": list(dict.fromkeys(protocols)),
        "protocol_hints": list(dict.fromkeys(protocols)), "formats": list(dict.fromkeys(formats)),
        "adapter": adapter, "access": access, "access_note": raw.get("access_note", ""),
        "account_required": bool(raw.get("account_required", False)),
        "quota_text": raw.get("quota_text", "unknown"), "update_claim": raw.get("update_claim", ""),
        "license_status": raw.get("license_status", ""), "metadata_fields": list(raw.get("metadata_fields") or []),
        "sets": list(raw.get("sets") or raw.get("source_sets") or []),
        "enabled_by_default": bool(raw.get("enabled_by_default", False)),
        "load_settings": deepcopy(raw.get("load_settings") or {}),
        "priority": raw.get("priority", "normal"), "priority_reason": raw.get("priority_reason", ""),
        "relation_to_current": raw.get("relation_to_current", "new_source"),
        "research_refs": _string_list(raw.get("research_refs") or [source_id], f"{source_id}.research_refs"),
        "legacy_specs": _legacy_specs({**raw, "adapter": raw.get("adapter") or adapter}),
        "payload_role": raw.get("payload_role", "proxy_list" if adapter.get("kind") in ADAPTERS - {"unsupported"} else "unknown"),
        "rights": rights, "rights_approved": rights_approved,
        "rights_status": rights.get("data_license", "unknown"),
        "evidence": _evidence_field(raw, source_id),
        "limits": deepcopy(raw.get("limits") or {}), "maturity": raw.get("maturity", "unknown"),
        "catalog_state": raw.get("catalog_state", "listed"), "tags": _string_list(raw.get("tags"), f"{source_id}.tags"),
        "collection_allowed": collection_allowed, "checked_at": raw.get("checked_at") or verification.get("checked_at_utc"),
        "verification_level": raw.get("verification_level", "documented"),
    }


def _validate_set(raw, catalog_ids):
    if not isinstance(raw, dict):
        raise CatalogError("sets: набор должен быть объектом")
    unknown = set(raw) - _SET_KEYS
    if unknown:
        raise CatalogError(f"set: неизвестные поля: {', '.join(sorted(unknown))}")
    set_id = _text(raw.get("id"), "set.id", maximum=64, allow_empty=False)
    if not ID_RE.fullmatch(set_id.replace(":", "-")) and not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{2,63}", set_id):
        raise CatalogError(f"set.id: недопустимый ID {set_id!r}")
    members = raw.get("members")
    if not isinstance(members, list) or any(not isinstance(value, str) for value in members):
        raise CatalogError(f"set {set_id}: members должен быть списком строк")
    if len(set(members)) != len(members) or any(value not in catalog_ids for value in members):
        raise CatalogError(f"set {set_id}: неизвестный или повторяющийся member")
    if raw.get("auto_add_new", False) is not False:
        raise CatalogError(f"set {set_id}: auto_add_new должен быть false")
    return {"id": set_id, "name": _text(raw.get("name", set_id), f"set {set_id}.name"),
            "kind": _text(raw.get("kind", "system"), f"set {set_id}.kind"),
            "catalog_revision": raw.get("catalog_revision"), "members": list(members), "auto_add_new": False}


def validate_catalog(value, *, allow_research=False, allow_unsafe=False):
    if not isinstance(value, dict):
        raise CatalogError("catalog: ожидается объект")
    if value.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise CatalogError("catalog: неподдерживаемая schema_version")
    if not allow_research:
        unknown = set(value) - _ROOT_KEYS
        if unknown:
            raise CatalogError(f"catalog: неизвестные поля: {', '.join(sorted(unknown))}")
    raw_sources = value.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise CatalogError("catalog.sources: нужен непустой список")
    sources = []
    seen = set()
    for raw in raw_sources:
        source = normalize_source(raw)
        if source["id"] in seen:
            raise CatalogError(f"catalog: повторяющийся ID {source['id']}")
        seen.add(source["id"])
        for endpoint in source["endpoints"]:
            _url(endpoint["url"], f"{source['id']}.endpoint", allow_unsafe=allow_unsafe)
        for key, states in EVIDENCE_STATES.items():
            state = source["evidence"].get(key, {}).get("state")
            if state not in states:
                raise CatalogError(f"{source['id']}.evidence.{key}: недопустимое состояние")
        if source["evidence"]["proxy_liveness"]["state"] != "not_run":
            raise CatalogError(f"{source['id']}: proxy_liveness должен оставаться not_run")
        if source["payload_role"] not in SUPPORTED_PAYLOAD_ROLES:
            raise CatalogError(f"{source['id']}: недопустимый payload_role")
        sources.append(source)
    sets = value.get("sets")
    if sets is None:
        sets = []
    if not isinstance(sets, list):
        raise CatalogError("catalog.sets: ожидается список")
    normalized_sets = [_validate_set(item, seen) for item in sets]
    if not normalized_sets:
        normalized_sets = []
        for set_id, definition in DEFAULT_SETS.items():
            members = [item for item in definition["members"] if item in seen]
            if set_id.startswith("protocol:"):
                protocol = set_id.split(":", 1)[1]
                members = [source["id"] for source in sources if protocol in source["protocols"]]
            normalized_sets.append({**deepcopy(definition), "members": members,
                                    "catalog_revision": value.get("revision", DEFAULT_REVISION)})
    set_ids = [item["id"] for item in normalized_sets]
    if len(set_ids) != len(set_ids):
        raise CatalogError("catalog.sets: повторяющийся ID набора")
    result = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "catalog_id": value.get("catalog_id", CATALOG_ID),
        "revision": value.get("revision", DEFAULT_REVISION),
        "published_at": value.get("published_at", value.get("generated_at_utc", DEFAULT_PUBLISHED_AT)),
        "minimum_app_version": value.get("minimum_app_version", DEFAULT_MINIMUM_APP_VERSION),
        "sources": sources, "sets": normalized_sets,
    }
    if not isinstance(result["revision"], int) or result["revision"] < 1:
        raise CatalogError("catalog.revision: ожидается положительное целое")
    _text(result["catalog_id"], "catalog.catalog_id", maximum=100, allow_empty=False)
    _text(result["published_at"], "catalog.published_at", maximum=100, allow_empty=False)
    return result


def load_catalog(path=None, *, allow_research=True, allow_unsafe=True):
    path = Path(path) if path is not None else bundled_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError(f"Не удалось прочитать каталог: {path}") from exc
    if isinstance(value, list):
        # A flat URL list is a valid settings document, never a catalog.  A
        # package built before the two files were separated ships the list
        # under this name, and saying which file is wrong is the difference
        # between a fixable build problem and a dead Sources tab.
        raise CatalogError(
            f"{path.name} — это плоский список URL, а не каталог источников. "
            f"Каталог должен лежать рядом с ним под именем source-catalog.json.")
    return validate_catalog(value, allow_research=allow_research, allow_unsafe=allow_unsafe)


_BUNDLED = None


def load_bundled():
    """The packaged catalog, validated once per process.

    The file inside the package cannot change while the application runs, so
    the parsed result is shared.  Callers read it; nobody mutates it in place.
    """
    global _BUNDLED
    if _BUNDLED is None:
        _BUNDLED = load_catalog(allow_research=True, allow_unsafe=True)
    return _BUNDLED


def decode_catalog_bytes(body, current=None, *, max_bytes=8 * 1024 * 1024):
    """Validate a fetched catalog body without executing or merging it."""
    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise CatalogError("catalog: ожидаются байты")
    if len(body) > max_bytes:
        raise CatalogError("catalog: превышен лимит размера")
    try:
        value = json.loads(bytes(body).decode('utf-8-sig'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError("catalog: невалидный JSON") from exc
    return accept_catalog(current, value) if current is not None else validate_catalog(value, allow_research=False, allow_unsafe=False)


def catalog_digest(catalog):
    return hashlib.sha256(json.dumps(catalog, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def accept_catalog(current, incoming):
    """Validate and reject a revision downgrade without mutating ``current``."""
    candidate = validate_catalog(incoming, allow_research=False, allow_unsafe=False)
    if current and candidate["revision"] < current.get("revision", 0):
        raise CatalogError("catalog: revision ниже уже принятого")
    if current and candidate["revision"] == current.get("revision"):
        if catalog_digest(candidate) != catalog_digest(current):
            raise CatalogError("catalog: тот же revision с другим содержимым")
    return candidate


def _canonical_legacy(value):
    parts = str(value).strip().split(None, 1)
    if len(parts) == 2 and parts[0] in USER_SOURCE_FORMATS:
        return parts[0], parts[1].strip()
    return "http", str(value).strip()


_ALIASES_CACHE = {}


def legacy_aliases(catalog):
    """``{flat spec: catalog id}`` for every way the app used to name a source.

    The map is a full walk of the catalog, and both :func:`migrate_settings`
    and ``gui.defaults`` need it on every settings read, so it is built once
    per catalog object.  The cache holds a strong reference to the catalog it
    was built from, so an ``id`` can never be recycled onto a different
    document while its map is still live.
    """
    key = id(catalog)
    cached = _ALIASES_CACHE.get(key)
    if cached is not None and cached[0] is catalog:
        return cached[1]
    aliases = {}
    for source in catalog["sources"]:
        for spec in source.get("legacy_specs", []):
            aliases.setdefault(str(spec).strip(), source["id"])
        for endpoint in source.get("endpoints", []):
            url = endpoint["url"]
            for spec in (url, "http " + url, "https " + url, "socks4 " + url,
                         "socks5 " + url, "auto " + url, "text " + url):
                aliases.setdefault(spec, source["id"])
    if len(_ALIASES_CACHE) > 4:
        _ALIASES_CACHE.clear()
    _ALIASES_CACHE[key] = (catalog, aliases)
    return aliases


def custom_id(spec):
    kind, url = _canonical_legacy(spec)
    digest = hashlib.sha256((kind + "\0" + url).encode()).hexdigest()[:16]
    return "custom-" + digest


def _custom_record(source_id, spec, catalog=None):
    kind, url = _canonical_legacy(spec)
    adapter_kind = kind
    if kind == "geonode":
        adapter_kind = "page-json"
    elif kind in ("http-fields", "auto", "text"):
        adapter_kind = "line"
    elif kind in ("http", "https", "socks4", "socks5", "socks5h"):
        adapter_kind = "line"
    protocols = [kind] if kind in ("http", "https", "socks4", "socks5", "socks5h") else []
    return {"id": source_id, "name": source_id, "publisher": {"id": "local", "name": "Пользователь"},
            "family_id": source_id, "category": "custom", "data_urls": [url], "fallback_urls": [],
            "endpoints": [{"id": "primary", "url": url, "role": "primary", "relation": "custom"}],
            "protocols": list(protocols),
            "protocol_hints": list(protocols),
            "formats": [], "adapter": {"kind": adapter_kind, "profile": _profile_for(kind, source_id), "config": {"legacy_kind": kind}},
            "access": {"kind": "public", "account_required": False, "quota_text": "unknown"},
            "rights": {"terms_url": "", "data_license": "unknown", "checked_at": None},
            "evidence": {key: {"state": ("not_run" if key != "proxy_liveness" else "not_run"), "checked_at": None}
                          for key in EVIDENCE_STATES},
            "limits": {}, "maturity": "custom", "catalog_state": "custom", "collection_allowed": True,
            "legacy_specs": [str(spec).strip()], "research_refs": [], "tags": ["custom"], "enabled_by_default": False,
            "sets": ["custom"], "load_settings": {}}


def collectable_source(item):
    """Only a proxy-list record with a registered adapter may be downloaded.

    Tor, MTProto, provider dashboards, self-hosted recipes and archived pages
    stay visible in the catalog, but they must never reach ``collect()``: their
    payload is not a list of proxy addresses this application can read.
    """
    if not isinstance(item, dict) or not item.get("endpoints"):
        return False
    if item.get("payload_role") not in (None, "proxy_list"):
        return False
    if (item.get("adapter") or {}).get("kind") not in ADAPTERS - {"unsupported"}:
        return False
    return bool(item.get("collection_allowed", True))


def dataset_group_of(item):
    """The identity of the *bytes* a source is expected to serve.

    Defaults to the publisher family, so a catalog that says nothing extra
    behaves exactly as before.  A research pass that compared full snapshots
    can set ``dataset_group`` on two records whose payloads were byte-identical
    and whose publishers are different people; every "is this an independent
    observation?" question is then answered with the same key.
    """
    if not isinstance(item, dict):
        return ''
    return str(item.get("dataset_group") or item.get("family_id") or item.get("id") or '')


def dataset_groups(catalog, source_ids=None):
    """``{source_id: dataset group}`` for the ids a caller is looking at."""
    wanted = set(source_ids) if source_ids is not None else None
    result = {}
    for source in catalog.get('sources', []) if isinstance(catalog, dict) else []:
        source_id = source.get('id')
        if not source_id or (wanted is not None and source_id not in wanted):
            continue
        result[source_id] = dataset_group_of(source)
    return result


#: The five states F13 asks a reader to be able to tell apart.  They are
#: derived, never stored: two of them are about the payload, one is about
#: access, and one is about how much the record has been exercised.
SUPPORT_STATES = ("supported", "needs_adapter", "needs_auth", "unsupported", "experimental")
NEEDS_AUTH_ACCESS = {"paid", "temporary_trial", "free_with_api_key"}
#: Payloads that are not a list of addresses under any access terms at all.
#: A provider's marketing page is deliberately *not* here: whether that page
#: can be used is decided by its access terms, not by the shape of the page.
NEVER_A_LIST_ROLES = {"documentation", "self_hosted", "subscription_config", "snapshot_unavailable"}


def support_status(item):
    """The one-word answer to "can this application use this record?".

    The order matters.  A record that needs an account is reported as
    ``needs_auth`` even when its payload is a vendor page rather than a list,
    because the access terms are the blocker the reader has to clear first --
    that is the whole honest answer for a paid or trial provider this
    application has no account with.  ``experimental`` is not a weaker
    ``supported``: it says the record is readable and is simply not one this
    application is willing to switch on by default.
    """
    if not isinstance(item, dict):
        return 'unsupported'
    role = item.get("payload_role", "unknown")
    adapter_kind = (item.get("adapter") or {}).get("kind")
    if role in NEVER_A_LIST_ROLES:
        return 'unsupported'
    if (item.get("access") or {}).get("account_required") or item.get("account_required") \
            or (item.get("access") or {}).get("kind") in NEEDS_AUTH_ACCESS:
        return 'needs_auth'
    if role == "commercial_page":
        return 'unsupported'
    if role == "proxy_list" and adapter_kind in ADAPTERS - {"unsupported"}:
        # A readable record is still experimental when the catalog is not
        # willing to switch it on by default, and for an experimental record
        # whose own priority says so.
        if item.get("priority") == "experimental" or not item.get("collection_allowed", True):
            return 'experimental'
        return 'supported'
    if role == "proxy_list":
        return 'needs_adapter'
    return 'unsupported'


def custom_source(url, kind="http", *, allow_unsafe=False):
    """Validated descriptor for a user-supplied URL and a supported format."""
    url = _url(url, "custom.url", allow_unsafe=allow_unsafe)
    if not isinstance(kind, str) or kind not in USER_SOURCE_FORMATS:
        raise CatalogError(f"Неподдерживаемый формат источника: {kind!r}")
    spec = (url if kind in ("http", "https") else f"{kind} {url}")
    source_id = custom_id(spec)
    adapter = _adapter({"kind": kind}, {"id": source_id})
    record = _custom_record(source_id, spec)
    record["adapter"] = adapter
    record["data_urls"] = [url]
    record["endpoints"] = [{"id": "primary", "url": url, "role": "primary", "relation": "custom"}]
    return {"id": source_id, "spec": spec, "url": url, "adapter": adapter,
            "name": record["name"], "kind": kind, "record": record}


def eligible_sources(catalog, *, require_rights=False):
    """Public proxy-list records allowed by the collection gate."""
    result = []
    for source in catalog.get('sources', []):
        if not collectable_source(source):
            continue
        if require_rights and not source.get('rights_approved', False):
            continue
        result.append(source)
    return result


def source_by_id(catalog, source_id, custom_specs=None):
    for source in catalog["sources"]:
        if source["id"] == source_id:
            return source
    for item in custom_specs or []:
        if item.get("id") == source_id:
            return item
    return None


def _materialization_spec(item):
    legacy = item.get("legacy_specs") or []
    if legacy:
        return list(legacy)
    endpoints = item.get("endpoints") or []
    if not endpoints:
        return []
    kind = (item.get("adapter") or {}).get("kind")
    if kind in LEGACY_KINDS - {'line', 'unsupported'}:
        prefix = kind if kind not in ('http', 'https') else ''
        return [(prefix + ' ' + endpoints[0]['url']).strip()]
    if kind in NEW_ADAPTERS:
        return [kind + ' ' + endpoints[0]['url']]
    return [endpoints[0]['url']]


def _plan_with_spec(source_id, item, spec):
    """The catalog source read with the exact format the user entered.

    A bare URL in the old text area meant "an HTTP list" before the catalog
    existed.  The catalog's own ``legacy_specs`` must not silently re-type that
    choice, so the recorded spec wins and the rest of the record is kept.
    ``None`` means the spec is not a format the app can read; the caller then
    falls back to the catalog's own declaration.
    """
    kind, url = _canonical_legacy(spec)
    if kind not in USER_SOURCE_FORMATS:
        return None
    if not any(endpoint.get("url") == url for endpoint in item.get("endpoints") or []):
        return None
    try:
        adapter = _adapter({"kind": kind}, {"id": source_id})
    except CatalogError:
        return None
    record = deepcopy(item)
    record["adapter"] = adapter
    return record


def _chosen_specs(value):
    """Validated ``{source id: legacy spec}`` map from a stored selection."""
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items()
            if isinstance(key, str) and isinstance(item, str) and item.strip()}


#: Public name: ``gui.validate`` filters a hand-edited settings file with it.
chosen_specs = _chosen_specs



def migrate_settings(settings, catalog=None, *, data_dir=None):
    """Return settings v3 while retaining every exact legacy URL and disable.

    ``catalog`` is the catalog the caller validated against.  Falling back to
    the bundled file is only right for a caller that has no other one: a
    selection updated against an accepted catalog would otherwise lose every
    source that only that catalog lists, and ``sources`` is the one list the
    collector reads.
    """
    if catalog is None:
        catalog = load_bundled()
    if not isinstance(settings, dict):
        raise ValueError("Ожидаются настройки проверки.")
    result = deepcopy(settings)
    selection = result.get("source_selection")
    if not isinstance(selection, dict):
        old = result.get("sources", [])
        if isinstance(old, dict) and isinstance(old.get("sources"), list):
            old = old["sources"]
        if not isinstance(old, list):
            raise ValueError("Источники должны быть списком URL (до 5000).")
        aliases = legacy_aliases(catalog)
        selected, custom, chosen = [], [], {}
        for spec in old:
            if not isinstance(spec, str):
                raise ValueError("Источник должен быть строкой URL или формата.")
            source_id = aliases.get(spec.strip())
            if source_id is None:
                kind, url = _canonical_legacy(spec)
                matches = [item["id"] for item in catalog["sources"]
                           if any(endpoint["url"] == url for endpoint in item["endpoints"])]
                source_id = matches[0] if len(matches) == 1 else custom_id(spec)
                if source_id.startswith("custom-") and not any(item["id"] == source_id for item in custom):
                    custom.append(_custom_record(source_id, spec))
            if source_id not in selected:
                selected.append(source_id)
            chosen[source_id] = spec.strip()
        # Old versions let a source sit in the list and stay paused, and they
        # wrote that pause as a URL or as an id.  Both are resolved through the
        # same alias table as the list itself, and the entry is kept even when
        # the source is also selected: ``selection_ids`` is what turns the
        # pair back into an active set, so dropping the pause here would
        # silently re-enable a source the user had deliberately turned off.
        disabled = []
        for value in result.get("download_disabled_ids", []):
            if not isinstance(value, str) or not value.strip():
                continue
            text = value.strip()
            source_id = aliases.get(text) or text
            if source_id not in selected:
                kind, url = _canonical_legacy(text)
                matches = [item["id"] for item in catalog["sources"]
                           if any(endpoint["url"] == url for endpoint in item["endpoints"])]
                if len(matches) == 1:
                    source_id = matches[0]
            if source_id not in disabled:
                disabled.append(source_id)
        # Existing versions used a list as the authoritative selection.  A
        # disabled ID is retained in the migration metadata but not active.
        selection = {"schema_version": 1, "catalog_revision": catalog["revision"],
                     "selected_ids": selected, "download_disabled_ids": disabled,
                     "sets": [{"id": "legacy-import", "members": selected}],
                     "specs": chosen, "custom_sources": custom}
    else:
        selection = deepcopy(selection)
        selected = selection.get("selected_ids", [])
        disabled = selection.get("download_disabled_ids", [])
        custom = selection.get("custom_sources", [])
        if not isinstance(selected, list) or not isinstance(disabled, list) or not isinstance(custom, list):
            raise ValueError("source_selection: повреждённые поля.")
        if any(not isinstance(value, str) for value in selected + disabled):
            raise ValueError("source_selection: IDs должны быть строками.")
        selected = list(dict.fromkeys(selected))
        disabled = list(dict.fromkeys(value for value in disabled if value not in selected))
    result["settings_version"] = 3
    result["source_selection"] = {
        "schema_version": 1, "catalog_revision": selection.get("catalog_revision", catalog["revision"]),
        "selected_ids": list(dict.fromkeys(selection.get("selected_ids", selected))),
        "download_disabled_ids": list(dict.fromkeys(selection.get("download_disabled_ids", disabled))),
        "sets": list(selection.get("sets") or []),
        "specs": _chosen_specs(selection.get("specs")),
        "custom_sources": [normalize_custom(item) for item in selection.get("custom_sources", custom)],
    }
    records = list(result["source_selection"]["custom_sources"])
    # Membership is tracked by id: comparing whole records made this quadratic in
    # the size of the selection, and the selection is what every click touches.
    seen = {entry.get("id") for entry in records if isinstance(entry, dict)}
    for source_id in result["source_selection"]["selected_ids"] + result["source_selection"]["download_disabled_ids"]:
        item = source_by_id(catalog, source_id, records)
        if item is not None and item.get("id") not in seen:
            seen.add(item.get("id"))
            records.append(item)
    specs = result["source_selection"]["specs"]
    materialized = []
    for source_id in result["source_selection"]["selected_ids"]:
        item = source_by_id(catalog, source_id, records)
        if item is None:
            continue
        if source_id in result["source_selection"]["download_disabled_ids"]:
            continue
        if not item.get("endpoints"):
            stored = next((entry for entry in result["source_selection"]["custom_sources"]
                           if entry.get("id") == source_id), None)
            item = _custom_plan(source_id, stored or item, records)
        if item is None or not collectable_source(item):
            continue
        # What the user typed outranks what the catalog declares about the URL.
        if source_id in specs and _plan_with_spec(source_id, item, specs[source_id]) is not None:
            materialized.append(specs[source_id].strip())
            continue
        materialized.extend(_materialization_spec(item))
    result["sources"] = list(dict.fromkeys(materialized))
    return result


def custom_spec(url, adapter):
    """Canonical ``kind URL`` string for a custom source, derived from the adapter.

    The adapter choice is the only authority: an old record without a stored
    spec still materializes as the exact legacy string it was saved with.
    """
    config = (adapter or {}).get("config") if isinstance(adapter, dict) else None
    legacy = (config or {}).get("legacy_kind")
    kind = (adapter or {}).get("kind")
    if legacy in LEGACY_KINDS:
        prefix = "" if legacy in ("http", "https") else f"{legacy} "
    elif kind in NEW_ADAPTERS or kind == "line":
        prefix = f"{kind} "
    else:
        prefix = ""
    return (prefix + url).strip()


def normalize_custom(item):
    if not isinstance(item, dict):
        raise ValueError("custom_sources: запись должна быть объектом.")
    source_id = item.get("id")
    url = item.get("url") or (item.get("endpoints") or [{}])[0].get("url")
    if not isinstance(source_id, str) or not ID_RE.fullmatch(source_id) or not isinstance(url, str):
        raise ValueError("custom_sources: нужен id и URL.")
    _url(url, "custom_sources.url", allow_unsafe=True)
    adapter = item.get("adapter") or {"kind": "line", "profile": "custom-v1", "config": {}}
    if not isinstance(adapter, dict) or adapter.get("kind") not in ADAPTERS:
        raise ValueError("custom_sources.adapter: неизвестный адаптер.")
    # A name the user typed is kept: without it a custom entry reverts to its id
    # on the next migration, which is exactly what the user typed to avoid.
    name = item.get("name")
    record = {"id": source_id, "url": url, "adapter": deepcopy(adapter), "spec": custom_spec(url, adapter)}
    if isinstance(name, str) and name.strip():
        record["name"] = name.strip()[:160]
    return record


def _custom_plan(source_id, item, custom_sources):
    """Rebuild a collectable plan for a stored custom entry."""
    adapter = (item or {}).get("adapter") or {}
    spec = (item or {}).get("spec") or custom_spec((item or {}).get("url", ""), adapter)
    if not spec or not (item or {}).get("url"):
        return None
    record = _custom_record(source_id, spec)
    if item.get('name'):
        record['name'] = item['name']
    record["adapter"] = deepcopy(adapter)
    record["data_urls"] = [item["url"]]
    record["endpoints"] = [{"id": "primary", "url": item["url"], "role": "primary", "relation": "custom"}]
    return record


def selection_ids(settings):
    selection = settings.get("source_selection") if isinstance(settings, dict) else None
    if not isinstance(selection, dict):
        return []
    disabled = set(selection.get("download_disabled_ids", []))
    return [value for value in selection.get("selected_ids", []) if value not in disabled]


def materialize_selection(settings, catalog=None):
    if catalog is None:
        catalog = load_bundled()
    selection = settings.get('source_selection', {}) if isinstance(settings, dict) else {}
    custom = [normalize_custom(item) for item in selection.get('custom_sources', [])]
    specs = _chosen_specs(selection.get('specs'))
    result = []
    for source_id in selection_ids(settings):
        item = source_by_id(catalog, source_id, custom)
        if item is not None and not item.get('endpoints'):
            item = _custom_plan(source_id, item, custom)
        if item is not None and source_id in specs:
            item = _plan_with_spec(source_id, item, specs[source_id]) or item
        if item is not None and collectable_source(item):
            result.append(item)
    return result


def resolve_specs(specs, catalog=None, *, include_disabled=True):
    """Resolve legacy strings or stable IDs to concrete source plans."""
    if catalog is None:
        catalog = load_bundled()
    aliases = legacy_aliases(catalog)
    custom_specs = []
    result = []
    for value in specs or []:
        if isinstance(value, dict):
            if value.get("id") and value.get("endpoints"):
                result.append(value)
            elif value.get("id"):
                item = source_by_id(catalog, value["id"], custom_specs)
                if item is None:
                    raise ValueError(f"Неизвестный источник: {value['id']}")
                result.append(item)
            else:
                raise ValueError("Некорректный источник.")
            continue
        if not isinstance(value, str):
            raise ValueError("Источник должен быть строкой URL или формата.")
        value = value.strip()
        item = source_by_id(catalog, value, custom_specs)
        source_id = aliases.get(value)
        if item is not None:
            pass
        elif source_id:
            item = source_by_id(catalog, source_id)
            item = _plan_with_spec(source_id, item, value) or item
        else:
            item = _custom_record(custom_id(value), value)
            custom_specs.append(item)
        result.append(item)
    if not include_disabled:
        result = [item for item in result if not item.get("download_disabled")]
    return result


def public_record(source):
    """Redact endpoint paths/queries while retaining host and protocol."""
    result = deepcopy(source)
    def redact(value):
        try:
            parsed = urlsplit(value)
            host = parsed.hostname or ""
            if ":" in host and not host.startswith("["):
                host = "[" + host + "]"
            port = f":{parsed.port}" if parsed.port else ""
            return f"{parsed.scheme}://{host}{port}/"
        except (TypeError, ValueError):
            return ""
    for endpoint in result.get("endpoints", []):
        endpoint["url"] = redact(endpoint.get("url", ""))
    result["data_urls"] = [redact(value) for value in result.get("data_urls", [])]
    result["fallback_urls"] = [redact(value) for value in result.get("fallback_urls", [])]
    result.pop("limits", None)
    return result


def apply_set(settings, set_id, catalog=None, *, keep_disabled=True):
    """Apply a materialized set without auto-adding future catalog members.

    Catalog-managed active IDs are replaced by the set's explicit member list.
    Local custom IDs remain selected, and an explicit disable stays disabled
    unless ``keep_disabled`` is false.  This is deliberately a snapshot, not a
    live query over catalog membership.
    """
    if catalog is None:
        catalog = load_bundled()
    definition = next((item for item in catalog['sets'] if item['id'] == set_id), None)
    if definition is None:
        raise ValueError(f'Неизвестный набор источников: {set_id}')
    result = deepcopy(settings)
    selection = deepcopy(result.get('source_selection') or {})
    custom_ids = [item.get('id') for item in selection.get('custom_sources', []) if isinstance(item, dict)]
    disabled = set(selection.get('download_disabled_ids', []))
    defined = set_members(definition, custom_ids)
    members = [value for value in defined if value not in disabled or not keep_disabled]
    selection['selected_ids'] = list(dict.fromkeys(members + custom_ids))
    selection['download_disabled_ids'] = sorted(value for value in disabled if keep_disabled or value not in members)
    selection['sets'] = list(selection.get('sets') or []) + [{'id': set_id, 'members': list(defined)}]
    selection['catalog_revision'] = catalog['revision']
    result['source_selection'] = selection
    result['settings_version'] = 3
    keep = set(selection['selected_ids'])
    selection['specs'] = {key: value for key, value in _chosen_specs(selection.get('specs')).items() if key in keep}
    return migrate_settings({**result, 'source_selection': selection}, catalog)


def remove_ids(settings, source_ids, catalog=None):
    """Remove IDs from the active selection while retaining set history."""
    result = deepcopy(settings)
    if not isinstance(result.get('source_selection'), dict):
        result = migrate_settings(result, catalog)
    selection = deepcopy(result.get('source_selection') or {})
    remove = set(source_ids or [])
    selection['selected_ids'] = [value for value in selection.get('selected_ids', []) if value not in remove]
    selection['download_disabled_ids'] = [value for value in selection.get('download_disabled_ids', []) if value not in remove]
    # A removed source takes its recorded format with it: re-adding it later is
    # a fresh choice and falls back to the catalog's own declaration.
    selection['specs'] = {key: value for key, value in _chosen_specs(selection.get('specs')).items()
                          if key not in remove}
    result['source_selection'] = selection
    return migrate_settings(result, catalog)


def set_disabled(settings, source_ids, disabled=True, catalog=None):
    result = deepcopy(settings)
    if not isinstance(result.get('source_selection'), dict):
        result = migrate_settings(result, catalog)
    selection = deepcopy(result.get('source_selection') or {})
    ids = set(source_ids or [])
    current = set(selection.get('download_disabled_ids', []))
    current.update(ids) if disabled else current.difference_update(ids)
    selection['download_disabled_ids'] = sorted(current)
    result['source_selection'] = selection
    return migrate_settings(result, catalog)


def source_specs_for_ids(source_ids, catalog=None, custom_sources=None):
    if catalog is None:
        catalog = load_bundled()
    custom_sources = custom_sources or []
    return [source_by_id(catalog, source_id, custom_sources) for source_id in source_ids]


COMPARED_FIELDS = ("name", "category", "protocols", "formats", "data_urls", "access",
                   "adapter", "evidence", "rights", "collection_allowed", "dataset_group")


def source_fingerprint(source):
    return {key: source.get(key) for key in COMPARED_FIELDS}


def catalog_diff(current, incoming):
    """Describe a newer catalog without changing the user's selection.

    Returns added / changed / retired stable IDs.  Every entry is only ever a
    description: nothing here selects a source or rewrites a stored set.
    """
    current = current or {"sources": []}
    incoming = incoming or {"sources": []}
    old = {item["id"]: source_fingerprint(item) for item in current.get("sources", [])}
    new = {item["id"]: source_fingerprint(item) for item in incoming.get("sources", [])}
    added = sorted(set(new) - set(old))
    retired = sorted(set(old) - set(new))
    changed = sorted(item_id for item_id in set(old) & set(new) if old[item_id] != new[item_id])
    return {
        "revision": incoming.get("revision"),
        "current_revision": current.get("revision"),
        "added": added, "changed": changed, "retired": retired,
        "unchanged": len(set(old) & set(new)) - len(changed),
    }
