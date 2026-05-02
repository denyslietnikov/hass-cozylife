"""CozyLife integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

try:
    import homeassistant.helpers.config_validation as cv
    import voluptuous as vol
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.const import CONF_EFFECT
    from homeassistant.core import HomeAssistant, ServiceCall
    from homeassistant.helpers.typing import ConfigType
except ModuleNotFoundError as err:
    if err.name != "homeassistant":
        raise
    cv = None
    vol = None
    ConfigEntry = Any
    HomeAssistant = Any
    ServiceCall = Any
    ConfigType = Any
    CONF_EFFECT = "effect"

from .const import (
    CONF_DEVICE_TYPE_CODE,
    CONF_DEVICES,
    CONF_SUBNET,
    DOMAIN,
    LIGHT_TYPE_CODE,
    PLATFORMS,
)
from .tcp_client import tcp_client

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN) if cv else None

_LOGGER = logging.getLogger(__name__)

LIGHT_ENTITIES_KEY = "light_entities"
_ABSORBED_IDS_KEY = "_absorbed_ids"
_SCENES = ["manual", "natural", "sleep", "warm", "study", "chrismas"]


def _get_subnet(ip: str) -> str:
    """Return the /24 subnet prefix for an IP address."""
    return ".".join(ip.split(".")[:3])


def _device_from_v1_data(data: dict) -> dict:
    """Build a hub device payload from a legacy per-device entry."""
    return {
        "ip": data["ip"],
        "did": data["did"],
        "pid": data.get("pid", "p93sfg"),
        "dmn": data.get("dmn", "CozyLife Device"),
        "dpid": data.get("dpid", [1]),
        CONF_DEVICE_TYPE_CODE: data.get(CONF_DEVICE_TYPE_CODE, LIGHT_TYPE_CODE),
        "rockers": data.get("rockers", 1),
    }


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate legacy per-device entries to one hub entry per /24 subnet."""
    if entry.version >= 2:
        return True

    _LOGGER.info("Migrating CozyLife config entry %s from v1 to v2", entry.entry_id)
    data = dict(entry.data)
    subnet = _get_subnet(data["ip"])
    device = _device_from_v1_data(data)

    existing_hub = None
    for other in hass.config_entries.async_entries(DOMAIN):
        if (
            other.entry_id != entry.entry_id
            and other.version >= 2
            and other.data.get(CONF_SUBNET) == subnet
            and other.data.get(CONF_DEVICES)
        ):
            existing_hub = other
            break

    if existing_hub:
        hub_devices = list(existing_hub.data.get(CONF_DEVICES, []))
        if device["did"] not in {dev["did"] for dev in hub_devices}:
            hub_devices.append(device)
            hass.config_entries.async_update_entry(
                existing_hub,
                data={**existing_hub.data, CONF_DEVICES: hub_devices},
            )

        hass.config_entries.async_update_entry(
            entry,
            version=2,
            data={CONF_SUBNET: subnet, CONF_DEVICES: []},
            unique_id=f"_absorbed_{data['did']}",
        )
        _LOGGER.info(
            "Merged device %s into hub for %s; entry %s marked absorbed",
            data["did"],
            subnet,
            entry.entry_id,
        )
    else:
        hass.config_entries.async_update_entry(
            entry,
            data={
                CONF_SUBNET: subnet,
                "start_ip": f"{subnet}.1",
                "end_ip": f"{subnet}.254",
                CONF_DEVICES: [device],
            },
            unique_id=subnet,
            title=f"CozyLife Hub ({subnet}.0/24)",
            version=2,
        )
        _LOGGER.info("Entry %s is now the hub for subnet %s", entry.entry_id, subnet)

    return True


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the CozyLife integration."""
    hass.data.setdefault(DOMAIN, {})

    entries = hass.config_entries.async_entries(DOMAIN)
    by_subnet: dict[str, list[ConfigEntry]] = {}
    for entry in entries:
        subnet = entry.data.get(CONF_SUBNET)
        if subnet and entry.data.get(CONF_DEVICES):
            by_subnet.setdefault(subnet, []).append(entry)

    absorbed_ids: set[str] = set()
    for subnet, group in by_subnet.items():
        if len(group) <= 1:
            continue

        _LOGGER.info("Consolidating %d CozyLife entries for %s", len(group), subnet)
        primary = group[0]
        merged_devices = list(primary.data.get(CONF_DEVICES, []))
        seen_dids = {dev["did"] for dev in merged_devices}

        for extra in group[1:]:
            for dev in extra.data.get(CONF_DEVICES, []):
                if dev["did"] not in seen_dids:
                    merged_devices.append(dev)
                    seen_dids.add(dev["did"])
            absorbed_ids.add(extra.entry_id)

        hass.config_entries.async_update_entry(
            primary,
            data={**primary.data, CONF_DEVICES: merged_devices},
        )

    for entry in entries:
        if entry.data.get(CONF_SUBNET) and not entry.data.get(CONF_DEVICES):
            absorbed_ids.add(entry.entry_id)

    hass.data[DOMAIN][_ABSORBED_IDS_KEY] = absorbed_ids

    for entry_id in absorbed_ids:
        _LOGGER.info("Removing absorbed CozyLife config entry %s", entry_id)
        await hass.config_entries.async_remove(entry_id)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a CozyLife hub config entry."""
    hass.data.setdefault(DOMAIN, {})

    absorbed = hass.data[DOMAIN].get(_ABSORBED_IDS_KEY, set())
    devices = entry.data.get(CONF_DEVICES, [])
    if entry.entry_id in absorbed or not devices:
        _LOGGER.info("Skipping absorbed/empty CozyLife entry %s", entry.entry_id)
        hass.async_create_task(hass.config_entries.async_remove(entry.entry_id))
        return True

    clients: dict[str, tcp_client] = {}
    for dev in devices:
        client = tcp_client(dev["ip"])
        client._device_id = dev["did"]
        client._pid = dev.get("pid", "p93sfg")
        client._dpid = dev.get("dpid", [1])
        client._device_model_name = dev.get("dmn", "CozyLife Device")
        client._device_type_code = dev.get(CONF_DEVICE_TYPE_CODE, LIGHT_TYPE_CODE)
        await client._connect()
        clients[dev["did"]] = client

    hass.data[DOMAIN][entry.entry_id] = {
        "clients": clients,
        "devices": devices,
        LIGHT_ENTITIES_KEY: [],
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, "set_all_effect"):

        async def async_set_all_effect(call: ServiceCall) -> None:
            effect = call.data.get(CONF_EFFECT)
            entries_data = hass.data.get(DOMAIN, {})
            for entry_data in entries_data.values():
                if not isinstance(entry_data, dict):
                    continue
                for entity in entry_data.get(LIGHT_ENTITIES_KEY, []):
                    await entity.async_set_effect(effect)
                    await asyncio.sleep(0.01)

        hass.services.async_register(
            DOMAIN,
            "set_all_effect",
            async_set_all_effect,
            schema=vol.Schema({vol.Required(CONF_EFFECT): vol.In(_SCENES)}),
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a CozyLife hub config entry."""
    if entry.entry_id not in hass.data.get(DOMAIN, {}):
        return True

    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if entry_data:
            for client in entry_data.get("clients", {}).values():
                await client.disconnect()

    return ok
