# Eaton / Scantronic Local BMS for Home Assistant

![i-on BMS integration logo](custom_components/eaton_bms/brand/logo.png)

A fully local Home Assistant custom integration for Eaton / Scantronic i-on
alarm panels exposing the BMS API. No Control4 controller, cloud account, MQTT
broker or additional alarm hardware is required.

## Features

- Live PIR/zone feedback, including wired expanders, while the alarm is disarmed.
- Partition status and explicit PIN-required Full set, Part set and Unset actions.
- Configurable Home/Night mappings to the panel's existing part sets.
- Motion, door, window or opening sensor classes for zones; optional tamper diagnostics.
- Panel faults, active alarms, full-set readiness and blocked-arm reasons.
- Open-zone notices are kept separate from equipment faults.
- Read-only programmable output status; it is not a physical door/lock sensor.
- Connection diagnostics, automatic discovery and reconnect/resynchronization.
- Exit countdown updated locally without additional panel requests.
- Last-alarm details retained through clearance and HA restarts.
- Triggering zone only when explicitly reported by the alarm payload.
- Optional room-occupancy helper example in `examples/`.

Independently developed; not an official Eaton or Control4 product. MIT licensed.

## Installation

Requires Home Assistant 2026.10.1 or later (the currently tested baseline).

### HACS custom repository

Default HACS store inclusion is subject to maintainer review. The repository can
be installed as a custom repository before it is listed:

1. In HACS, open the menu and select **Custom repositories**.
2. Add `https://github.com/a-mavrides/eaton-bms-ha` with category **Integration**.
3. Download **Eaton / Scantronic Local BMS** and restart Home Assistant.
4. Open **Settings → Devices & services → Add integration** and search for
   **Eaton / Scantronic Local BMS**.

### Manual installation

Download the repository/release source ZIP. Copy `custom_components/eaton_bms/`
into your HA configuration directory, including its `brand/` and `translations/`
folders. The manifest should be at `/config/custom_components/eaton_bms/manifest.json`.
Restart HA and add the integration through Devices & services. No YAML is required.

When upgrading a previous manual installation, replace the integration files and
restart. Keep the existing integration entry; credentials, trusted certificate
and entity identities remain in Home Assistant.

## Setup and certificates

Leave installer mode. Enter your panel address, HTTPS port (normally `4433`),
**BMS PIN** and **BMS password**. Setup discovers the panel's TLS certificate
without transmitting credentials. Compare the displayed fingerprint against the
certificate in your working local panel browser and confirm once.

The accepted fingerprint is stored internally and checked on subsequent TLS
connections before credentials are sent. There is no customer-specific IP or
certificate in this repository. Use **Reconfigure** to change the connection or
confirm a replacement certificate. Disable the integration temporarily while
making installer changes to the panel.

## Alarm controls

Each arm/disarm action requires your **normal alarm user PIN**. The BMS PIN/password
are for setup and monitoring. The integration does not save the normal user PIN;
BMS credentials are stored in HA's integration configuration.

| Home Assistant action | Panel command |
| --- | --- |
| Away | Full set (`fullset`) |
| Home | Part set B (`partset-b`), default mapping |
| Night | Part set C (`partset-c`), default mapping |
| Disarm | Unset (`unset`) |

Use **Configure** to map Home/Night to `partset-b`, `partset-c`, `partset-d`, or
`partset`. The panel determines zone membership in those modes; the integration
never reprograms it. Commands are not retried automatically or used to force/bypass
faults. State changes come from panel feedback, not an assumed command success.

Phone notifications require a separate HA automation watching the alarm entity's
`triggered` state. No notifications or control automations are created automatically.

## Entities and retained details

- Zones default to motion sensors. Change their display type through Configure;
  their unique identity stays the same. New zones are discovered during refreshes.
- Zone tamper and individual equipment-fault entities are disabled by default.
- **Ready for full set** is derived from live zone readings and blocking panel
  faults, with `blocking_reasons` attributes. It applies to full set only and is
  not a guarantee the panel will accept a command.
