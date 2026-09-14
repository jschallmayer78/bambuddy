# Snapmaker U1 support

Bambuddy can manage a Snapmaker U1 alongside Bambu Lab printers. The U1 does
not speak Bambu's MQTT protocol: its stock firmware is a Moonraker fork on top
of Klipper, served over plain HTTP. This page covers how to add one, what
works, what does not, and the reasoning where a decision is not obvious.

## Adding a U1

1. **Printers → Add printer**, set **Printer type** to *Snapmaker U1*.
2. Enter the printer's IP address. If its Moonraker listens somewhere other
   than port 80, append the port (`192.168.1.9:7125`).
3. Leave **Access code** empty. The U1 has none — Moonraker answers every
   request on a private LAN without a key or token. The field stays available
   for the rare installation that has configured a Moonraker API token.
4. The serial number is only a label for a U1. Leave it blank and Bambuddy
   derives one from the address (`u1-192-168-1-9`), or from the printer's own
   serial when it was found by a network scan.

A subnet scan finds U1s too: a host that does not answer Bambu's ports is
asked for `/machine/system_info`, and a machine that reports Snapmaker's
`product_info` block is offered as a Snapmaker U1. Vanilla Moonraker printers
(a Voron, a converted Ender) answer that endpoint without the block and are
deliberately skipped — Bambuddy's U1 driver leans on U1-only firmware macros,
so adding an unrelated Klipper machine would produce a half-working card.

## What works

| Area | Notes |
|------|-------|
| Status | State, progress, layer, temperatures, part-fan speed, print speed, active toolhead. |
| Filament slots | The four toolheads appear as a filament unit with four slots — colour, material and which one is active. Read from the touchscreen-assigned `print_task_config`, so third-party spools show up too, not just RFID-tagged Snapmaker ones. |
| Slot colours | Can be written back to the printer (idle only, and never over an official RFID spool's colour, which belongs to its tag). |
| Camera | Live view and snapshots, and everything that builds on them — finish photos, layer timelapse, plate detection, Obico. No configuration: the built-in camera is used automatically. |
| Control | Pause, resume, cancel, emergency stop, nozzle and bed temperature, part fan, print speed, homing, jogging, filament unload, raw G-code. |
| Exclude object | Klipper's `exclude_object`, so "skip objects" works during a print. |
| Files | Browse, upload, download, delete, thumbnails and free space through Moonraker's file API. |
| Printing | Queue dispatch uploads the file and starts it, applying the job's toolhead mapping and per-print options (auto-level, flow calibration, timelapse). |
| Diagnostics | Three U1 checks: the Moonraker API answers, the machine identifies as a Snapmaker, and Klipper is ready. |

### Print files must be G-code

Klipper executes G-code. A 3MF is a slicer project, and a U1 will list one it
was given and then fail to open it, so Bambuddy refuses the upload with that
explanation rather than letting the job fail on the printer. Slice for the U1
and queue the resulting `.gcode`.

### Remaining time is estimated, not reported

Klipper publishes elapsed print duration and file progress but no ETA. The
figure shown is the file-progress estimate (the same one Mainsail and Fluidd
show). For the first two percent — where that estimate swings by hours between
polls — the sliced file's own estimate is used instead when its metadata
carries one.

## What is not supported, and why

Everything below is Bambu-specific hardware or protocol with no U1
counterpart. These controls are hidden on a U1's card, and the API answers
`501 Not Implemented` naming the operation rather than failing with a 500:

- **AMS** — slot configuration, filament backup, loading/unloading through an
  AMS, drying. The U1 has four direct-drive toolheads and no dryer.
- **K-profiles / flow calibration profiles** — a Bambu firmware feature. The
  U1's own flow calibration is available as a per-print option instead.
- **HMS codes** — the U1 reports its own structured error codes, which
  Bambuddy surfaces in the same place; Bambu's HMS catalogue and its guided
  actions do not apply.
- **Chamber heater, airduct, chamber and auxiliary fans, chamber light.**
- **Calibration stages** (Bambu's `stg_cur` progress display).
- **MQTT debug log** — there is no MQTT session to log.
- **Virtual printer / Bambu Studio dispatch** — unchanged, Bambu-only.

## How it is wired, for anyone reading the code

- `backend/app/services/printer_drivers/` — the driver seam. `base.py` holds
  the printer-type constants, `UnsupportedOperation` and the await-tolerant
  `call_driver`; `factory.py` maps a type to a driver.
- `backend/app/services/printer_drivers/snapmaker_u1.py` — the U1 driver. It
  polls (2 s idle, 1 s while printing) rather than subscribing: a WebSocket
  subscription is cheaper but its failure mode is a silent stall, where the
  printer looks frozen instead of disconnected.
- `backend/app/services/snapmaker/` — the protocol itself: `moonraker.py`
  (HTTP + WebSocket transport), `status.py` (payload → `PrinterState`),
  `camera.py` (the `start_monitor` keepalive).
- `Printer.printer_type` is `NULL` on every row written before this existed,
  and every one of those is a Bambu machine, so `NULL` reads as `bambu`
  everywhere rather than being backfilled.

### The camera needs a keepalive

The U1 serves no video stream. Its camera plugin writes a single JPEG to
`/server/files/camera/monitor.jpg` and only keeps refreshing it while
something asks — `camera.start_monitor` over the WebSocket. So the live view
is a poll loop with a keepalive attached, and three of its rules come from how
the plugin actually behaves: `start_monitor` must not be hammered, a cold
start has no file for about a second, and `monitor.jpg` briefly 404s while it
is rewritten (a retry, not an error).

If you run community firmware that provides a real stream, configure it as an
external camera on the printer — an explicitly configured camera always wins
over the built-in one.
