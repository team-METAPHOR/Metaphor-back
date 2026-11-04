"""Client address for rate limits, honoring X-Forwarded-For only from trusted proxies."""

import ipaddress

from starlette.requests import Request

from app.core.config import get_settings


def _trusted(address: str, networks: list) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in net for net in networks)


def client_ip(request: Request) -> str:
    """Peer address, or the right-most X-Forwarded-For hop that is not a trusted proxy.

    XFF is ignored unless the direct peer is in settings.trusted_proxies, so clients cannot
    spoof it. Entries left of the first untrusted hop are client-controlled and never used.
    """
    peer = request.client.host if request.client else ""
    networks = [ipaddress.ip_network(n, strict=False) for n in get_settings().trusted_proxies]
    if not _trusted(peer, networks):
        return peer
    hops = [h.strip() for v in request.headers.getlist("x-forwarded-for") for h in v.split(",")]
    for hop in reversed(hops):
        if hop and not _trusted(hop, networks):
            return hop
    return peer
