"""Pure, data-only source adapters.

No function in this module performs I/O, imports response data, or evaluates a
profile expression.  A profile chooses fixed field paths and a registered
parser; ``collect`` supplies the final normalize/policy/storage boundary.
"""
from __future__ import annotations

import base64
import csv
from html.parser import HTMLParser
import io
import json
import math
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ADAPTER_KINDS = ("line", "json-records", "fields", "page-json", "html-table")
DEFAULT_MAX_RECORDS = 500_000
DEFAULT_MAX_DEPTH = 64
DEFAULT_MAX_STRING = 1024 * 1024
DEFAULT_MAX_COLUMNS = 256
DEFAULT_MAX_NODES = 1_000_000
#: Nesting budget for the table subtree that is actually read.  A layout in
#: front of the table is covered by ``max_nodes`` instead.
MAX_HTML_DEPTH = 100
#: How many leading rows may carry the header before the table is refused.
MAX_HEADER_SCAN = 8
_VOID_TAGS = frozenset(("area", "base", "br", "col", "embed", "hr", "img", "input",
                        "link", "meta", "param", "source", "track", "wbr"))
PROTOCOLS = {"http", "https", "socks4", "socks5", "socks5h"}
_METADATA_KEYS = {
    "country", "country_code", "iso", "asn", "asn_org", "anonymity",
    "last_checked", "lastChecked", "last_check", "checked_at", "exit_ip",
    "supports_https", "https", "ssl", "city", "region", "isp", "latency",
    "latency_ms", "uptime", "response_time_ms", "speed", "org",
}


class AdapterError(ValueError):
    def __init__(self, code, *, partial=False, message=None):
        super().__init__(message or code)
        self.code = code
        self.partial = partial


def _limits(limits=None):
    value = limits or {}
    def integer(name, default, maximum):
        result = value.get(name, default)
        if isinstance(result, bool) or not isinstance(result, int) or result < 1 or result > maximum:
            raise AdapterError("SOURCE_LIMIT_INVALID")
        return result
    return {
        "max_bytes": integer("max_bytes", 32 * 1024 * 1024, 512 * 1024 * 1024),
        "max_records": integer("max_records", DEFAULT_MAX_RECORDS, 10_000_000),
        "max_depth": integer("max_depth", DEFAULT_MAX_DEPTH, 128),
        "max_string": integer("max_string", DEFAULT_MAX_STRING, 16 * 1024 * 1024),
        "max_columns": integer("max_columns", DEFAULT_MAX_COLUMNS, 1024),
        "max_nodes": integer("max_nodes", DEFAULT_MAX_NODES, 5_000_000),
    }


def _decode(body, limits):
    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise AdapterError("SOURCE_INVALID_UTF8")
    raw = bytes(body)
    if len(raw) > limits["max_bytes"]:
        raise AdapterError("SOURCE_TOO_LARGE")
    if b"\x00" in raw:
        raise AdapterError("SOURCE_NUL_BYTE")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AdapterError("SOURCE_INVALID_UTF8") from exc


def _check_shape(value, limits, depth=0):
    if depth > limits["max_depth"]:
        raise AdapterError("SOURCE_JSON_DEPTH")
    if isinstance(value, str) and len(value.encode("utf-8")) > limits["max_string"]:
        raise AdapterError("SOURCE_STRING_TOO_LARGE")
    if isinstance(value, dict):
        if len(value) > limits["max_records"]:
            raise AdapterError("SOURCE_RECORD_LIMIT")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > limits["max_string"]:
                raise AdapterError("SOURCE_JSON_SHAPE")
            _check_shape(item, limits, depth + 1)
    elif isinstance(value, list):
        if len(value) > limits["max_records"]:
            raise AdapterError("SOURCE_RECORD_LIMIT")
        for item in value:
            _check_shape(item, limits, depth + 1)


def _path(value, path):
    if path in (None, "", []):
        return value
    if isinstance(path, str):
        path = [part for part in path.split(".") if part]
    current = value
    for part in path:
        if isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return None
            current = current[index]
        else:
            return None
    return current


def _profile(profile):
    if isinstance(profile, str):
        return {"profile": profile, "config": {}}
    if not isinstance(profile, dict):
        raise AdapterError("SOURCE_PROFILE_INVALID")
    return {"kind": profile.get("kind"), "profile": str(profile.get("profile") or "generic-v1"),
            "config": profile.get("config") if isinstance(profile.get("config"), dict) else {}}


