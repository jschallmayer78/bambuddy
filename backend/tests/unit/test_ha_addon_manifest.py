"""The add-on manifest has to agree with the application it ships.

Home Assistant decides whether an add-on has an update by comparing this
version string, and the convention here is Bambuddy's own version plus an
add-on revision. A stale value means the Supervisor offers no update after a
release — the failure is silent and only shows up as "why is my add-on still
on the old version".
"""

import re
from pathlib import Path

import pytest

from backend.app.core.config import APP_VERSION

ADDON_DIR = Path(__file__).resolve().parents[3] / "ha-addon" / "bambuddy"
CONFIG = ADDON_DIR / "config.yaml"


def _field(name: str) -> str:
    match = re.search(rf'^{name}:\s*"?([^"\n]+)"?\s*$', CONFIG.read_text(), re.MULTILINE)
    assert match, f"{name} missing from {CONFIG}"
    return match.group(1).strip()


@pytest.mark.skipif(not CONFIG.exists(), reason="add-on manifest not present in this checkout")
def test_the_addon_version_tracks_the_app_version():
    version = _field("version")
    assert version.startswith(f"{APP_VERSION}-"), (
        f"add-on version {version!r} does not match APP_VERSION {APP_VERSION!r}; "
        "it must be <app version>-<add-on revision>"
    )
    revision = version[len(APP_VERSION) + 1 :]
    assert revision.isdigit(), f"add-on revision {revision!r} should be a plain number"


@pytest.mark.skipif(not CONFIG.exists(), reason="add-on manifest not present in this checkout")
def test_the_panel_is_wired_to_the_apps_own_port():
    """The add-on is on the host network, so ingress has to point at the one
    listener Bambuddy actually has — there is no separate internal port."""
    assert _field("host_network") == "true"
    assert _field("ingress") == "true"
    # options.port and ingress_port are the same listener; keep them in step.
    options_port = re.search(r"^options:\n(?:.*\n)*?  port:\s*(\d+)", CONFIG.read_text(), re.MULTILINE)
    assert options_port, "options.port missing"
    assert _field("ingress_port") == options_port.group(1)


@pytest.mark.skipif(not CONFIG.exists(), reason="add-on manifest not present in this checkout")
def test_the_entrypoint_passes_the_options_through():
    """run.sh is what turns the user's options into Bambuddy's environment."""
    run_sh = (ADDON_DIR / "run.sh").read_text()
    for variable in ("BAMBUDDY_HA_INGRESS_AUTH", "BAMBUDDY_HA_INGRESS_ROLE", "DATA_DIR"):
        assert variable in run_sh, f"{variable} is never exported by run.sh"
    # Without exec the Supervisor's SIGTERM never reaches uvicorn.
    assert re.search(r"^exec uvicorn", run_sh, re.MULTILINE), "run.sh must exec uvicorn, not spawn it"
