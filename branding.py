"""Neutral product identity and request profiles.

The profiles describe HTTP request metadata only.  They do not change TLS,
HTTP protocol, or browser behaviour and must not be used as an anonymity
claim.
"""
from __future__ import annotations

import hashlib
import json

PRODUCT_NAME = "Proxy Workbench"
PRODUCT_ID = "ProxyWorkbench"
PRODUCT_VERSION = "1.1.0"
DEFAULT_REQUEST_PROFILE = "workbench"

REQUEST_PROFILES = {
    "workbench": {
        "User-Agent": f"{PRODUCT_ID}/{PRODUCT_VERSION}",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    },
    "standard": {
        "User-Agent": f"{PRODUCT_ID}/{PRODUCT_VERSION}",
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "identity",
    },
    "minimal": {
        "User-Agent": f"{PRODUCT_ID}/{PRODUCT_VERSION}",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    },
}

REQUEST_PROFILE_LABELS = {
    "workbench": "Рабочий профиль",
    "standard": "Стандартный",
    "minimal": "Минимальный",
}


def validate_profile(profile):
    if profile not in REQUEST_PROFILES:
        raise ValueError("Неизвестный request-профиль.")
    return profile


def profile_digest(profile):
    validate_profile(profile)
    encoded = json.dumps(REQUEST_PROFILES[profile], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:20]


def merge_headers(profile, overrides=None):
    """Return a fresh case-insensitive header merge for one request."""
    validate_profile(profile)
    headers = dict(REQUEST_PROFILES[profile])
    for name, value in (overrides or {}).items():
        for existing in tuple(headers):
            if existing.lower() == name.lower():
                del headers[existing]
        headers[name] = value
    return headers


def profile_metadata(profile):
    validate_profile(profile)
    return {"id": profile, "label": REQUEST_PROFILE_LABELS[profile]}