def _protocol(value, *, https_is_http=True, default=None):
    if value is None or (isinstance(value, str) and not value.strip()):
        return [default] if default else []
    if isinstance(value, (list, tuple, set)):
        raw_values = value
    else:
        raw_values = [value]
    result = []
    for raw in raw_values:
        if not isinstance(raw, str):
            continue
        item = raw.strip().lower()
        if item in ("connect80", "connect443", "https", "ssl", "tls", "http/https", "http,https"):
            item = "http" if https_is_http else "https"
        if item == "socks":
            item = "socks5"
        if item == "socks4a":
            item = "socks4"
        if item == "socks5h":
            item = "socks5"
        if item in ("http", "socks4", "socks5"):
            if item not in result:
                result.append(item)
    return result


def _has_credentials(value):
    if not isinstance(value, str):
        return False
    return "@" in value and "://" in value and bool(urlsplit(value).username)


def _address(record, *, host_fields=("host", "ip", "address"), port_fields=("port",), address_fields=("proxy", "url", "endpoint", "address")):
    if not isinstance(record, dict):
        return None, None
    for field in address_fields:
        value = record.get(field)
        if isinstance(value, str) and value.strip() and not _has_credentials(value):
            return value.strip(), "record"
    host = None
    for field in host_fields:
        value = record.get(field)
        if isinstance(value, (str, int)) and str(value).strip() and not _has_credentials(str(value)):
            host = str(value).strip()
            break
    if host is None:
        return None, None
    port = None
    for field in port_fields:
        value = record.get(field)
        if value is not None and str(value).strip():
            port = str(value).strip()
            break
    if port is None:
        return host, "record"
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    return f"{host}:{port}", "record"


def _metadata(record):
    if not isinstance(record, dict):
        return {}
    result = {}
    aliases = {
        "country_code": "country_code", "iso": "country", "countryCode": "country",
        "asnOrg": "asn_org", "asn_org": "asn_org", "org": "asn_org",
        "anonymityLevel": "anonymity", "anonymity": "anonymity",
        "lastChecked": "last_checked", "last_checked": "last_checked",
        "last_check": "last_checked", "checked_at": "last_checked",
        "latencyMs": "latency_ms", "latency_ms": "latency_ms",
        "response_time_ms": "response_time_ms", "response_ms": "latency_ms",
        "supportsHttps": "supports_https", "supports_https": "supports_https",
        "ssl": "supports_https", "https": "supports_https",
        "exit_ip": "exit_ip", "lastSeen": "last_checked",
    }
    for key in set(record) & (_METADATA_KEYS | set(aliases)):
        value = record.get(key)
        target = aliases.get(key, key)
        if key == "asn" and isinstance(value, dict):
            number = value.get("autonomous_system_number")
            organization = value.get("autonomous_system_organization")
            if number is not None:
                result["asn"] = number
            if organization:
                result["asn_org"] = str(organization)[:512]
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, str):
                value = value.strip()[:4096]
            if target == "asn" and isinstance(value, str):
                match = re.search(r"AS(\d+)", value, re.I)
                value = int(match.group(1)) if match else value[:128]
            result[target] = value
    geolocation = record.get("geolocation")
    if isinstance(geolocation, dict):
        country = geolocation.get("country")
        if isinstance(country, dict) and country.get("iso_code"):
            result.setdefault("country", str(country["iso_code"])[:8])
        elif isinstance(country, str):
            result.setdefault("country", country[:8])
    if "country" not in result:
        for key in ("country_code", "iso", "countryCode"):
            value = record.get(key)
            if isinstance(value, str) and value:
                result["country"] = value[:8]
                break
    return result


