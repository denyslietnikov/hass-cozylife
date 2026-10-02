# CozyLife for Home Assistant

A Home Assistant custom integration for local CozyLife lights and switches.
It communicates directly with devices over TCP port 5555 and does not require
cloud access after the devices have joined your Wi-Fi network.

## Features

- Config flow setup from the Home Assistant UI
- One hub config entry per /24 subnet
- Async TCP client with bounded requests, reconnect and device identity checks
- One shared poller per device (5 seconds for switches, 60 seconds for lights)
- RGB, color temperature, brightness, effects, and transitions for lights
- Kelvin color temperature API for newer Home Assistant releases
- Single relay and multi-rocker switch support with atomic bitmask commands
- Hub options for polling, device IP addresses, relay counts and rescanning
- Redacted diagnostics and optional power-on / indicator LED settings
- Optional Circadian Lighting integration for the `natural` effect

## Installation

### HACS

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=denisletnikov&repository=hass-cozylife&category=integration)

Or manually add this repository as a HACS custom integration repository.

### Manual

Copy `custom_components/cozylife` into your Home Assistant
`custom_components` directory and restart Home Assistant.

## Setup

1. Add your CozyLife devices to Wi-Fi using the official app.
2. Assign static IP addresses to the devices in your router.
3. In Home Assistant, go to Settings > Devices & Services > Add Integration.
4. Search for CozyLife.
5. Enter an IP range to scan, for example `192.168.1.1` to `192.168.1.254`.

Devices on the same /24 subnet are grouped under one CozyLife hub entry.

Use the hub's **Configure** menu to change polling intervals, update a device's
IP address or relay count, or scan for additional devices. Rescanning retains
offline devices and existing identities; it updates addresses for matching DIDs.
The known `e5aHVS` model is discovered with two relays. Other models default to
one relay until the correct count is set explicitly.

Switch polling defaults to 5 seconds. Both relays share that single query.
After repeated connection failures, retries back off up to 60 seconds; an
explicit command still attempts the device immediately. A missed acknowledgement
marks the device unavailable rather than reporting a successful switch action.

Power-on state (DPID 18) and indicator LED (DPID 19) entities are disabled by
default. Enable them individually only after confirming the model's behavior;
setup never writes these settings. RGB-only white effects use HS values rather
than unsupported CCT commands. Additional packed/static-color formats remain
unsupported pending hardware validation.

Download integration diagnostics from the hub menu to inspect polling and errors.
Device IDs and IP addresses are redacted. Existing light and rocker `unique_id`
values are unchanged; updating does not require removing the integration.

## YAML Migration

Legacy YAML platform configuration is imported into config entries. After the
devices appear under Devices & Services, remove the old YAML and restart Home
Assistant.

For dual-rocker switches, legacy `switches2` entries are imported with two
rocker entities mapped to the shared bitmask register.

## Development

For the full HA tests, use Python 3.14 and install dependencies:

```bash
python -m pip install -r requirements-test.txt
```

Run tests:

```bash
python -m pytest tests/
```

Run local checks when the pre-commit environment is installed:

```bash
pre-commit run --all-files
```

CI runs the full HA/TCP test suite, Ruff, HACS and hassfest validation. Existing
GitHub scheduled workflows disabled for inactivity must be re-enabled in Actions.