- **Panel faults**, **Fault count**, and **Active alarm count** expose reported
  information. Mains/battery faults use the panel's own descriptions; undocumented
  fault codes are not guessed. Type 5000 open-zone notices are not equipment faults.
- **Exit countdown** needs panel-reported `time-to-exit`. Countdown reaching zero
  does not mark the alarm armed; the alarm entity waits for panel feedback.
- **Last alarm** records when HA first observed the episode, reported type and
  explicit zone references. It persists locally after clearance/restart. It is
  not a reconstruction of the panel's historical log; events during downtime may
  be missed.
- **Triggering zone** displays `Not reported` during an alarm without an explicit
  zone reference. The integration never guesses a cause from PIR activity.
- Connection status remains visible when disconnected. Frequently changing
  **Last event**, **Last heartbeat**, and **Last status sync** timestamps are disabled
  by default for new entities. Existing enabled entities retain their user settings;
  disable these manually if you do not need their history.

Motion detects movement, not continuous occupancy. See `examples/README.md` for
an optional occupancy helper that holds activity for a configurable time and
becomes unavailable when its source is unavailable. Raw PIR states remain unchanged.

## Local operation and performance

The client maintains one SSE event stream, serializes JSON requests, and reconciles
status independently every 60 seconds. System metadata is cached after its first
read. Reconnection uses backoff from 2 to 60 seconds and a fresh snapshot. A silent
stream is detected by a 180-second read timeout; polling can detect failure sooner.
Known PIR transitions do not trigger extra panel queries. Countdown ticks are local.

Entities become unavailable when the event stream disconnects. Unknown readings
remain unknown, and unsupported `/faults/` or `/alarms/` routes (404/405) do not
prevent zone monitoring. Disabling/removing the integration cancels its background
stream, reconciliation and active countdown tasks.

## Verification and limitations

Local BMS discovery and disarmed PIR/restore events have been physically confirmed
on an **i-on40H+ running firmware 8.00.34**, with 10 zones including an upstairs
expander. Other panels/firmware require compatibility testing.

**28 tests pass** with Home Assistant **2026.10.1** and Python **3.14.8** using native
HA setup/services and a loopback HTTPS simulator. Tests cover authentication and
certificate trust, PIR feedback, reconnect, discovery, commands, fault classification,
readiness, persistent last-alarm records, countdowns without added requests,
occupancy-helper availability and concurrency during persistence.

Physical arm/disarm, countdown and triggering-zone payload verification are still
pending. These features follow the recovered protocol and are simulator-tested;
this does not imply verification on every panel. No output control, zone bypass
or complete historical log is implemented.

## Privacy, development and provenance

Share this repository/source package, not your HA configuration/backups/storage:
those contain BMS credentials and may contain retained alarm zone names.
The repository has no real credentials, customer address/fingerprint, captured
user traffic, Control4 source, encryption keys or installer binaries. Tests use
only dummy credentials, localhost and temporary generated certificates.

Install `requirements-test.txt` in a separate Python 3.14 environment and run
`python -m pytest --tb=short` from the repository root. Tests do not contact a
physical panel. GitHub workflows run tests, Hassfest and HACS validation.

Independent Python implementation based on the local BMS protocol used by official
Scantronic Control4 drivers v100 (2019-10-04):

- https://drivers.control4.com/securitypanel_ip_scantronic_ion.c4z
- https://drivers.control4.com/comms_ip_scantronic_ion_sse.c4z

Reads: `/system/`, `/system/partitions/`, `/system/partitions-N/zones/`, `/outputs/`,
`/faults/`, `/alarms/`. Events: `/events/` (SSE). Commands: JSON PUT to
`/system/partitions-N/`, using the supplied user PIN with the BMS password.
No installer/web login, `ssid` session or keypad simulation is used.

Report issues at https://github.com/a-mavrides/eaton-bms-ha/issues with your panel
model, firmware, HA version and sanitized logs. Never post credentials or raw HAR files.