def normalize_timestamp(value):
    """Return UTC seconds for a provider timestamp, or None when unclear."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        number = float(raw)
        return number if math.isfinite(number) else None
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone
        text = raw.replace('Z', '+00:00')
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _json_records(body, profile, page_context, limits):
    config = _profile(profile)
    text = _decode(body, limits)
    if text.lstrip().startswith("<"):
        raise AdapterError("SOURCE_HTML_PLACEHOLDER")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise AdapterError("SOURCE_INVALID_JSON") from exc
    _check_shape(data, limits)
    name = config["profile"]
    cfg = config["config"]
    records_path = cfg.get("records_path")
    if records_path is None:
        records_path = "data" if isinstance(data, dict) and "data" in data else "proxies" if isinstance(data, dict) and "proxies" in data else None
    container = _path(data, records_path) if records_path is not None else data
    if container is None and isinstance(data, dict):
        # Protocol-keyed maps are an explicit envelope, not an arbitrary
        # object whose values happen to look like proxies.
        map_keys = [key for key in data if key.lower() in PROTOCOLS]
        if map_keys:
            container = []
            for key in map_keys:
                values = data.get(key)
                if not isinstance(values, list):
                    raise AdapterError("SOURCE_JSON_SHAPE")
                for value in values:
                    if isinstance(value, dict):
                        container.append(dict(value, protocol=key))
                    else:
                        container.append({"proxy": value, "protocol": key})
        elif cfg.get("map_url_keys") and all(isinstance(key, str) and "://" in key for key in data):
            container = []
            for key, value in data.items():
                if not isinstance(value, dict):
                    raise AdapterError("SOURCE_JSON_SHAPE")
                container.append(dict(value, proxy=key))
        else:
            raise AdapterError("SOURCE_JSON_SHAPE")
    if isinstance(container, dict):
        map_keys = [key for key in container if key.lower() in PROTOCOLS]
        if map_keys:
            flattened = []
            for key in map_keys:
                values = container.get(key)
                if not isinstance(values, list):
                    raise AdapterError("SOURCE_JSON_SHAPE")
                for value in values:
                    if isinstance(value, dict):
                        flattened.append(dict(value, protocol=key))
                    else:
                        flattened.append({"proxy": value, "protocol": key})
            container = flattened
        elif cfg.get("map_url_keys") and all(isinstance(key, str) and "://" in key for key in container):
            flattened = []
            for key, value in container.items():
                if not isinstance(value, dict):
                    raise AdapterError("SOURCE_JSON_SHAPE")
                flattened.append(dict(value, proxy=key))
            container = flattened
        else:
            raise AdapterError("SOURCE_JSON_SHAPE")
    if container is None:
        raise AdapterError("SOURCE_JSON_SHAPE")
    if not isinstance(container, list):
        raise AdapterError("SOURCE_JSON_SHAPE")
    if not container:
        return dict(state="empty", records=[], rejects={}, pages=1, metadata={})
    if len(container) > limits["max_records"]:
        raise AdapterError("SOURCE_RECORD_LIMIT")
    default = cfg.get("default_protocol")
    default_list = _protocol(default, default=default) if isinstance(default, str) else []
    default_protocol = default_list[0] if default_list else None
    records, rejects = [], {}
    def reject(reason):
        rejects[reason] = rejects.get(reason, 0) + 1
    for raw in container:
        if len(records) >= limits["max_records"]:
            raise AdapterError("SOURCE_RECORD_LIMIT")
        if isinstance(raw, str):
            if _has_credentials(raw):
                reject("credentials_present")
                continue
            values = [raw]
            declared = {}
            origin = "record"
        elif isinstance(raw, dict):
            if _has_credentials(raw.get("proxy")) or _has_credentials(raw.get("url")) or raw.get("username") or raw.get("password"):
                reject("credentials_present")
                continue
            value, origin = _address(raw, host_fields=tuple(cfg.get("host_fields", ("host", "ip", "address"))),
                                      port_fields=tuple(cfg.get("port_fields", ("port",))),
                                      address_fields=tuple(cfg.get("address_fields", ("proxy", "url", "endpoint", "address"))))
            if value is None:
                reject("invalid_address")
                continue
            values = [value]
            protocol_value = raw.get("protocols", raw.get("protocol", raw.get("type")))
            if protocol_value is None and raw.get("map_protocol"):
                protocol_value = raw.get("map_protocol")
            protocols = _protocol(protocol_value, default=default_protocol)
            if not protocols:
                # A map key or an explicit URL scheme is the only other
                # protocol origin; never guess from a filename.
                scheme = urlsplit(value).scheme if "://" in value else ""
                protocols = _protocol(scheme)
            if not protocols:
                reject("unsupported_protocol")
                continue
            declared = _metadata(raw)
            if len(protocols) > 8:
                reject("protocol_limit")
                continue
            records.append({"value": value, "values": [f"{protocol}://{value}" if "://" not in value else value for protocol in protocols],
                            "protocol_origin": "record", "declared": declared})
            continue
        else:
            reject("invalid_record")
            continue
        scheme = urlsplit(values[0]).scheme if "://" in values[0] else ""
        protocols = _protocol(scheme, default=default_protocol)
        if not protocols:
            reject("unsupported_protocol")
            continue
        records.append({"value": values[0], "values": [values[0] if "://" in values[0] else f"{protocol}://{values[0]}" for protocol in protocols],
                        "protocol_origin": "record", "declared": {}})
    if records and rejects:
        state = "partial"
    elif records:
        state = "complete"
    else:
        state = "empty" if not container else "invalid"
    return dict(state=state, records=records, rejects=rejects, pages=1, metadata={})


def _guess_delimiter(text, requested):
    if requested and requested != "auto":
        if requested not in (",", ";", "\t", "|"):
            raise AdapterError("SOURCE_DELIMITER_INVALID")
        return requested
    first = text.splitlines()[0] if text.splitlines() else ""
    choices = []
    for delimiter in (",", ";", "\t", "|"):
        count = first.count(delimiter)
        if count:
            choices.append((count, delimiter))
    if len(choices) != 1 or not choices[0][0]:
        raise AdapterError("SOURCE_DELIMITER_AMBIGUOUS")
    return choices[0][1]


def _fields(body, profile, page_context, limits):
    config = _profile(profile)
    cfg = config["config"]
    text = _decode(body, limits)
    if text.lstrip().startswith("<"):
        raise AdapterError("SOURCE_HTML_PLACEHOLDER")
    delimiter = _guess_delimiter(text, cfg.get("delimiter", "auto"))
    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        rows = list(reader)
    except (csv.Error, UnicodeError) as exc:
        raise AdapterError("SOURCE_CSV_INVALID") from exc
    if not rows:
        return dict(state="empty", records=[], rejects={}, pages=1, metadata={})
    if len(rows[0]) > limits["max_columns"]:
        raise AdapterError("SOURCE_COLUMN_LIMIT")
    header = [str(value).strip().lower() for value in rows[0]]
    header_mode = cfg.get("header", True)
    if header_mode:
        required = [str(value).lower() for value in cfg.get("required", ("ip", "port", "protocol"))]
        if any(value not in header for value in required) or len(set(header)) != len(header):
            raise AdapterError("SOURCE_HEADER_MISMATCH")
        data_rows = rows[1:]
    else:
        data_rows = rows
        header = [str(value).lower() for value in cfg.get("columns", ("ip", "port", "protocol"))]
    records, rejects = [], {}
    def reject(reason):
        rejects[reason] = rejects.get(reason, 0) + 1
    for row in data_rows:
        if len(records) >= limits["max_records"]:
            raise AdapterError("SOURCE_RECORD_LIMIT")
        if not row or not any(str(value).strip() for value in row):
            continue
        if len(row) > limits["max_columns"]:
            reject("column_limit")
            continue
        values = {}
        for key, value in zip(header, row):
            values[key] = value.strip()
        ip_key = cfg.get("ip_field", "ip")
        port_key = cfg.get("port_field", "port")
        address_key = cfg.get("address_field")
        if address_key and address_key in values:
            value = values[address_key]
        else:
            if ip_key not in values or port_key not in values:
                reject("field_missing")
                continue
            value = f"{values[ip_key]}:{values[port_key]}"
        if _has_credentials(value):
            reject("credentials_present")
            continue
        protocol = _protocol(values.get(cfg.get("protocol_field", "protocol")))
        if not protocol:
            reject("unsupported_protocol")
            continue
        declared = {}
        for key, target in (cfg.get("metadata") or {"country": "country", "anonymity": "anonymity", "last_checked": "last_checked"}).items():
            if key in values:
                declared[target] = values[key][:4096]
        values_for = [value if "://" in value else f"{item}://{value}" for item in protocol]
        records.append({"value": value, "values": values_for, "protocol_origin": "record", "declared": declared})
    state = "partial" if records and rejects else "complete" if records else "empty" if not data_rows else "invalid"
    return dict(state=state, records=records, rejects=rejects, pages=1, metadata={})


class _TableParser(HTMLParser):
    def __init__(self, profile, limits):
        super().__init__(convert_charrefs=True)
        self.profile = profile
        self.limits = limits
        self.target_id = profile.get("table_id")
        self.target_class = set(profile.get("table_classes", ()))
        self.target_index = profile.get("table_index") or 1
        self.row_fragment = bool(profile.get("row_fragment"))
        self.table_depth = 0
        # ``table_ordinal`` counts every <table> of the document, ``table_depth``
        # only the nesting: a layout table in front of the target shifts the
        # first number and not the second.
        self.table_ordinal = 0
        self.target_table_depth = 0
        # A fragment is a sequence of <tr> rows with no <table> of its own.
        self.in_target = self.row_fragment
        self.in_row = False
        self.in_cell = False
        self.cell = None
        self.row = []
        self.rows = []
        self.headers = []
        self.nodes = 0
        self.depth = 0
        self.header_fingerprint = None
        self.script_depth = 0

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > self.limits["max_nodes"]:
            raise AdapterError("SOURCE_HTML_NODE_LIMIT")
        attrs = dict(attrs)
        if tag == "script":
            self.script_depth += 1
            return
        if self.script_depth:
            return
        if tag == "table" and not self.row_fragment:
            self.table_depth += 1
            self.table_ordinal += 1
            classes = set(attrs.get("class", "").split())
            matches = (self.target_id and attrs.get("id") == self.target_id) or (
                not self.target_id and self.table_ordinal == self.target_index
                and (not self.target_class or classes & self.target_class))
            if not self.in_target and matches:
                self.in_target = True
                self.target_table_depth = self.table_depth
            if self.in_target:
                self.depth = 0
        if not self.in_target:
            return
        # The depth budget guards the subtree that is actually read.  The node
        # budget above still covers the whole document, so a deeply nested page
        # layout in front of the table cannot either exhaust memory or hide a
        # readable table.
        if tag not in _VOID_TAGS:
            self.depth += 1
            if self.depth > MAX_HTML_DEPTH:
                raise AdapterError("SOURCE_HTML_DEPTH")
        if tag == "tr":
            self.in_row = True
            self.row = []
        elif tag in ("td", "th") and self.in_row:
            self.in_cell = True
            self.cell = {"attrs": attrs, "text": []}

    def handle_endtag(self, tag):
        if tag == "script" and self.script_depth:
            self.script_depth -= 1
            return
        if self.script_depth:
            return
        if tag == "table" and not self.row_fragment:
            # table_depth covers the whole document, so a layout table in front
            # of the target shifts neither its position nor the moment the
            # parser is done with it.
            closed = self.in_target and self.table_depth <= self.target_table_depth
            self.table_depth = max(0, self.table_depth - 1)
            if closed:
                self.in_target = False
        if not self.in_target:
            return
        if tag not in _VOID_TAGS:
            self.depth = max(0, self.depth - 1)
        if tag in ("td", "th") and self.in_cell:
            self.row.append({"attrs": self.cell["attrs"], "text": "".join(self.cell["text"]).strip()})
            self.in_cell = False
            self.cell = None
        elif tag == "tr" and self.in_row:
            if self.row:
                self.rows.append(self.row)
            self.in_row = False

    def handle_data(self, data):
        if self.in_target and self.in_cell and not self.script_depth:
            if len(data) <= self.limits["max_string"]:
                self.cell["text"].append(data)

    def handle_entityref(self, name):
        self.handle_data("&" + name + ";")

    def handle_charref(self, name):
        self.handle_data("&#" + name + ";")


def _decode_cell(value, encoding):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if encoding == "base64":
        try:
            raw = base64.b64decode(value.encode("ascii"), validate=True)
        except (ValueError, UnicodeError) as exc:
            raise AdapterError("SOURCE_HTML_ATTRIBUTE_INVALID") from exc
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AdapterError("SOURCE_INVALID_UTF8") from exc
    return value


def _html_table(body, profile, page_context, limits):
    config = _profile(profile)
    cfg = config["config"]
    text = _decode(body, limits)
    fragment = cfg.get("json_html_field")
    if fragment:
        text = _json_fragment(text, fragment, limits)
    parser = _TableParser(cfg, limits)
    try:
        parser.feed(text)
        parser.close()
    except AdapterError:
        raise
    except (ValueError, UnicodeError) as exc:
        raise AdapterError("SOURCE_HTML_INVALID") from exc
    if not parser.in_target and not parser.rows:
        raise AdapterError("SOURCE_HTML_PLACEHOLDER")
    if not parser.rows:
        return dict(state="empty", records=[], rejects={}, pages=1, metadata={})
    rows = parser.rows
    plan = _column_plan(cfg, rows)
    header = cfg.get("header")
    header_prefix = cfg.get("header_prefix")
    if plan is not None:
        rows = rows[plan[0] + 1:]
    elif header:
        observed = [str(cell["text"]).strip().lower() for cell in rows[0]]
        if observed != [str(item).lower() for item in header]:
            raise AdapterError("SOURCE_HTML_SCHEMA_CHANGED")
        rows = rows[1:]
    elif header_prefix:
        observed = [str(cell["text"]).strip().lower() for cell in rows[0]]
        if observed[:len(header_prefix)] != [str(item).lower() for item in header_prefix]:
            raise AdapterError("SOURCE_HTML_SCHEMA_CHANGED")
        rows = rows[1:]
    elif rows and not any("data-ip" in cell["attrs"] for cell in rows[0]):
        rows = rows[1:]
    records, rejects = [], {}
    def reject(reason):
        rejects[reason] = rejects.get(reason, 0) + 1
    for row in rows:
        if len(records) >= limits["max_records"]:
            raise AdapterError("SOURCE_RECORD_LIMIT")
        if plan is not None:
            ip, port, protocol_text, country = _row_by_columns(row, plan[1])
        else:
            ip = port = None
            protocol_text = ""
            country = ""
            for cell in row:
                attrs = cell["attrs"]
                if "data-ip" in attrs:
                    ip = _decode_cell(attrs["data-ip"], cfg.get("address_encoding", "base64"))
                if "data-port" in attrs:
                    port = _decode_cell(attrs["data-port"], cfg.get("address_encoding", "base64"))
                href = attrs.get("href", "")
                if "type=" in href:
                    match = re.search(r"(?:[?&])type=([^&#]+)", href, re.I)
                    if match:
                        protocol_text = match.group(1)
                if not protocol_text and cell["text"]:
                    found = re.search(r"\b(?:HTTP|HTTPS|SOCKS4|SOCKS5)\b", cell["text"], re.I)
                    if found:
                        protocol_text = found.group(0)
                if not country:
                    country = cell["text"] if re.fullmatch(r"[A-Za-z]{2}", cell["text"].strip()) else ""
        if not ip or not port:
            reject("invalid_address")
            continue
        protocol = _protocol(protocol_text if isinstance(protocol_text, list)
                             else protocol_text or cfg.get("default_protocol", "http"))
        if not protocol:
            reject("unsupported_protocol")
            continue
        value = f"{ip}:{port}"
        records.append({"value": value, "values": [f"{item}://{value}" for item in protocol],
                        "protocol_origin": "record", "declared": {"country": country.upper()} if country else {}})
    state = "partial" if records and rejects else "complete" if records else "empty" if not rows else "invalid"
    return dict(state=state, records=records, rejects=rejects, pages=1, metadata={})


def _json_fragment(text, field, limits):
    """The HTML table a JSON envelope carries in one of its string fields.

    The body is decoded as data only: the field value is markup the same
    parser reads from a downloaded page, never anything that is executed.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise AdapterError("SOURCE_INVALID_JSON") from exc
    value = _path(data, str(field))
    if not isinstance(value, str) or not value.strip():
        raise AdapterError("SOURCE_JSON_SHAPE")
    _check_shape(data, limits)
    return value


