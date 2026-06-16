"""Parse ``List-Unsubscribe`` headers and perform one-click unsubscribes.

A bulk sender's ``List-Unsubscribe`` header (RFC 2369) holds one or more URIs in
angle brackets, e.g.::

    <mailto:unsub@list.example?subject=unsubscribe>, <https://example/u?id=abc>

There are three cases, with different automation levels:

* **mailto:** - unsubscribe by **sending an email** to that address (we use the
  active SMTP profile). Fully automatic.
* **https + ``List-Unsubscribe-Post: List-Unsubscribe=One-Click``** (RFC 8058) -
  unsubscribe with a single **HTTPS POST**. Fully automatic.
* **https without one-click** - usually a confirmation page; it **can't be
  automated** reliably, so we just hand back the link to open.

Standard library only (``urllib`` for the POST; the e-mail send lives in
``notifications``). No third-party deps.
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request

_URI_RE = re.compile(r"<([^>]+)>")


def parse_list_unsubscribe(value: str, post: str = "") -> dict:
    """Parse a ``List-Unsubscribe`` (+ ``List-Unsubscribe-Post``) into parts.

    Returns ``{"mailto": str|None, "http": str|None, "oneclick": bool}`` -
    the first mailto: URI, the first http(s) URI, and whether RFC 8058 one-click
    POST is advertised (``post`` contains ``List-Unsubscribe=One-Click``).
    """
    mailto = http = None
    for uri in _URI_RE.findall(value or ""):
        u = uri.strip()
        low = u.lower()
        if low.startswith("mailto:") and mailto is None:
            mailto = u
        elif (low.startswith("http://") or low.startswith("https://")) \
                and http is None:
            http = u
    oneclick = "one-click" in (post or "").lower()
    return {"mailto": mailto, "http": http, "oneclick": bool(oneclick and http)}


def parse_mailto(uri: str) -> tuple[str, str, str]:
    """Split a ``mailto:addr?subject=..&body=..`` URI into (to, subject, body)."""
    rest = uri[len("mailto:"):] if uri.lower().startswith("mailto:") else uri
    addr, _, query = rest.partition("?")
    params = urllib.parse.parse_qs(query)
    subject = (params.get("subject") or ["unsubscribe"])[0]
    body = (params.get("body") or ["unsubscribe"])[0]
    return urllib.parse.unquote(addr).strip(), subject, body


def http_one_click(url: str, timeout: int = 15) -> bool:
    """RFC 8058 one-click unsubscribe: POST ``List-Unsubscribe=One-Click``.

    Returns True on a 2xx response, False otherwise. Never raises - any network
    error is reported as a failure to the caller.
    """
    data = b"List-Unsubscribe=One-Click"
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "imap-cleanup-tool"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:  # pylint: disable=broad-exception-caught
        return False
