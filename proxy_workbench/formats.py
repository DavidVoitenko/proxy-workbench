"""Ready-to-use configuration files built from exported proxies."""
from __future__ import annotations

import json

# Browsers read at most a handful of fallbacks; a longer PAC list only slows failures down.
PAC_LIMIT = 10
CLASH_LIMIT = 200
PAC_TYPES = {'http': 'PROXY', 'https': 'HTTPS', 'socks4': 'SOCKS', 'socks5': 'SOCKS5', 'socks5h': 'SOCKS5'}
CLASH_TYPES = {'http': 'http', 'socks5': 'socks5', 'socks5h': 'socks5'}


def split(proxy):
    scheme, _, address = proxy.partition('://')
    host, _, port = address.rpartition(':')
    return scheme, host.strip('[]'), int(port)


def pac(proxies, limit=PAC_LIMIT):
    """proxy.pac: the best proxies in order; no DIRECT fallback, so traffic never leaks."""
    entries = []
    for proxy in proxies:
        scheme, host, port = split(proxy)
        if scheme in PAC_TYPES:
            entries.append(f'{PAC_TYPES[scheme]} {"[" + host + "]" if ":" in host else host}:{port}')
        if len(entries) >= limit:
            break
    value = '; '.join(entries) or 'PROXY 127.0.0.1:9'
    return ('// Proxy Workbench: best working proxies from the latest check.\n'
            '// Browsers try them in order. Re-check often: public proxies go away.\n'
            'function FindProxyForURL(url, host) {\n'
            f'  return {json.dumps(value)};\n'
            '}\n')


def clash(rows, limit=CLASH_LIMIT):
    """A complete Clash / Mihomo config with an automatic fastest-proxy group."""
    proxies = []
    for row in rows:
        scheme, host, port = split(row['proxy'])
        if scheme not in CLASH_TYPES:
            continue
        country = row.get('country') or '??'
        name = f'{country} {CLASH_TYPES[scheme]} {host}:{port}'
        proxies.append({'name': name, 'type': CLASH_TYPES[scheme], 'server': host, 'port': port})
        if len(proxies) >= limit:
            break
    names = [proxy['name'] for proxy in proxies] or ['DIRECT']
    lines = ['# Proxy Workbench: working proxies from the latest check, fastest picked automatically.',
             'mixed-port: 7890', 'allow-lan: false', 'mode: rule', 'log-level: warning']
    # JSON flow mappings are valid YAML and need no YAML library.
    lines += ['proxies:', *(f'  - {json.dumps(proxy, ensure_ascii=False)}' for proxy in proxies)] if proxies \
        else ['proxies: []']
    lines += ['proxy-groups:',
              f'  - {json.dumps({"name": "auto", "type": "url-test", "url": "http://www.gstatic.com/generate_204", "interval": 300, "tolerance": 100, "proxies": names}, ensure_ascii=False)}',
              'rules:', '  - MATCH,auto']
    return '\n'.join(lines) + '\n'


SINGBOX_TYPES = {'http': ('http', None), 'socks4': ('socks', '4'), 'socks5': ('socks', '5'), 'socks5h': ('socks', '5')}


def singbox(rows, limit=CLASH_LIMIT):
    """A sing-box config: a local mixed inbound on 127.0.0.1:2080 and an automatic fastest-proxy group."""
    outbounds = []
    for row in rows:
        scheme, host, port = split(row['proxy'])
        if scheme not in SINGBOX_TYPES:
            continue
        kind, version = SINGBOX_TYPES[scheme]
        outbound = {'type': kind, 'tag': f"{row.get('country') or '??'} {scheme} {host}:{port}",
                    'server': host, 'server_port': port}
        if version:
            outbound['version'] = version
        outbounds.append(outbound)
        if len(outbounds) >= limit:
            break
    tags = [outbound['tag'] for outbound in outbounds]
    auto = ({'type': 'urltest', 'tag': 'auto', 'outbounds': tags, 'url': 'http://www.gstatic.com/generate_204',
             'interval': '5m'} if tags else {'type': 'direct', 'tag': 'auto'})
    config = {'log': {'level': 'warn'},
              'inbounds': [{'type': 'mixed', 'tag': 'in', 'listen': '127.0.0.1', 'listen_port': 2080}],
              'outbounds': [auto, *outbounds],
              'route': {'final': 'auto'}}
    return json.dumps(config, ensure_ascii=False, indent=2) + '\n'