def _column_plan(cfg, rows):
    """Resolve a declared column schema against the header row of the table.

    Returns ``(last_non_data_row, positions)`` where ``positions`` maps the
    logical names ``ip``/``port``/``protocol``/``country`` to cell positions.  A
    value is either a fixed position or the exact header label of the column,
    so a page that inserts a flag or a sort control does not shift the schema.
    ``header_skip`` names rows that carry no proxy at all (a filter form, a
    caption) in front of the column names.
    """
    columns = cfg.get("columns")
    if not isinstance(columns, dict) or "ip" not in columns or "port" not in columns:
        return None
    skip = cfg.get("header_skip", 0)
    if skip is None:
        skip = 0
    if isinstance(skip, bool) or not isinstance(skip, int) or skip < 0 or skip > MAX_HEADER_SCAN:
        raise AdapterError("SOURCE_HTML_SCHEMA_CHANGED")
    fixed = {key: value for key, value in columns.items() if isinstance(value, int) and not isinstance(value, bool)}
    if len(fixed) == len(columns):
        return (skip - 1, {key: max(0, int(value)) for key, value in fixed.items()})
    labels = {str(value).strip().lower() for value in columns.values() if isinstance(value, str)}
    for position, row in enumerate(rows[skip:skip + MAX_HEADER_SCAN], start=skip):
        header = [str(cell["text"]).strip().lower() for cell in row]
        if not labels <= set(header):
            continue
        positions = {}
        for key, value in columns.items():
            positions[key] = int(value) if isinstance(value, int) and not isinstance(value, bool) \
                else header.index(str(value).strip().lower())
        return (position, positions)
    raise AdapterError("SOURCE_HTML_SCHEMA_CHANGED")


