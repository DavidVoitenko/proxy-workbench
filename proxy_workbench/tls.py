"""One verifying TLS context for every outgoing HTTPS connection.

``ssl.create_default_context()`` trusts whatever the interpreter's OpenSSL was
built to look at.  In a frozen build that is a path on the build machine
(``/opt/homebrew/etc/openssl@3``, a CI toolcache), which does not exist on the
user's computer, so every certificate failed and every HTTPS source looked
unreachable.  The certifi bundle ships with httpx and inside every build, so it
is always added; the system store is still loaded first, which keeps the
Windows store and corporate roots working.
"""
from __future__ import annotations

import ssl

import certifi

__all__ = ['default_context']


def default_context(cafile=None):
    """A verifying context: a given CA file replaces the defaults, never disables checks."""
    if cafile:
        context = ssl.create_default_context(cafile=cafile)
    else:
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=certifi.where())
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context
