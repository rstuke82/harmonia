"""Validated, DNS-pinned notification delivery with redirects disabled."""

import ipaddress
import socket
from urllib.parse import urlsplit

from requests import Session
from fastapi import HTTPException

from . import network
from .redirect_policy import PinnedHTTPS


def destination(url, allow_private=False):
    try:
        parts = urlsplit(url)
        port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
        if (
            not url
            or len(url) > 2048
            or any(c.isspace() for c in url)
            or parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
            or not 1 <= port <= 65535
        ):
            raise ValueError()
    except ValueError:
        raise HTTPException(
            422, "Enter a complete webhook URL without embedded credentials or a fragment."
        ) from None
    try:
        addresses = {
            entry[4][0]
            for entry in socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
        }
        parsed = [ipaddress.ip_address(address) for address in addresses]
    except (OSError, ValueError):
        raise HTTPException(422, "Could not resolve the notification server.") from None
    if not parsed or any(
        ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved for ip in parsed
    ):
        raise HTTPException(422, "This notification destination is not allowed.")
    if any(not ip.is_global for ip in parsed) and not allow_private:
        raise HTTPException(
            422, "Enable local notification server access to use a private address."
        )
    if parts.scheme != "https" and not (allow_private and all(not ip.is_global for ip in parsed)):
        raise HTTPException(422, "Internet notification servers require HTTPS.")
    return parts, sorted(addresses)[0], port


class PinnedHTTP(PinnedHTTPS):
    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        if request.url.startswith("https:"):
            return super().get_connection_with_tls_context(request, verify, proxies, cert)
        return self.poolmanager.connection_from_host(self.address, port=self.port, scheme="http")


def post(url, payload, allow_private=False):
    parts, address, port = destination(url, allow_private)
    default_port = 443 if parts.scheme == "https" else 80
    host = parts.hostname
    if ":" in host:
        host = "[" + host + "]"
    if port != default_port:
        host += ":" + str(port)
    with Session() as session:
        session.trust_env = False
        session.mount(parts.scheme + "://", PinnedHTTP(address, parts.hostname, port))
        response = network.request(
            "POST",
            url,
            _session=session,
            _service="Notification webhook",
            _safe_path="/webhook",
            _log_body=False,
            headers={"Host": host},
            json=payload,
            timeout=(5, 10),
            allow_redirects=False,
            stream=True,
        )
        try:
            if 300 <= response.status_code < 400:
                raise HTTPException(
                    502, "The notification server redirected the request. Configure its final URL."
                )
            response.raise_for_status()
        finally:
            response.close()