def _row_by_columns(row, positions):
    def cell(key):
        index = positions.get(key)
        return row[index]["text"].strip() if index is not None and index < len(row) else ""
    country = cell("country")
    if not re.fullmatch(r"[A-Za-z]{2}", country):
        country = ""
    # A protocol cell may list several of them: "HTTPS SOCKS4 SOCKS5".
    protocol = re.split(r"[\s,]+", cell("protocol")) if positions.get("protocol") is not None else ""
    return cell("ip"), cell("port"), [item for item in protocol if item] if protocol else "", country


def parse_json_records(body, profile=None, page_context=None, limits=None):
    value = dict(profile) if isinstance(profile, dict) else {'profile': profile or 'generic-v1', 'config': {}}
    value['kind'] = 'json-records'
    return parse_page(body, value, page_context, limits)


def parse_fields(body, profile=None, page_context=None, limits=None):
    value = dict(profile) if isinstance(profile, dict) else {'profile': profile or 'generic-fields-v1', 'config': {}}
    value['kind'] = 'fields'
    return parse_page(body, value, page_context, limits)


def parse_page_json(body, profile=None, page_context=None, limits=None):
    value = dict(profile) if isinstance(profile, dict) else {'profile': profile or 'page-number-v1', 'config': {}}
    value['kind'] = 'page-json'
    return parse_page(body, value, page_context, limits)


