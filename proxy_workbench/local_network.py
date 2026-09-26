"""Read the local routing choice without hostname DNS or network traffic."""
import ipaddress
import socket


def route_addresses():
    """Return the local source addresses selected for IPv4 and IPv6 routes.

    UDP connect only asks the kernel to choose a route and a source address;
    no packet is transmitted without send/sendto. Numeric documentation
    addresses avoid the hostname resolver, which can stall desktop startup
    for minutes on offline machines and hosted macOS runners.
    """
    found = set()
    for family, target in ((socket.AF_INET, ('192.0.2.1', 9)),
                           (socket.AF_INET6, ('2001:db8::1', 9, 0, 0))):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as connection:
                connection.settimeout(.2)
                connection.connect(target)
                address = connection.getsockname()[0]
                parsed = ipaddress.ip_address(address)
                if not parsed.is_loopback and not parsed.is_unspecified:
                    found.add(address)
        except (OSError, ValueError):
            continue
    return tuple(sorted(found))
