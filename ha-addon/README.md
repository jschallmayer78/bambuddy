# Bambuddy as a Home Assistant add-on

Adds Bambuddy to Home Assistant's sidebar, using the Home Assistant login you
already have.

## Install

1. Settings → Add-ons → Add-on Store → ⋮ → **Repositories**
2. Add this repository's URL.
3. Install **Bambuddy** from the store, then **Start**.

Bambuddy appears in the sidebar. Everything else — printers, queue, camera —
works exactly as it does outside Home Assistant.

## Options

| Option | Default | What it does |
|--------|---------|--------------|
| `ha_auth` | `true` | Sign in with the Home Assistant account that opened the panel, instead of a second Bambuddy login. |
| `ha_auth_role` | `user` | Role given to accounts created that way. `admin` grants Bambuddy's full permissions. |
| `port` | `8000` | The port Bambuddy listens on. The add-on runs on the host network, so this is also the port for direct access from your LAN. |
| `log_level` | `info` | Bambuddy's log level. |

`ha_auth` maps a Home Assistant user to a Bambuddy account named
`ha-<username>`, which is separate from any local Bambuddy account of the same
name. It only works through the sidebar panel — see
`backend/app/services/ha_ingress_auth.py` for the checks and why each exists.

## Why host network

Printer discovery (SSDP), Bambu's MQTT and FTPS, the camera protocols and the
virtual printer all need to reach — and be reachable on — your LAN directly.
A bridged add-on network would break discovery and the virtual printer.

The practical consequence: Bambuddy's own port is open on your LAN, not only
inside Home Assistant. If you rely on Home Assistant for access control, turn
Bambuddy's own authentication on as well (Settings → Users in Bambuddy).

## Building the image

The add-on builds from source at a git ref, set by the `BAMBUDDY_REF` build
argument in `ha-addon/bambuddy/Dockerfile` (default: this repository's default
branch). To run a fork or a branch, change that argument, or use
`ha-addon/prepare-local-addon.sh` to vendor a working tree into a local add-on
under `/addons`.