def parse_html_table(body, profile=None, page_context=None, limits=None):
    value = dict(profile) if isinstance(profile, dict) else {'profile': profile or 'generic-html-v1', 'config': {}}
    value['kind'] = 'html-table'
    return parse_page(body, value, page_context, limits)


def parse_line(body, profile=None, page_context=None, limits=None):
    value = dict(profile) if isinstance(profile, dict) else {'profile': profile or 'line-v1', 'config': {}}
    value['kind'] = 'line'
    return parse_page(body, value, page_context, limits)


def _line(body, profile, page_context, limits):
    text = _decode(body, limits)
    config = _profile(profile)["config"]
    legacy_kind = config.get("legacy_kind", "")
    default = config.get("default_protocol", "http")
    first_token = config.get("line_address") == "first-token"
    records, rejects = [], {}
    for raw in text.splitlines():
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        if first_token:
            # "address<tab>free-form note" is the one shape a line list uses;
            # only the leading token is the address.
            value = value.split(None, 1)[0]
        if legacy_kind == "http-fields":
            match = re.fullmatch(r"(\d{1,3}(?:\.\d{1,3}){3}:\d{1,5}):[A-Za-z][A-Za-z .'-]*", value)
            if not match:
                rejects["invalid_address"] = rejects.get("invalid_address", 0) + 1
                continue
            value = match.group(1)
        if legacy_kind == "text":
            flat = re.sub(r"<[^>]*>", " ", value)
            addresses = re.findall(r"(?:(?:https?|socks[45]h?)://)?(\d{1,3}(?:\.\d{1,3}){3})(?::|\s+)(\d{2,5})(?!\d)", flat, re.I)
            if not addresses:
                continue
            for host, port in addresses:
                records.append({"value": f"{host}:{port}", "values": [f"http://{host}:{port}"],
                                "protocol_origin": "profile", "declared": {}})
            continue
        if _has_credentials(value):
            rejects["credentials_present"] = rejects.get("credentials_present", 0) + 1
            continue
        if "://" in value:
            try:
                protocols = _protocol(urlsplit(value).scheme)
            except ValueError:
                # A line is data, never a reason to abandon the whole source.
                rejects["invalid_address"] = rejects.get("invalid_address", 0) + 1
                continue
        elif legacy_kind == "auto":
            protocols = ["http", "socks4", "socks5"]
        else:
            protocols = _protocol(default, default=default)
        if not protocols:
            rejects["unsupported_protocol"] = rejects.get("unsupported_protocol", 0) + 1
            continue
        records.append({"value": value, "values": [value if "://" in value else f"{item}://{value}" for item in protocols],
                        "protocol_origin": "profile" if "://" not in value else "record", "declared": {}})
        if len(records) > limits["max_records"]:
            raise AdapterError("SOURCE_RECORD_LIMIT")
    return dict(state="partial" if records and rejects else "complete" if records else "empty",
                records=records, rejects=rejects, pages=1, metadata={})


