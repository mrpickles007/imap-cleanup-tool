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

import email.header
import re
import urllib.parse
import urllib.request

_URI_RE = re.compile(r"<([^>]+)>")


def _decode_mime_words(value: str) -> str:
    """Decode RFC 2047 encoded-words in a header value.

    Some senders MIME-encode their ``List-Unsubscribe`` (e.g.
    ``=?us-ascii?Q?=3Cmailto=3A...?=``), which hides the ``<...>`` URIs from a
    plain regex. Returns the input unchanged if it isn't encoded or on any error.
    """
    if not value or "=?" not in value:
        return value or ""
    try:
        return str(email.header.make_header(email.header.decode_header(value)))
    except Exception:  # pylint: disable=broad-exception-caught
        return value


def parse_list_unsubscribe(value: str, post: str = "") -> dict:
    """Parse a ``List-Unsubscribe`` (+ ``List-Unsubscribe-Post``) into parts.

    Returns ``{"mailto": str|None, "http": str|None, "oneclick": bool}`` -
    the first mailto: URI, the first http(s) URI, and whether RFC 8058 one-click
    POST is advertised (``post`` contains ``List-Unsubscribe=One-Click``).
    """
    value = _decode_mime_words(value)
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


_ONE_CLICK_BODY = b"List-Unsubscribe=One-Click"
_ONE_CLICK_HEADERS = {"Content-Type": "application/x-www-form-urlencoded",
                      "User-Agent": "imap-cleanup-tool"}


class _RepostRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects while keeping the One-Click **POST + body**.

    urllib's default downgrades a 301/302/303 POST to a bodyless GET (which
    would drop ``List-Unsubscribe=One-Click`` and silently no-op the
    unsubscribe), and won't auto-follow a 307/308 that carries a body. Some
    RFC 8058 endpoints sit behind a redirect, so we re-issue the same POST to
    the new location instead. The parent still enforces the redirect-count caps.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = newurl.replace(" ", "%20")
        if not newurl.lower().startswith(("http://", "https://")):
            return None                         # don't follow to other schemes
        return urllib.request.Request(
            newurl, data=_ONE_CLICK_BODY, method="POST",
            headers=dict(_ONE_CLICK_HEADERS),
            origin_req_host=req.origin_req_host, unverifiable=True)


def http_one_click(url: str, timeout: int = 15) -> bool:
    """RFC 8058 one-click unsubscribe: POST ``List-Unsubscribe=One-Click``.

    Follows redirects **as POST** (keeping the body) via ``_RepostRedirect``.
    Returns True on a 2xx response, False otherwise. Never raises - any network
    error is reported as a failure to the caller.
    """
    req = urllib.request.Request(
        url, data=_ONE_CLICK_BODY, method="POST",
        headers=dict(_ONE_CLICK_HEADERS))
    opener = urllib.request.build_opener(_RepostRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:  # pylint: disable=broad-exception-caught
        return False
