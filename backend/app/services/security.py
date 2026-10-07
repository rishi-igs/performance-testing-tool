"""Target URL validation (SSRF protection) and API-key checks."""
from __future__ import annotations

import hmac
import ipaddress
import socket
from urllib.parse import urlsplit

from ..config import Settings


class TargetNotAllowed(ValueError):
    pass


def check_target(url: str, settings: Settings) -> None:
    """Reject targets that point at internal infrastructure.

    * Hosts listed in ALLOWED_HOSTS are always accepted.
    * Link-local (cloud metadata, 169.254.0.0/16), unspecified and multicast
      addresses are always rejected.
    * Private and loopback addresses are rejected unless ALLOW_PRIVATE_TARGETS=true.

    Note: DNS is resolved here and again by JMeter, so a hostile DNS server could
    change its answer in between. Run the tool on a network that cannot reach
    sensitive internal services if you need protection against that.
    """
    host = (urlsplit(url).hostname or "").lower()
    if host in settings.allowed_hosts:
        return
    port = urlsplit(url).port or (443 if url.startswith("https") else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise TargetNotAllowed(f"Cannot resolve host '{host}'") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            raise TargetNotAllowed(f"Target address {ip} is not allowed")
        if (ip.is_private or ip.is_loopback or ip.is_reserved) and not settings.allow_private_targets:
            raise TargetNotAllowed(
                f"Target address {ip} is private. Add the host to ALLOWED_HOSTS or set "
                "ALLOW_PRIVATE_TARGETS=true for local testing."
            )


def api_key_ok(provided: str | None, settings: Settings) -> bool:
    if not settings.api_key:
        return True
    if not provided:
        return False
    return hmac.compare_digest(provided.encode(), settings.api_key.encode())
