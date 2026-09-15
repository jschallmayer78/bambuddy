"""Serving Bambuddy from behind Home Assistant's ingress.

A Home Assistant add-on that appears in the sidebar is reached through the
Supervisor's ingress proxy, which mounts the app under a per-session path like
``/api/hassio_ingress/aBc123…/``. That path is not known at build time, it
changes between sessions, and the app is *also* reachable directly on its own
port — so nothing may be hard-coded to either shape.

The fix is one line of HTML: the index page is served with a ``<base href>``
naming the prefix the browser is actually using, and every URL the frontend
builds is relative to it. Direct access gets ``<base href="/">``, which is
exactly what a document with no base tag already resolved to, so that path is
unchanged.

``X-Ingress-Path`` is attacker-controllable — anyone who can reach the app's
port can send one — so it is validated rather than trusted: a single absolute
path, no scheme, no host, no traversal, no control characters. A header that
fails validation is ignored, not rejected, because the request itself is still
a perfectly good direct request.
"""

from __future__ import annotations

import re

INGRESS_PATH_HEADER = "X-Ingress-Path"

# Home Assistant sends "/api/hassio_ingress/<token>". Deliberately strict: one
# leading slash, then path segments of unreserved URL characters. That rules
# out "//evil.example" (a protocol-relative URL, which would point <base> at
# another origin), "/x/../../y", backslashes, and anything with a control
# character or whitespace that could break out of the attribute.
_VALID_PREFIX = re.compile(r"^/[A-Za-z0-9._~\-/]{0,255}$")


def ingress_prefix(request) -> str:
    """The validated ingress prefix for this request, or ``""``.

    Returned without a trailing slash so callers can append one deliberately.
    """
    raw = request.headers.get(INGRESS_PATH_HEADER)
    if not raw:
        return ""
    candidate = raw.strip()
    if not candidate or candidate == "/":
        return ""
    if ".." in candidate or candidate.startswith("//"):
        return ""
    if not _VALID_PREFIX.match(candidate):
        return ""
    return candidate.rstrip("/")


def base_href(request) -> str:
    """Value for the document's ``<base href>``. Always ends in a slash."""
    prefix = ingress_prefix(request)
    return f"{prefix}/" if prefix else "/"


def is_ingress_request(request) -> bool:
    """Whether this request arrived through a (well-formed) ingress prefix."""
    return bool(ingress_prefix(request))


_HEAD_OPEN = re.compile(rb"<head[^>]*>", re.IGNORECASE)
_EXISTING_BASE = re.compile(rb"<base\s+href=\"[^\"]*\"\s*/?>", re.IGNORECASE)


def inject_base_href(html: bytes, href: str) -> bytes:
    """Put ``<base href="…">`` first inside ``<head>``.

    First, because every relative URL after it resolves against it — including
    the preload and module tags Vite emits at the top of the document.

    The href is a validated path, but it is still escaped here rather than
    interpolated raw: this function is the last thing between a request header
    and an HTML attribute, and it should be safe to read on its own.
    """
    escaped = href.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    tag = f'<base href="{escaped}">'.encode()

    # A build that already carries a base tag (or a second pass over an
    # already-rendered document) is replaced rather than stacked — the first
    # base tag in a document wins, so a stale one would silently take
    # precedence over the one meant for this request.
    if _EXISTING_BASE.search(html):
        return _EXISTING_BASE.sub(tag, html, count=1)

    match = _HEAD_OPEN.search(html)
    if not match:
        # No <head> to anchor to. Serving the document unchanged is the honest
        # outcome: it works on direct access, and under ingress the browser
        # reports the broken asset paths plainly instead of getting a base tag
        # dropped somewhere it does not belong.
        return html
    return html[: match.end()] + tag + html[match.end() :]
