"""Config entry lifecycle and migration on HA core."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

from homeassistant import loader
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er

from custom_components.cozylife import async_migrate_entry
from custom_components.cozylife.const import DOMAIN
from custom_components.cozylife.tcp_client import tcp_client


def register_integration(hass):
    path = Path(__file__).resolve().parents[1] / "custom_components" / DOMAIN
    manifest = json.loads((path / "manifest.json").read_text())
    hass.data[loader.DATA_CUSTOM_COMPONENTS] = {
        DOMAIN: loader.Integration(
            hass,
            f"custom_components.{DOMAIN}",
            path,
            manifest,
            {file.name for file in path.iterdir() if file.is_file()},
        )
    }


async def test_hub_loads_real_entities_and_unloads(hass, entry, mock_device, mocker):
    device, host, port = mock_device
    metadata = {
        **entry.data["devices"][0],
        "did": device.device_info["did"],
        "ip": host,
    }
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "devices": [metadata]}
    )
    client = tcp_client(host)
    client._port = port
    mocker.patch("custom_components.cozylife.tcp_client", return_value=client)
    register_integration(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state == ConfigEntryState.LOADED
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "switch", DOMAIN, f"{device.device_info['did']}_wippe1"
    )
    sibling_id = registry.async_get_entity_id(
        "switch", DOMAIN, f"{device.device_info['did']}_wippe2"
    )
    assert entity_id and sibling_id
    assert hass.states.get(entity_id).state == "off"
    assert hass.states.get(sibling_id).state == "off"
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinators"][client.device_id]
    device.state["1"] = 3
    await coordinator.async_refresh()
    assert hass.states.get(entity_id).state == "on"
    assert hass.states.get(sibling_id).state == "on"
    assert len([request for request in device.requests if request["cmd"] == 2]) == 2
    changed = asyncio.Event()
    remove_listener = coordinator.async_add_listener(changed.set)
    device.state["1"] = 0
    await asyncio.wait_for(changed.wait(), timeout=6.5)
    remove_listener()
    assert hass.states.get(entity_id).state == "off"
    assert hass.states.get(sibling_id).state == "off"
    assert len([request for request in device.requests if request["cmd"] == 2]) == 3
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": sibling_id}, blocking=True
    )
    assert device.state["1"] == 2
    assert hass.states.get(entity_id).state == "off"
    assert hass.states.get(sibling_id).state == "on"
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "settings"}
    )
    assert form["step_id"] == "settings"
    await hass.config_entries.options.async_configure(
        menu["flow_id"], {"switch_interval": 3, "light_interval": 60}
    )
    await hass.async_block_till_done()
    assert entry.state == ConfigEntryState.LOADED
    assert (
        registry.async_get_entity_id(
            "switch", DOMAIN, f"{device.device_info['did']}_wippe1"
        )
        == entity_id
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinators"][client.device_id]
    assert coordinator.update_interval.total_seconds() == 3
    assert client._heartbeat_task is None
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.entry_id not in hass.data[DOMAIN]
    assert not client.available
    assert coordinator._shutdown_requested


async def test_rgb_light_loads_effects_and_cleans_up(hass, entry, mock_device, mocker):
    device, host, port = mock_device
    device.device_info.update(pid="o0mmpn", dtp="01")
    metadata = {
        "ip": host,
        "did": device.device_info["did"],
        "pid": "o0mmpn",
        "dmn": "Smart led Strip",
        "device_type_code": "01",
        "dpid": [1, 2, 4, 5, 6, 7, 8],
    }
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "devices": [metadata]}
    )
    client = tcp_client(host)
    client._port = port
    mocker.patch("custom_components.cozylife.tcp_client", return_value=client)
    register_integration(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id(
        "light", DOMAIN, client.device_id
    )
    assert entity_id
    assert hass.states.get(entity_id).state == "off"
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": entity_id, "brightness": 88, "hs_color": [240, 80]},
        blocking=True,
    )
    assert hass.states.get(entity_id).state == "on"
    assert hass.states.get(entity_id).attributes["brightness"] == 88
    await hass.services.async_call(
        DOMAIN, "set_all_effect", {"effect": "warm"}, blocking=True
    )
    assert hass.states.get(entity_id).attributes["last_effect"] == "warm"
    assert all(
        "3" not in request["msg"]["data"]
        for request in device.requests
        if request["cmd"] == 3
    )
    entity = hass.data[DOMAIN][entry.entry_id]["light_entities"][0]
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entity._transition_task is None
    assert not client.available


async def test_single_legacy_entry_keeps_device_identity(hass, entry):
    device = dict(entry.data["devices"][0])
    hass.config_entries.async_update_entry(entry, version=1, data=device)
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 2
    assert entry.data["devices"][0]["did"] == device["did"]
    assert entry.data["devices"][0]["rockers"] == 2
    assert entry.unique_id == "192.168.88"


async def test_offline_device_does_not_block_hub(hass, entry, mocker):
    from custom_components.cozylife import async_setup_entry, async_unload_entry

    client = tcp_client("192.168.88.18")
    client.query = AsyncMock(return_value=None)
    mocker.patch("custom_components.cozylife.tcp_client", return_value=client)
    mocker.patch.object(
        hass.config_entries, "async_forward_entry_setups", return_value=None
    )
    mocker.patch.object(
        hass.config_entries, "async_unload_platforms", return_value=True
    )
    assert await async_setup_entry(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinators"][client.device_id]
    assert not coordinator.last_update_success
    assert await async_unload_entry(hass, entry)
    assert coordinator._shutdown_requested
