"""Proxy socket helpers for non-HTTP protocols (SMTP, IMAP, etc.)."""

from __future__ import annotations

import base64
import http.client
import socket
from urllib.parse import urlparse

from loguru import logger


def create_proxy_socket(
    proxy_url: str,
    target_host: str,
    target_port: int,
    timeout: float = 30.0,
) -> socket.socket:
    """Create a socket to *target_host:target_port* through the given proxy.

    Supported proxy schemes:
      - ``socks5://[user:pass@]host:port``
      - ``http://[user:pass@]host:port``   (HTTP CONNECT tunnel)

    Returns a connected :class:`socket.socket` ready for use (or SSL wrapping).
    """
    parsed = urlparse(proxy_url)
    scheme = (parsed.scheme or "").lower()
    proxy_host = parsed.hostname or ""
    proxy_port = parsed.port
    username = parsed.username
    password = parsed.password

    if not proxy_host or not proxy_port:
        raise ValueError(f"Invalid proxy URL: {proxy_url!r} (missing host or port)")

    if scheme == "socks5":
        return _socks5_connect(
            proxy_host,
            proxy_port,
            target_host,
            target_port,
            username=username,
            password=password,
            timeout=timeout,
        )
    elif scheme in ("http", "https"):
        return _http_connect(
            proxy_host,
            proxy_port,
            target_host,
            target_port,
            username=username,
            password=password,
            timeout=timeout,
        )
    else:
        raise ValueError(
            f"Unsupported proxy scheme {scheme!r} in {proxy_url!r}. Expected 'socks5' or 'http'."
        )


def _socks5_connect(
    proxy_host: str,
    proxy_port: int,
    target_host: str,
    target_port: int,
    *,
    username: str | None = None,
    password: str | None = None,
    timeout: float = 30.0,
) -> socket.socket:
    """Connect via SOCKS5 proxy using python-socks."""
    try:
        from python_socks import ProxyType
        from python_socks.sync import Proxy
    except ImportError:
        raise ImportError(
            "python-socks is required for SOCKS5 proxy support. "
            "Install it with: pip install python-socks"
        )

    proxy = Proxy(
        proxy_type=ProxyType.SOCKS5,
        host=proxy_host,
        port=proxy_port,
        username=username,
        password=password,
        rdns=True,
    )
    sock = proxy.connect(dest_host=target_host, dest_port=target_port, timeout=timeout)
    logger.debug(
        "SOCKS5 proxy tunnel established: {}:{} -> {}:{}",
        proxy_host,
        proxy_port,
        target_host,
        target_port,
    )
    return sock


def _http_connect(
    proxy_host: str,
    proxy_port: int,
    target_host: str,
    target_port: int,
    *,
    username: str | None = None,
    password: str | None = None,
    timeout: float = 30.0,
) -> socket.socket:
    """Connect via HTTP CONNECT tunnel.

    Uses a raw HTTP request to the proxy so we get back a plain socket
    suitable for subsequent SMTP/IMAP traffic (including STARTTLS).
    """
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        conn = http.client.HTTPConnection(proxy_host, proxy_port, timeout=timeout)
        conn.sock = sock

        headers: dict[str, str] = {}
        if username is not None:
            creds = f"{username}:{password or ''}"
            encoded = base64.b64encode(creds.encode()).decode()
            headers["Proxy-Authorization"] = f"Basic {encoded}"

        conn.set_tunnel(target_host, target_port, headers=headers)
        conn.connect()

        # conn.connect() with a tunnel sends CONNECT and waits for 200.
        # After that, conn.sock is the tunneled raw socket.
        tunneled = conn.sock
        conn.close()  # detach without closing the underlying socket
        logger.debug(
            "HTTP CONNECT tunnel established: {}:{} -> {}:{}",
            proxy_host,
            proxy_port,
            target_host,
            target_port,
        )
        return tunneled
    except Exception:
        sock.close()
        raise
