"""Bambuddy served through Home Assistant's ingress.

Three things have to hold for the sidebar panel to work, and each of them is
invisible on direct access — which is what made them expensive to find:

* the entry document names the session's prefix, so relative URLs resolve
  inside it,
* the camera stream carries its boundary somewhere the proxy cannot rewrite,
* the Home Assistant session can be exchanged for a Bambuddy one, but only
  from the Supervisor.
"""

from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.user import User
from backend.app.services import ha_ingress_auth as ha

INGRESS_PATH = "/api/hassio_ingress/ZmFrZS1zZXNzaW9u"


@pytest.fixture
def frontend_build(tmp_path, monkeypatch):
    """A stand-in static build, so the test does not need `npm run build`."""
    from backend.app.core.config import settings

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        '<!doctype html><html><head><base href="/">'
        '<link rel="stylesheet" href="./assets/index.css">'
        "</head><body><div id=root></div>"
        '<script type="module" src="./assets/index.js"></script></body></html>'
    )
    monkeypatch.setattr(settings, "static_dir", static_dir)
    return static_dir


class TestEntryDocument:
    @pytest.mark.asyncio
    async def test_the_base_tag_names_the_ingress_session(self, async_client: AsyncClient, frontend_build):
        response = await async_client.get("/", headers={"X-Ingress-Path": INGRESS_PATH})
        assert response.status_code == 200
        assert f'<base href="{INGRESS_PATH}/">' in response.text
        # The shipped root base must be replaced, not joined by a second tag:
        # the first one in the document is the one the browser obeys.
        assert response.text.count("<base") == 1

    @pytest.mark.asyncio
    async def test_direct_access_is_unchanged(self, async_client: AsyncClient, frontend_build):
        response = await async_client.get("/")
        assert response.status_code == 200
        assert '<base href="/">' in response.text

    @pytest.mark.asyncio
    async def test_deep_routes_get_the_tag_too(self, async_client: AsyncClient, frontend_build):
        """A page opened directly at /camera/5 loads its assets from this tag."""
        response = await async_client.get("/camera/5", headers={"X-Ingress-Path": INGRESS_PATH})
        assert response.status_code == 200
        assert f'<base href="{INGRESS_PATH}/">' in response.text

    @pytest.mark.asyncio
    async def test_a_forged_prefix_is_ignored(self, async_client: AsyncClient, frontend_build):
        response = await async_client.get("/", headers={"X-Ingress-Path": "//evil.example"})
        assert '<base href="/">' in response.text
        assert b"evil.example" not in response.content

    @pytest.mark.asyncio
    async def test_the_entry_document_is_still_revalidated(self, async_client: AsyncClient, frontend_build):
        """Rendering it per request must not cost the no-cache guarantee."""
        response = await async_client.get("/")
        assert "no-cache" in response.headers["cache-control"]

    @pytest.mark.asyncio
    async def test_api_routes_are_not_swallowed_by_the_spa(self, async_client: AsyncClient, frontend_build):
        response = await async_client.get("/api/v1/does-not-exist", headers={"X-Ingress-Path": INGRESS_PATH})
        assert response.status_code == 404
        assert "<base" not in response.text


class TestCameraBoundary:
    def test_the_stream_headers_carry_the_boundary_separately(self):
        """Ingress rebuilds Content-Type from its base type and drops the
        boundary; without this header no browser can parse the stream."""
        from backend.app.api.routes.camera import (
            _MJPEG_HEADERS,
            MJPEG_BOUNDARY,
            MJPEG_BOUNDARY_HEADER,
            MJPEG_MEDIA_TYPE,
        )

        assert _MJPEG_HEADERS[MJPEG_BOUNDARY_HEADER] == MJPEG_BOUNDARY
        assert f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}" == MJPEG_MEDIA_TYPE
        # The frames the generators emit have to use that same delimiter.
        from backend.app.services.external_camera import _format_mjpeg_frame

        assert _format_mjpeg_frame(b"x").startswith(f"--{MJPEG_BOUNDARY}".encode())