PARSERS = {
    "line": _line,
    "json-records": _json_records,
    "fields": _fields,
    "html-table": _html_table,
    "page-json": _json_records,
}


def parse_page(body, profile, page_context=None, limits=None):
    """Parse one bounded page; all returned records are still unnormalized."""
    limits = _limits(limits)
    if isinstance(profile, str) and profile in PARSERS:
        profile = {'kind': profile, 'profile': profile, 'config': {}}
    profile = _profile(profile)
    kind = profile.get("kind")
    if kind is None:
        # A string profile names a parser only when explicitly wrapped by the
        # caller; bare legacy names remain the streaming parser's concern.
        raise AdapterError("SOURCE_ADAPTER_UNSUPPORTED")
    parser = PARSERS.get(kind)
    if parser is None:
        raise AdapterError("SOURCE_ADAPTER_UNSUPPORTED")
    try:
        result = parser(body, {**profile, "profile": profile.get("profile", "generic-v1"),
                               "config": profile.get("config", {})}, page_context or {}, limits)
        result.setdefault("next", None)
        result.setdefault("pages", 1)
        return result
    except AdapterError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise AdapterError("SOURCE_ADAPTER_ERROR", partial=True) from exc


def _page_profile(profile):
    if isinstance(profile, dict):
        return profile
    return {"profile": profile, "config": {}}


