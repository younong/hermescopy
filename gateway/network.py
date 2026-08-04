"""Network listener address classification shared by connector transports."""

from __future__ import annotations

import ipaddress
import socket


def is_network_accessible(host: str) -> bool:
    """Return whether *host* exposes a listener beyond loopback."""
    try:
        address = ipaddress.ip_address(host)
        if address.is_loopback:
            return False
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None and mapped.is_loopback:
            return False
        return True
    except ValueError:
        pass

    try:
        resolved = socket.getaddrinfo(
            host,
            None,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )
        return any(
            not ipaddress.ip_address(sockaddr[0]).is_loopback
            for _family, _type, _proto, _canonname, sockaddr in resolved
        )
    except (socket.gaierror, OSError):
        return True
