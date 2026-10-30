"""Bounded public website reads. Validate every redirect and pin the resolved IP.

The connection retains the original Host header and TLS hostname verification;
only its TCP destination is pinned, preventing a second DNS lookup/rebinding.
"""

import http.client
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

MAX_WEBSITE_BYTES = 4 * 1024 * 1024
MAX_REDIRECTS = 3


def public_destination(url: str) -> tuple:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use a public http or https website without credentials")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as error:
        raise ValueError("Invalid website port") from error
    if port not in {80, 443}:
        raise ValueError("Website ingestion supports public HTTP and HTTPS ports only")
    addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError("The website could not be resolved")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0].split("%")[0])
        if not ip.is_global or (getattr(ip, "ipv4_mapped", None) and not ip.ipv4_mapped.is_global):
            raise ValueError("Private, local and special-use website destinations are not allowed")
    return parsed, port, addresses[0][4][0]


def fetch_public_text(url: str) -> str:
    for redirect in range(MAX_REDIRECTS + 1):
        parsed, port, address = public_destination(url)
        connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(parsed.hostname, port, timeout=15)
        connection._create_connection = lambda ignored, timeout, source_address=None: socket.create_connection(
            (address, port), timeout=timeout, source_address=source_address)
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection.request("GET", path, headers={"User-Agent": "RelayDesk knowledge crawler/1.0", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location or redirect == MAX_REDIRECTS:
                    raise ValueError("The website redirected too many times or omitted its destination")
                url = urljoin(url, location)
                continue  # the next iteration resolves and validates the new destination
            if response.status >= 400:
                raise ValueError(f"The website returned HTTP {response.status}")
            content_type = response.getheader("Content-Type", "").lower()
            if content_type and not any(value in content_type for value in ("text/html", "text/plain", "application/xhtml+xml")):
                raise ValueError("The website must return HTML or plain text")
            if response.getheader("Content-Encoding", "identity").lower() not in {"", "identity"}:
                raise ValueError("Compressed website responses are not supported")
            content = response.read(MAX_WEBSITE_BYTES + 1)
            if len(content) > MAX_WEBSITE_BYTES:
                raise ValueError("The website response is too large (maximum 4 MB)")
            return content.decode("utf-8", errors="replace")
        finally:
            connection.close()
    raise ValueError("The website redirected too many times")