def page_info(data, profile):
    """Read fixed pagination paths without evaluating response expressions."""
    config = _page_profile(profile).get("config", {})
    def value(name, default=None):
        path = config.get(name + "_path", name)
        result = _path(data, path)
        return default if result is None else result
    records = value("records", [])
    if not isinstance(records, list):
        raise AdapterError("SOURCE_JSON_SHAPE")
    page = value("page", 1)
    total = value("total", None)
    has_more = value("has_more", None)
    next_value = value("next", None)
    try:
        page = int(page)
    except (TypeError, ValueError) as exc:
        raise AdapterError("SOURCE_PAGE_INVALID") from exc
    if total is not None:
        try:
            total = max(0, int(total))
        except (TypeError, ValueError) as exc:
            raise AdapterError("SOURCE_TOTAL_INVALID") from exc
    return {"records": records, "page": page, "total": total,
            "has_more": has_more, "next": next_value}


def next_page_url(base_url, page, profile, page_number):
    """Build a bounded next URL for page-number/offset pagination."""
    config = _page_profile(profile).get("config", {})
    mode = config.get("mode", "page-number")
    parsed = urlsplit(base_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if mode == "page-number":
        query[str(config.get("page_param", "page"))] = str(page_number)
        if config.get("limit_param"):
            query[str(config["limit_param"])] = str(config.get("limit_value", query.get(str(config["limit_param"]), "100")))
    elif mode == "offset":
        offset_param = str(config.get("offset_param", "offset"))
        limit_param = str(config.get("limit_param", "limit"))
        limit = int(query.get(limit_param, config.get("limit_value", 100)))
        query[offset_param] = str(int(query.get(offset_param, 0)) + limit)
        if config.get("limit_param"):
            query[limit_param] = str(limit)
    elif mode == "cursor":
        cursor = page.get("next") if isinstance(page, dict) else None
        if not cursor:
            return None
        query[str(config.get("cursor_param", "cursor"))] = str(cursor)
    elif mode == "next-url":
        next_value = page.get("next") if isinstance(page, dict) else None
        if not isinstance(next_value, str) or not next_value:
            return None
        # Only a path or same-host absolute URL is accepted; collect performs
        # the complete SSRF/redirect validation again.
        if "://" in next_value:
            next_parsed = urlsplit(next_value)
            if (next_parsed.scheme, next_parsed.hostname) != (parsed.scheme, parsed.hostname):
                raise AdapterError("SOURCE_PAGINATION_NEXT_INVALID")
        next_path = urlsplit(next_value).path or "/"
        prefix = config.get("path_prefix")
        if prefix and not next_path.startswith(str(prefix)):
            raise AdapterError("SOURCE_PAGINATION_NEXT_INVALID")
        return urlunsplit((parsed.scheme, parsed.netloc, next_value if next_value.startswith("/") else "/" + next_value, urlencode(query), ""))
    else:
        raise AdapterError("SOURCE_PAGINATION_MODE_INVALID")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
