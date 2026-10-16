"""Separate external-artwork and administrator LAN image transport policies."""

import ipaddress
import socket
from urllib.parse import urlsplit, urljoin
from requests import Session
from . import network
from requests.adapters import HTTPAdapter
from fastapi import HTTPException


def origin(url):
    try:
        parts = urlsplit(url)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.fragment
        ):
            raise ValueError()
        return (
            parts.scheme,
            parts.hostname.lower(),
            parts.port or (443 if parts.scheme == "https" else 80),
        )
    except ValueError:
        raise HTTPException(422, "Invalid image URL.") from None


class PinnedHTTPS(HTTPAdapter):
    def __init__(self, address, hostname, port):
        self.address = address
        self.hostname = hostname
        self.port = port
        super().__init__()

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        return self.poolmanager.connection_from_host(
            self.address,
            port=self.port,
            scheme="https",
            pool_kwargs={"server_hostname": self.hostname, "assert_hostname": self.hostname},
        )


def external_get(url, headers):
    scheme, host, port = origin(url)
    if scheme != "https":
        raise HTTPException(422, "External artwork requires HTTPS.")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    except OSError:
        raise HTTPException(502, "Could not resolve the artwork host.") from None
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise HTTPException(422, "External artwork must use a public internet address.")
    session = Session()
    session.trust_env = False
    session.mount("https://", PinnedHTTPS(sorted(addresses)[0], host, port))
    # Never forward caller service credentials to artwork origins.
    safe = {"User-Agent": "Harmonia", "Host": host if port == 443 else f"{host}:{port}"}
    try:
        response = network.get(
            url,
            headers=safe,
            stream=True,
            timeout=(5, 5),
            allow_redirects=False,
            _session=session,
            _log_body=False,
        )
    except Exception:
        session.close()
        raise
    close = response.close

    def cleanup():
        try:
            close()
        finally:
            session.close()

    response.close = cleanup
    return response


def get(url, headers=None, external=False):
    initial = origin(url)
    for hop in range(4):
        if external:
            response = external_get(url, headers)
        else:
            if origin(url) != initial:
                raise HTTPException(502, "Plex image redirect to another origin was blocked.")
            response = network.get(
                url,
                headers=headers,
                stream=True,
                timeout=(5, 5),
                allow_redirects=False,
                _service="Plex artwork",
                _log_body=False,
            )
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("Location", "")
        response.close()
        if not location:
            raise HTTPException(502, "Image redirect is missing its destination.")
        url = urljoin(url, location)
        # Validate before the next request, not after credentials have been sent.
        if not external and origin(url) != initial:
            raise HTTPException(502, "Plex image redirect to another origin was blocked.")
    raise HTTPException(502, "Too many image redirects.")
