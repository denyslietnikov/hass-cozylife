"""CozyLife integration."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

try:
    import homeassistant.helpers.config_validation as cv
    import voluptuous as vol
    from homeassistant.components.light import LightEntityFeature
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.const import CONF_EFFECT
    from homeassistant.core import HomeAssistant, ServiceCall
    from homeassistant.helpers import service
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
    CONF_LIGHT_INTERVAL,
    CONF_SUBNET,
    CONF_SWITCH_INTERVAL,
    DEFAULT_LIGHT_INTERVAL,
    DEFAULT_SWITCH_INTERVAL,
    DOMAIN,
    LIGHT_TYPE_CODE,
    PLATFORMS,
    SCENES,
    SWITCH_TYPE_CODE,
)
from .runtime import CozyLifeRuntimeData
from .tcp_client import tcp_client

if TYPE_CHECKING:
    from .runtime import CozyLifeConfigEntry

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN) if cv else None

_LOGGER = logging.getLogger(__name__)

_ABSORBED_IDS_KEY = "_absorbed_ids"


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

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        "set_effect",
        entity_domain="light",
        schema={vol.Required(CONF_EFFECT): vol.In(SCENES)},
        func="async_set_effect",
        required_features=[LightEntityFeature.EFFECT],
    )

    async def async_set_all_effect(call: ServiceCall) -> None:
        effect = call.data[CONF_EFFECT]
        for loaded_entry in hass.config_entries.async_loaded_entries(DOMAIN):
            runtime = getattr(loaded_entry, "runtime_data", None)
            if runtime is None:
                continue
            for entity in runtime.light_entities:
                if effect not in entity.effect_list:
                    continue
                await entity.async_set_effect(effect)
                await asyncio.sleep(0.01)

    hass.services.async_register(
        DOMAIN,
        "set_all_effect",
        async_set_all_effect,
        schema=vol.Schema({vol.Required(CONF_EFFECT): vol.In(SCENES)}),
    )

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


async def async_setup_entry(hass: HomeAssistant, entry: CozyLifeConfigEntry) -> bool:
    """Set up a CozyLife hub config entry."""
    hass.data.setdefault(DOMAIN, {})

    absorbed = hass.data[DOMAIN].get(_ABSORBED_IDS_KEY, set())
    devices = entry.data.get(CONF_DEVICES, [])
    if entry.entry_id in absorbed or not devices:
        _LOGGER.info("Skipping absorbed/empty CozyLife entry %s", entry.entry_id)
        hass.async_create_task(hass.config_entries.async_remove(entry.entry_id))
        return True

    clients: dict[str, tcp_client] = {}
    from .coordinator import CozyLifeCoordinator

    coordinators = {}
    for dev in devices:
        client = tcp_client(dev["ip"])
        client._device_id = dev["did"]
        client._expected_device_id = dev["did"]
        client._pid = dev.get("pid", "p93sfg")
        client._dpid = dev.get("dpid", [1])
        client._device_model_name = dev.get("dmn", "CozyLife Device")
        client._device_type_code = dev.get(CONF_DEVICE_TYPE_CODE, LIGHT_TYPE_CODE)
        client.name = dev.get("name")
        # Polling already keeps active connections alive; no second heartbeat loop.
        client._heartbeat_enabled = False
        clients[dev["did"]] = client
        is_switch = client.device_type_code == SWITCH_TYPE_CODE
        interval = entry.options.get(
            CONF_SWITCH_INTERVAL if is_switch else CONF_LIGHT_INTERVAL,
            DEFAULT_SWITCH_INTERVAL if is_switch else DEFAULT_LIGHT_INTERVAL,
        )
        coordinators[dev["did"]] = CozyLifeCoordinator(hass, entry, client, interval)

    entry.runtime_data = runtime = CozyLifeRuntimeData(clients, coordinators, devices)
    try:
        # One offline device must not prevent the rest of the hub from loading.
        await asyncio.gather(
            *(coord.async_refresh() for coord in coordinators.values())
        )
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await runtime.async_shutdown()
        del entry.runtime_data
        raise

    return True


async def async_unload_entry(hass: HomeAssistant, entry: CozyLifeConfigEntry) -> bool:
    """Unload a CozyLife hub config entry."""
    if not hasattr(entry, "runtime_data"):
        return True

    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_shutdown()
        del entry.runtime_data

    return ok
