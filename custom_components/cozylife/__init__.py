"""CozyLife integration."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

try:
    import homeassistant.helpers.config_validation as cv
    import voluptuous as vol
    from homeassistant.components.light import LightEntityFeature
    from homeassistant.config_entries import ConfigEntry, ConfigEntryState
    from homeassistant.const import CONF_EFFECT
    from homeassistant.core import HomeAssistant, ServiceCall
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
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
_MERGE_RELOADS_KEY = "_merge_reloads"
EFFECT_CONCURRENCY = 4


def _async_transfer_registries(hass, source: ConfigEntry, target: ConfigEntry) -> None:
    """Move ownership before removing an absorbed entry, without yielding."""
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    device_ids = {}
    to_move = []
    for device in list(
        dr.async_entries_for_config_entry(device_registry, source.entry_id)
    ):
        existing = next(
            (
                match
                for identifier in device.identifiers
                if (
                    match := device_registry.async_get_device_by_identifier(
                        identifier, target.entry_id
                    )
                )
                is not None
            ),
            None,
        )
        if existing is not None:
            device_ids[device.id] = existing.id
        else:
            to_move.append(device.id)
            device_ids[device.id] = device.id
    # Registry event callbacks run immediately. Move entities first, otherwise
    # the device's ownership-change event deletes its old owner's entities.
    for entity in list(
        er.async_entries_for_config_entry(entity_registry, source.entry_id)
    ):
        entity_registry.async_update_entity(
            entity.entity_id,
            config_entry_id=target.entry_id,
            device_id=device_ids.get(entity.device_id, entity.device_id),
        )
    for device_id in to_move:
        device_registry.async_update_device(
            device_id, new_config_entry_id=target.entry_id
        )


def _async_reload_merged_hub(hass, entry: ConfigEntry) -> None:
    """Coalesce merges, including ones arriving while hub setup is awaiting I/O."""
    if entry.state is not ConfigEntryState.LOADED:
        return
    pending = hass.data.setdefault(DOMAIN, {}).setdefault(_MERGE_RELOADS_KEY, set())
    if entry.entry_id in pending:
        return
    pending.add(entry.entry_id)

    async def reload() -> None:
        try:
            while await hass.config_entries.async_reload(entry.entry_id):
                runtime = getattr(entry, "runtime_data", None)
                if runtime is None or runtime.devices == entry.data[CONF_DEVICES]:
                    break
        finally:
            pending.discard(entry.entry_id)

    hass.async_create_task(reload())


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

        _async_transfer_registries(hass, entry, existing_hub)

        hass.config_entries.async_update_entry(
            entry,
            version=2,
            data={CONF_SUBNET: subnet, CONF_DEVICES: []},
            unique_id=f"_absorbed_{data['did']}",
        )
        _async_reload_merged_hub(hass, existing_hub)
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
        entities = []
        for loaded_entry in hass.config_entries.async_loaded_entries(DOMAIN):
            runtime = getattr(loaded_entry, "runtime_data", None)
            if runtime is None:
                continue
            for entity in runtime.light_entities:
                if effect not in entity.effect_list:
                    continue
                entities.append(entity)
        semaphore = asyncio.Semaphore(EFFECT_CONCURRENCY)

        async def apply_effect(entity) -> None:
            async with semaphore:
                await entity.async_set_effect(effect)

        # Each light owns its task: superseding one fade must not cancel the
        # service or the other lights. Cancelling the service cancels them all.
        results = await asyncio.gather(
            *(apply_effect(entity) for entity in entities), return_exceptions=True
        )
        failed = []
        for entity, result in zip(entities, results):
            if isinstance(result, Exception):
                name = entity.entity_id or entity.unique_id
                failed.append(name)
                _LOGGER.warning("Could not set effect on %s: %s", name, result)
        if failed:
            raise HomeAssistantError(
                f"Could not set CozyLife effect on {len(failed)} light(s): "
                + ", ".join(failed)
            )

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
            _async_transfer_registries(hass, extra, primary)
            absorbed_ids.add(extra.entry_id)

        hass.config_entries.async_update_entry(
            primary,
            data={**primary.data, CONF_DEVICES: merged_devices},
        )
        _async_reload_merged_hub(hass, primary)

    for entry in entries:
        if entry.data.get(CONF_SUBNET) and not entry.data.get(CONF_DEVICES):
            if group := by_subnet.get(entry.data[CONF_SUBNET]):
                _async_transfer_registries(hass, entry, group[0])
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

    while True:
        devices = list(entry.data[CONF_DEVICES])
        await _async_setup_devices(hass, entry, devices)
        if devices == entry.data[CONF_DEVICES]:
            return True
        # Another v1 entry may merge while initial polling/platform setup awaits.
        # Finish with all devices before HA marks this entry loaded, rather than
        # racing the outer setup with a scheduled config-entry reload.
        if not await async_unload_entry(hass, entry):
            raise HomeAssistantError("Unable to unload CozyLife platforms after merge")


async def _async_setup_devices(
    hass, entry: CozyLifeConfigEntry, devices: list[dict]
) -> None:
    """Set up one consistent snapshot of a hub's devices."""
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


async def async_unload_entry(hass: HomeAssistant, entry: CozyLifeConfigEntry) -> bool:
    """Unload a CozyLife hub config entry."""
    if not hasattr(entry, "runtime_data"):
        return True

    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_shutdown()
        del entry.runtime_data

    return ok
