"""Network/environment DNS triage. No network activity without --live.

No DNS workarounds or crawler permissions are inferred from this tool.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import platform
import socket
from pathlib import Path

DEFAULT_HOSTS = ('example.com', 'boards-api.greenhouse.io', 'api.ashbyhq.com')


def resolve_host(host: str) -> dict:
    try:
        records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = sorted({item[4][0] for item in records})
        return {'host': host, 'ok': True, 'address_count': len(addresses)}
    except (OSError, socket.gaierror) as exc:
        return {'host': host, 'ok': False, 'error_type': type(exc).__name__,
                'error': str(exc)[:160]}


def diagnose(*, live: bool = False, hosts: tuple[str, ...] = DEFAULT_HOSTS) -> dict:
    # Do not dump env values: proxy variables can contain credentials.
    out = {
        'mode': 'dns_live' if live else 'environment_only',
        'python': platform.python_version(), 'platform': platform.system(),
        'proxies_configured': {key: bool(os.getenv(key)) for key in
                               ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY')},
        'loopback': resolve_host('localhost'),
        'external_dns': [],
        'diagnosis': 'NO_EXTERNAL_LOOKUP_PERFORMED',
        'notes': ['A working resolver is only evidence of DNS, not HTTP availability or site permission.'],
    }
    if Path('/etc/resolv.conf').is_file():
        try:
            contents = Path('/etc/resolv.conf').read_text(errors='replace')
            out['configured_nameservers'] = sum(line.lstrip().startswith('nameserver ') for line in contents.splitlines())
        except OSError:
            pass
    if not live:
        return out
    if not hosts or len(hosts) > 8 or any(not h or len(h) > 253 or '/' in h for h in hosts):
        raise ValueError('Supply 1..8 plain domain names')
    # Multiple independent domains distinguish environment-wide resolver failure
    # from a single misconfigured hostname. Not a claim about root cause.
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        out['external_dns'] = list(pool.map(resolve_host, hosts))
    passed = sum(x['ok'] for x in out['external_dns'])
    out['diagnosis'] = ('DNS_OK' if passed == len(hosts) else
                        'DNS_UNAVAILABLE' if passed == 0 else 'DNS_PARTIAL')
    out['notes'].append({
        'DNS_UNAVAILABLE': 'All independent external lookups failed: inspect DNS/network restrictions before debugging crawlers.',
        'DNS_PARTIAL': 'Some hosts resolve: inspect per-domain DNS, VPN, proxy, and provider network rules.',
        'DNS_OK': 'DNS resolves; troubleshoot TCP/TLS/HTTP/robots separately if requests still fail.',
    }[out['diagnosis']])
    return out


def main() -> None:
    from .console import configure_console
    configure_console()
    parser = argparse.ArgumentParser(description='Safe resolver diagnostics; use --live for DNS lookups')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--hosts', nargs='+', default=list(DEFAULT_HOSTS))
    parser.add_argument('--output', help='Write JSON result (optional)')
    args = parser.parse_args()
    report = diagnose(live=args.live, hosts=tuple(args.hosts))
    content = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(content + '\n', encoding='utf-8')
    print(content)


if __name__ == '__main__':
    main()
