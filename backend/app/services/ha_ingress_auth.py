"""Signing in with the Home Assistant account that opened the sidebar panel.

When Bambuddy runs as a Home Assistant add-on, whoever opens its panel has
already authenticated to Home Assistant. Asking them to log in a second time,
to a separate set of credentials, is the kind of friction that makes people
turn Bambuddy's own auth off — which is worse than either option. So the
Supervisor's ingress headers are accepted as proof of identity and exchanged
for an ordinary Bambuddy JWT; from there every existing permission check works
unchanged.

**Those headers are only trustworthy on the ingress path**, and nothing about
the headers themselves says where they came from — anyone who can reach the
add-on's port directly can send `X-Remote-User-Name: admin`. Four things must
therefore all hold before a token is issued:

1. The add-on explicitly enabled this (``BAMBUDDY_HA_INGRESS_AUTH``). A
   Bambuddy running anywhere else never takes this path at all.
2. The request carries a well-formed ingress prefix.
3. The peer address is inside the Supervisor's own network. This is the load-
   bearing one: the Supervisor is the only thing that can reach the add-on
   from that network, and a request from the LAN cannot forge its source
   address and still receive the response.
4. Home Assistant actually identified a user in the headers.

The resulting Bambuddy account is namespaced (``ha-<name>``) so it can never
collide with, or take over, a local account of the same name.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re

logger = logging.getLogger(__name__)

# Home Assistant's ingress adds these on every proxied request.
USER_ID_HEADER = "X-Remote-User-Id"
USER_NAME_HEADER = "X-Remote-User-Name"
DISPLAY_NAME_HEADER = "X-Remote-User-Display-Name"

# The Supervisor's internal docker network. Add-ons see requests from
# 172.30.32.x; the /23 covers the documented hassio range. Overridable for
# installations that moved it, and for tests.
DEFAULT_SUPERVISOR_CIDR = "172.30.32.0/23"

ENV_ENABLED = "BAMBUDDY_HA_INGRESS_AUTH"
ENV_ROLE = "BAMBUDDY_HA_INGRESS_ROLE"
ENV_SUPERVISOR_CIDR = "BAMBUDDY_HA_SUPERVISOR_CIDR"

# Prefix that keeps provisioned accounts in their own namespace. A Home
# Assistant user called "admin" becomes "ha-admin", which is a different
# account from Bambuddy's own "admin" and cannot inherit its permissions.
USERNAME_PREFIX = "ha-"

_SAFE_NAME = re.compile(r"[^a-z0-9._-]+")


class IngressAuthUnavailable(Exception):
    """This request may not exchange ingress headers for a token.

    Carries a reason for the log. It is deliberately NOT shown to the caller:
    "which of the four checks failed" is a probing oracle, and the frontend
    only needs to know that it should fall back to the normal login form.
    """


def is_enabled() -> bool:
    return os.environ.get(ENV_ENABLED, "").strip().lower() in ("1", "true", "yes", "on")


def configured_role() -> str:
    """Role given to accounts provisioned from Home Assistant.

    Anything other than "admin" is a plain user — a typo in the add-on option
    must not silently produce administrators.
    """
    return "admin" if os.environ.get(ENV_ROLE, "").strip().lower() == "admin" else "user"


def _supervisor_network() -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    raw = os.environ.get(ENV_SUPERVISOR_CIDR, "").strip() or DEFAULT_SUPERVISOR_CIDR
    try:
        return ipaddress.ip_network(raw, strict=False)
    except ValueError:
        logger.warning(
            "%s is not a network (%r); falling back to %s", ENV_SUPERVISOR_CIDR, raw, DEFAULT_SUPERVISOR_CIDR
        )
        return ipaddress.ip_network(DEFAULT_SUPERVISOR_CIDR)


def peer_is_supervisor(client_host: str | None) -> bool:
    if not client_host:
        return False
    try:
        address = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    network = _supervisor_network()
    if address.version != network.version:
        return False
    return address in network


def sanitize_username(raw: str) -> str:
    """Namespaced, storable username for a Home Assistant identity."""
    candidate = _SAFE_NAME.sub("-", (raw or "").strip().lower()).strip("-.")
    return f"{USERNAME_PREFIX}{candidate}" if candidate else ""


def resolve_identity(request) -> tuple[str, str]:
    """``(username, display_name)`` for the Home Assistant user on this request.

    Raises :class:`IngressAuthUnavailable` unless all four conditions hold.
    """
    from backend.app.core.ingress import is_ingress_request

    if not is_enabled():
        raise IngressAuthUnavailable("Home Assistant ingress auth is not enabled")
    if not is_ingress_request(request):
        raise IngressAuthUnavailable("request did not arrive through ingress")

    client_host = getattr(getattr(request, "client", None), "host", None)
    if not peer_is_supervisor(client_host):
        raise IngressAuthUnavailable(f"peer {client_host!r} is not the Supervisor")

    ha_user_id = (request.headers.get(USER_ID_HEADER) or "").strip()
    ha_username = (request.headers.get(USER_NAME_HEADER) or "").strip()
    display_name = (request.headers.get(DISPLAY_NAME_HEADER) or "").strip()
    if not ha_user_id:
        # No user id means Home Assistant did not attach an identity — a
        # long-lived-token API call, for instance. Nothing to sign in as.
        raise IngressAuthUnavailable("no Home Assistant user on the request")

    # Prefer the HA username: it is unique within Home Assistant, so the
    # mapping stays stable when someone renames their display name. The id is
    # the fallback for installations where the username header is absent.
    username = sanitize_username(ha_username) or sanitize_username(ha_user_id)
    if not username:
        raise IngressAuthUnavailable("Home Assistant identity has no usable name")

    return username, (display_name or ha_username or username)
