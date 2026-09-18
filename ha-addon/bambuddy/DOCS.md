# Bambuddy

Bambuddy in Home Assistant's sidebar: print archive, queue, camera, spool
inventory and printer control for Bambu Lab and Snapmaker machines.

## Getting started

1. Start the add-on. **Bambuddy** appears in the sidebar.
2. Open it and add a printer (Printers → Add printer). Discovery scans your
   LAN; a Bambu machine needs LAN-only mode and its access code, a Snapmaker U1
   needs neither.

## Options

### `ha_auth` (default: `true`)

Sign in with the Home Assistant account that opened the panel, rather than a
second Bambuddy login.

The first time a Home Assistant user opens the panel, Bambuddy creates an
account named `ha-<their username>` for them. That name is deliberately
namespaced: a Home Assistant user called `admin` gets `ha-admin`, which is a
different account from a local Bambuddy `admin` and inherits nothing from it.

This only works through the sidebar. Opening Bambuddy directly on its port
shows the ordinary login, because the checks behind this option require the
request to have come from the Supervisor itself — see
`backend/app/services/ha_ingress_auth.py` for what is verified and why.

Turn it off if you want everyone to use Bambuddy's own accounts.

### `ha_auth_role` (default: `user`)

The role those accounts get. `user` joins the Operators group; `admin` grants
Bambuddy's full permissions, including user management and settings.

Every Home Assistant user who opens the panel gets this role — Home Assistant
does not tell the add-on who its administrators are. If that is too much,
leave it at `user` and promote individuals inside Bambuddy.

### `port` (default: `8000`)

The port Bambuddy listens on. The add-on runs on the host network, so this is
also where it answers on your LAN, and it is what the sidebar panel proxies to.

### `log_level` (default: `info`)

`trace` and `debug` are the useful ones when a printer will not connect.

## Access from outside Home Assistant

Because the add-on is on the host network, `http://<your-ha-host>:8000` reaches
Bambuddy directly, bypassing Home Assistant entirely. That is what makes
printer discovery, MQTT, FTPS and the virtual printer work — but it also means
Home Assistant is not guarding that door.

If anyone else can reach that network, enable Bambuddy's own authentication
(Settings → Users) as well.

## Storage

Everything Bambuddy writes — its database, archived 3MFs, thumbnails, logs —
lives in the add-on's `/data`, so it is included in Home Assistant backups and
survives add-on updates.

`/share` is mounted read-write: a file dropped in Home Assistant's `share`
folder shows up in Bambuddy's file manager.

## Updating

The add-on builds Bambuddy from source when it is installed or updated, at the
ref named by the `BAMBUDDY_REF` build argument in its `Dockerfile`. Changing
that argument (to a tag, a branch, or a fork) and reinstalling is how you move
to a different version.

## Troubleshooting

**The panel is blank, or assets fail to load.** Restart the add-on and reload
the page. Bambuddy serves its entry document with a `<base href>` matching the
ingress session; a page left open across an add-on restart holds a stale one.

**The camera tile stays empty in the panel but works on the direct URL.** This
is the ingress proxy dropping the multipart boundary from the response's
content type. Bambuddy sends the boundary in a separate header for exactly
this reason and its player falls back to it — if you see this, the frontend
build is older than the backend. Reinstall the add-on so both are rebuilt
together.

**The camera updates about once a second in the Companion app, or behind a
reverse proxy.** That is the fallback working as intended, not a fault. Bambuddy
reads the MJPEG stream itself (see the note above), which needs the response
body to arrive piece by piece. The web view iOS renders the panel in buffers a
response that never ends, and a reverse proxy with response buffering switched
on does the same — in both cases not one chunk arrives. After six seconds
without a frame Bambuddy drops the stream and polls the single-image endpoint
instead, which is an ordinary request that works everywhere. The picture is
live, just at roughly one frame a second. If you would rather have the smooth
feed on your phone, turn response buffering off for the Home Assistant host in
your proxy (`proxy_buffering off;` in nginx); the iOS web view cannot be talked
out of it.

**Printers are not discovered.** Discovery is SSDP multicast, which needs the
host network. Check that the add-on's `host_network` is still true and that
Home Assistant itself is on the same subnet as the printers.
