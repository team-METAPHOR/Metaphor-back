"""Client address for rate limits, honoring X-Forwarded-For only from trusted proxies."""

import ipaddress
import logging

from starlette.requests import Request

from app.core.config import get_settings

logger = logging.getLogger(__name__)
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
# Warn once per process: behind an unlisted proxy every client shares one rate-limit key.
_warned_untrusted_xff = False


def parse_ip(value: str) -> IPAddress | None:
    """Canonical address from "1.2.3.4", "1.2.3.4:80", "[v6]:443", "::ffff:1.2.3.4"; else None.

    One client must map to one rate-limit key, so ports, brackets, zone ids, case and
    IPv4-mapped IPv6 forms are normalized away.
    """
    value = value.strip()
    if value.startswith("["):
        host, sep, rest = value[1:].partition("]")
        if not sep or (rest and not rest.startswith(":")):
            return None
        value = host
    elif value.count(":") == 1:
        value = value.split(":", 1)[0]
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def _warn_untrusted_xff() -> None:
    global _warned_untrusted_xff
    if not _warned_untrusted_xff:
        _warned_untrusted_xff = True
        # Never log the header value: it is client-controlled.
        logger.warning("X-Forwarded-For received from untrusted peer; set TRUSTED_PROXIES")


def client_ip(request: Request) -> str:
    """Peer address, or the right-most X-Forwarded-For hop that is not a trusted proxy.

    XFF is ignored unless the direct peer is in settings.trusted_proxies, so clients cannot
    spoof it. Entries left of the first untrusted hop are client-controlled and never used.
    An unparsable hop stops the walk and the (trusted) peer is returned instead.
    """
    raw_peer = request.client.host if request.client else ""
    peer = parse_ip(raw_peer)
    if peer is None:
        return raw_peer
    networks = [ipaddress.ip_network(n, strict=False) for n in get_settings().trusted_proxies]

    def trusted(ip: IPAddress) -> bool:
        return any(ip in net for net in networks)

    if not trusted(peer):
        if "x-forwarded-for" in request.headers:
            _warn_untrusted_xff()
        return str(peer)
    hops = [h for v in request.headers.getlist("x-forwarded-for") for h in v.split(",")]
    for hop in reversed(hops):
        ip = parse_ip(hop)
        if ip is None:
            return str(peer)
        if not trusted(ip):
            return str(ip)
    return str(peer)
