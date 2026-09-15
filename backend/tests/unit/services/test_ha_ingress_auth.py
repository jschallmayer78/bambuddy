"""Who may exchange Home Assistant's ingress headers for a Bambuddy token.

The headers themselves prove nothing — anyone who can reach the add-on's port
can send them. Each test here pins one of the four conditions that together
make them trustworthy.
"""

from types import SimpleNamespace

import pytest

from backend.app.services import ha_ingress_auth as ha


def request_with(*, ingress_path="/api/hassio_ingress/abc", client="172.30.32.2", **headers):
    all_headers = {}
    if ingress_path is not None:
        all_headers["X-Ingress-Path"] = ingress_path
    all_headers.update(headers)
    return SimpleNamespace(
        headers=all_headers,
        client=SimpleNamespace(host=client) if client else None,
    )


def supervisor_request(**overrides):
    defaults = {
        "X-Remote-User-Id": "0f1e2d3c",
        "X-Remote-User-Name": "joerg",
        "X-Remote-User-Display-Name": "Jörg",
    }
    defaults.update(overrides)
    return request_with(**defaults)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv(ha.ENV_ENABLED, "true")
    return monkeypatch


class TestSwitch:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_the_add_on_can_turn_it_on(self, monkeypatch, value):
        monkeypatch.setenv(ha.ENV_ENABLED, value)
        assert ha.is_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "maybe"])
    def test_anything_else_leaves_it_off(self, monkeypatch, value):
        monkeypatch.setenv(ha.ENV_ENABLED, value)
        assert ha.is_enabled() is False

    def test_off_when_the_variable_is_absent(self, monkeypatch):
        monkeypatch.delenv(ha.ENV_ENABLED, raising=False)
        assert ha.is_enabled() is False

    def test_only_an_exact_admin_option_grants_admin(self, monkeypatch):
        monkeypatch.setenv(ha.ENV_ROLE, "admin")
        assert ha.configured_role() == "admin"
        monkeypatch.setenv(ha.ENV_ROLE, "Administrator")
        assert ha.configured_role() == "user", "a typo must not silently mint admins"
        monkeypatch.delenv(ha.ENV_ROLE, raising=False)
        assert ha.configured_role() == "user"


class TestPeerCheck:
    @pytest.mark.parametrize("host", ["172.30.32.2", "172.30.33.9"])
    def test_the_supervisor_network_is_accepted(self, host):
        assert ha.peer_is_supervisor(host) is True

    @pytest.mark.parametrize("host", ["192.168.1.50", "10.0.0.1", "127.0.0.1", "", None, "not-an-ip", "::1"])
    def test_everything_else_is_not(self, host):
        assert ha.peer_is_supervisor(host) is False

    def test_the_network_can_be_moved(self, monkeypatch):
        monkeypatch.setenv(ha.ENV_SUPERVISOR_CIDR, "10.42.0.0/16")
        assert ha.peer_is_supervisor("10.42.7.1") is True
        assert ha.peer_is_supervisor("172.30.32.2") is False

    def test_a_broken_setting_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv(ha.ENV_SUPERVISOR_CIDR, "not a network")
        assert ha.peer_is_supervisor("172.30.32.2") is True


class TestUsernames:
    def test_names_are_namespaced(self):
        assert ha.sanitize_username("joerg") == "ha-joerg"

    def test_a_local_account_cannot_be_impersonated(self):
        """The prefix is the whole point: HA's "admin" is not Bambuddy's."""
        assert ha.sanitize_username("admin") == "ha-admin"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Jörg Schallmayer", "ha-j-rg-schallmayer"),
            ("  spaced  ", "ha-spaced"),
            ("UPPER", "ha-upper"),
            ("../../etc/passwd", "ha-etc-passwd"),
            ("weird!@#$chars", "ha-weird-chars"),
        ],
    )
    def test_anything_unsafe_is_folded_out(self, raw, expected):
        assert ha.sanitize_username(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "!!!", "-", "."])
    def test_a_name_with_nothing_usable_left_yields_nothing(self, raw):
        assert ha.sanitize_username(raw) == ""


class TestResolveIdentity:
    def test_a_supervisor_request_resolves(self, enabled):
        username, display = ha.resolve_identity(supervisor_request())
        assert username == "ha-joerg"
        assert display == "Jörg"

    def test_refused_when_the_add_on_did_not_enable_it(self, monkeypatch):
        monkeypatch.delenv(ha.ENV_ENABLED, raising=False)
        with pytest.raises(ha.IngressAuthUnavailable, match="not enabled"):
            ha.resolve_identity(supervisor_request())

    def test_refused_without_an_ingress_prefix(self, enabled):
        """Direct access to the port, headers and all."""
        request = request_with(
            ingress_path=None,
            **{"X-Remote-User-Id": "x", "X-Remote-User-Name": "admin"},
        )
        with pytest.raises(ha.IngressAuthUnavailable, match="ingress"):
            ha.resolve_identity(request)

    def test_refused_from_the_lan_even_with_perfect_headers(self, enabled):
        """The forgery that the peer check exists to stop."""
        request = request_with(
            client="192.168.1.50",
            **{"X-Remote-User-Id": "x", "X-Remote-User-Name": "admin"},
        )
        with pytest.raises(ha.IngressAuthUnavailable, match="Supervisor"):
            ha.resolve_identity(request)

    def test_refused_when_home_assistant_named_no_user(self, enabled):
        with pytest.raises(ha.IngressAuthUnavailable, match="no Home Assistant user"):
            ha.resolve_identity(request_with())

    def test_the_user_id_stands_in_for_a_missing_username(self, enabled):
        request = supervisor_request(**{"X-Remote-User-Name": "", "X-Remote-User-Display-Name": ""})
        username, display = ha.resolve_identity(request)
        assert username == "ha-0f1e2d3c"
        assert display == "ha-0f1e2d3c"

    def test_the_display_name_is_only_a_label(self, enabled):
        """Renaming a display name must not create a second account."""
        first, _ = ha.resolve_identity(supervisor_request())
        second, _ = ha.resolve_identity(supervisor_request(**{"X-Remote-User-Display-Name": "Someone Else"}))
        assert first == second