class TestHomeAssistantSignIn:
    @pytest.fixture
    def as_supervisor(self, monkeypatch):
        """Enable the feature and treat the test client's address as the Supervisor."""
        monkeypatch.setenv(ha.ENV_ENABLED, "true")
        monkeypatch.setenv(ha.ENV_SUPERVISOR_CIDR, "127.0.0.0/8")
        return monkeypatch

    @staticmethod
    def _headers(username="joerg", display="Joerg S"):
        # Plain ASCII: HTTP headers are ASCII on the wire, and Home Assistant
        # encodes anything else before it reaches us. Non-ASCII names are
        # covered where they are actually handled — see the sanitiser's tests.
        return {
            "X-Ingress-Path": INGRESS_PATH,
            "X-Remote-User-Id": "0f1e2d3c",
            "X-Remote-User-Name": username,
            "X-Remote-User-Display-Name": display,
        }

    @pytest.mark.asyncio
    async def test_a_panel_visitor_gets_a_bambuddy_token(self, async_client: AsyncClient, as_supervisor, db_session):
        response = await async_client.post("/api/v1/auth/ha-ingress", headers=self._headers())
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["access_token"]
        assert body["token_type"] == "bearer"
        assert body["user"]["username"] == "ha-joerg"
        assert body["user"]["auth_source"] == "ha"
        assert body["requires_2fa"] is False

        user = (await db_session.execute(select(User).where(User.username == "ha-joerg"))).scalar_one()
        # No password hash at all — this account is only ever reachable through
        # ingress, and a hash is something a reset flow could latch onto.
        assert user.password_hash is None
        assert user.role == "user"

    @pytest.mark.asyncio
    async def test_the_same_visitor_reuses_their_account(self, async_client: AsyncClient, as_supervisor, db_session):
        await async_client.post("/api/v1/auth/ha-ingress", headers=self._headers())
        await async_client.post("/api/v1/auth/ha-ingress", headers=self._headers(display="Renamed"))

        users = (await db_session.execute(select(User).where(User.username == "ha-joerg"))).scalars().all()
        assert len(users) == 1

    @pytest.mark.asyncio
    async def test_the_role_option_decides_what_they_get(self, async_client: AsyncClient, as_supervisor, db_session):
        as_supervisor.setenv(ha.ENV_ROLE, "admin")
        response = await async_client.post("/api/v1/auth/ha-ingress", headers=self._headers(username="chief"))
        assert response.status_code == 200
        assert response.json()["user"]["role"] == "admin"

    @pytest.mark.asyncio
    async def test_refused_when_the_add_on_did_not_enable_it(self, async_client: AsyncClient, monkeypatch):
        monkeypatch.delenv(ha.ENV_ENABLED, raising=False)
        response = await async_client.post("/api/v1/auth/ha-ingress", headers=self._headers())
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_refused_when_the_request_did_not_come_through_ingress(
        self, async_client: AsyncClient, as_supervisor
    ):
        headers = self._headers()
        headers.pop("X-Ingress-Path")
        response = await async_client.post("/api/v1/auth/ha-ingress", headers=headers)
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_refused_from_outside_the_supervisor_network(self, async_client: AsyncClient, monkeypatch):
        """The forged-header case: everything looks right except where it came from."""
        monkeypatch.setenv(ha.ENV_ENABLED, "true")
        monkeypatch.setenv(ha.ENV_SUPERVISOR_CIDR, "172.30.32.0/23")
        response = await async_client.post("/api/v1/auth/ha-ingress", headers=self._headers(username="admin"))
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_the_refusal_says_nothing_about_which_check_failed(self, async_client: AsyncClient, monkeypatch):
        monkeypatch.delenv(ha.ENV_ENABLED, raising=False)
        response = await async_client.post("/api/v1/auth/ha-ingress", headers=self._headers())
        detail = response.json()["detail"]
        assert "ingress" not in detail.lower() or "not available" in detail.lower()
        assert "Supervisor" not in detail

    @pytest.mark.asyncio
    async def test_a_local_account_cannot_be_taken_over(self, async_client: AsyncClient, as_supervisor, db_session):
        """Belt and braces behind the ha- prefix: a local account keeps its own."""
        local = User(username="ha-joerg", password_hash="x", role="admin", auth_source="local", is_active=True)
        db_session.add(local)
        await db_session.commit()

        response = await async_client.post("/api/v1/auth/ha-ingress", headers=self._headers())
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_a_disabled_account_stays_disabled(self, async_client: AsyncClient, as_supervisor, db_session):
        """Home Assistant does not know Bambuddy disabled someone."""
        await async_client.post("/api/v1/auth/ha-ingress", headers=self._headers())
        user = (await db_session.execute(select(User).where(User.username == "ha-joerg"))).scalar_one()
        user.is_active = False
        await db_session.commit()

        response = await async_client.post("/api/v1/auth/ha-ingress", headers=self._headers())
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_the_route_is_public_at_the_gateway(self, async_client: AsyncClient, as_supervisor):
        """It IS the login, so the auth middleware must not demand a token for it."""
        with patch("backend.app.core.auth.is_auth_enabled", return_value=True):
            response = await async_client.post("/api/v1/auth/ha-ingress", headers=self._headers())
        assert response.status_code != 401 or response.json()["detail"] != "Authentication required"


class TestFraming:
    """Home Assistant renders the panel in an iframe on its own origin.

    Bambuddy's default is ``frame-ancestors 'none'`` plus ``X-Frame-Options:
    SAMEORIGIN``, which would make that panel permanently blank with nothing in
    any log to say why — the browser refuses silently.
    """

    @pytest.mark.asyncio
    async def test_an_ingress_request_may_be_framed(self, async_client: AsyncClient, frontend_build):
        response = await async_client.get("/", headers={"X-Ingress-Path": INGRESS_PATH})
        assert "frame-ancestors *;" in response.headers["content-security-policy"]
        # The legacy header would block the embed on its own.
        assert "x-frame-options" not in response.headers

    @pytest.mark.asyncio
    async def test_direct_access_keeps_the_strict_rules(self, async_client: AsyncClient, frontend_build):
        """Where a clickjacking attempt would actually land."""
        response = await async_client.get("/")
        assert "frame-ancestors 'none';" in response.headers["content-security-policy"]
        assert response.headers["x-frame-options"] == "SAMEORIGIN"

    @pytest.mark.asyncio
    async def test_a_forged_prefix_does_not_unlock_framing(self, async_client: AsyncClient, frontend_build):
        """The relaxation rides on the same validation as everything else.

        Worth stating even though a cross-origin iframe load cannot set a
        request header in the first place: that property is what the whole
        relaxation rests on, so the validation had better be the same one.
        """
        response = await async_client.get("/", headers={"X-Ingress-Path": "//evil.example"})
        assert "frame-ancestors 'none';" in response.headers["content-security-policy"]
        assert response.headers["x-frame-options"] == "SAMEORIGIN"
