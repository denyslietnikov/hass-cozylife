# CozyLife for Home Assistant

A Home Assistant custom integration for local CozyLife lights and switches.
It communicates directly with devices over TCP port 5555 and does not require
cloud access after the devices have joined your Wi-Fi network.

## Features

- Config flow setup from the Home Assistant UI
- One hub config entry per /24 subnet
- Async TCP client with heartbeat and reconnect
- RGB, color temperature, brightness, effects, and transitions for lights
- Kelvin color temperature API for newer Home Assistant releases
- Single relay and dual-rocker switch support
- Optional Circadian Lighting integration for the `natural` effect

## Installation

### HACS

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=yangqian&repository=hass-cozylife&category=integration)

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

## YAML Migration

Legacy YAML platform configuration is imported into config entries. After the
devices appear under Devices & Services, remove the old YAML and restart Home
Assistant.

For dual-rocker switches, legacy `switches2` entries are imported with two
rocker entities mapped to the shared bitmask register.

## Development

Install dependencies:

```bash
pip install -r requirements.txt
```

Run tests:

```bash
pytest --asyncio-mode=auto tests/
```

Run local checks when the pre-commit environment is installed:

```bash
pre-commit run --all-files
```
