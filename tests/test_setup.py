"""Config entry lifecycle and migration on HA core."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
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
    coordinator = entry.runtime_data.coordinators[client.device_id]
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
    coordinator = entry.runtime_data.coordinators[client.device_id]
    assert coordinator.update_interval.total_seconds() == 3
    assert client._heartbeat_task is None
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not hasattr(entry, "runtime_data")
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
        DOMAIN,
        "set_effect",
        {"entity_id": entity_id, "effect": "sleep"},
        blocking=True,
    )
    assert hass.states.get(entity_id).attributes["last_effect"] == "sleep"
    await hass.services.async_call(
        DOMAIN, "set_all_effect", {"effect": "warm"}, blocking=True
    )
    assert hass.states.get(entity_id).attributes["last_effect"] == "warm"
    assert all(
        "3" not in request["msg"]["data"]
        for request in device.requests
        if request["cmd"] == 3
    )
    entity = entry.runtime_data.light_entities[0]
    runtime = entry.runtime_data
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entity._transition_task is None
    assert not client.available
    assert runtime.light_entities == []
    assert not hasattr(entry, "runtime_data")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data is not runtime
    assert (
        er.async_get(hass).async_get_entity_id("light", DOMAIN, client.device_id)
        == entity_id
    )
    await hass.services.async_call(
        DOMAIN,
        "set_effect",
        {"entity_id": entity_id, "effect": "study"},
        blocking=True,
    )
    assert hass.states.get(entity_id).attributes["last_effect"] == "study"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_default_light_fade_toggle_and_options_reload(
    hass, entry, mock_device, mocker
):
    device, host, port = mock_device
    device.state["4"] = 400
    metadata = {
        "ip": host,
        "did": device.device_info["did"],
        "pid": "o0mmpn",
        "dmn": "Smart led Strip",
        "device_type_code": "01",
        "dpid": [1, 2, 4, 5, 6],
    }
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "subnet": "127.0.0", "devices": [metadata]},
    )
    client = tcp_client(host)
    client._port = port
    mocker.patch("custom_components.cozylife.tcp_client", return_value=client)
    register_integration(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("light", DOMAIN, client.device_id)
    assert entity_id
    assert entry.runtime_data.light_entities[0]._default_transition == 2

    started = asyncio.get_running_loop().time()
    await hass.services.async_call(
        "light", "toggle", {"entity_id": entity_id}, blocking=True
    )
    assert 2 <= asyncio.get_running_loop().time() - started < 3
    writes = [
        request["msg"]["data"] for request in device.requests if request["cmd"] == 3
    ]
    assert len(writes) > 2
    assert writes[0]["4"] == 0
    assert writes[-1]["4"] == 400
    assert hass.states.get(entity_id).state == "on"
    assert hass.states.get(entity_id).attributes["brightness"] == 102
    device.requests.clear()
    await hass.services.async_call(
        "light", "toggle", {"entity_id": entity_id}, blocking=True
    )
    writes = [
        request["msg"]["data"] for request in device.requests if request["cmd"] == 3
    ]
    assert writes[-2]["4"] == 0
    assert writes[-1] == {"1": 0}
    assert hass.states.get(entity_id).state == "off"
    assert hass.states.get(entity_id).attributes["last_brightness"] == 400

    menu = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "device"}
    )
    await hass.config_entries.options.async_configure(
        menu["flow_id"], {"did": client.device_id}
    )
    await hass.config_entries.options.async_configure(
        menu["flow_id"], {"ip": host, "default_transition": 0.02}
    )
    await hass.async_block_till_done()
    assert registry.async_get_entity_id("light", DOMAIN, client.device_id) == entity_id
    entity = entry.runtime_data.light_entities[0]
    assert entity._default_transition == 0.02
    assert entity.extra_state_attributes["last_brightness"] == 400
    assert not any("13" in write for write in writes)

    device.requests.clear()
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": entity_id}, blocking=True
    )
    assert device.state["4"] == 400
    assert len([request for request in device.requests if request["cmd"] == 3]) >= 2
    device.requests.clear()
    await hass.services.async_call(
        "light", "turn_off", {"entity_id": entity_id, "transition": 0}, blocking=True
    )
    writes = [
        request["msg"]["data"] for request in device.requests if request["cmd"] == 3
    ]
    assert writes == [{"1": 0}]
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("enabled", [False, True])
async def test_native_countdown_service_and_reload(
    hass, entry, mock_device, mocker, enabled
):
    device, host, port = mock_device
    device.device_info.update(pid="o0mmpn", dtp="01")
    device.state["13"] = 0
    metadata = {
        "ip": host,
        "did": device.device_info["did"],
        "pid": "o0mmpn",
        "dmn": "Smart led Strip",
        "device_type_code": "01",
        "dpid": [1, 2, 4, 5, 6, 13],
    }
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "devices": [metadata]}
    )
    original_process = device.process_request

    async def omit_countdown_from_regular_query(request):
        response = await original_process(request)
        if request["cmd"] == 2:
            if request["msg"]["attr"] == [13]:
                response["msg"] = {"attr": [13], "data": {"13": device.state["13"]}}
            else:
                response["msg"]["data"] = {
                    key: value for key, value in device.state.items() if key != "13"
                }
        return response

    mocker.patch.object(
        device, "process_request", side_effect=omit_countdown_from_regular_query
    )
    client = tcp_client(host)
    client._port = port
    mocker.patch("custom_components.cozylife.tcp_client", return_value=client)
    registry = er.async_get(hass)
    if enabled:
        registry.async_get_or_create(
            "number",
            DOMAIN,
            f"{device.device_info['did']}_countdown",
            config_entry=entry,
            suggested_object_id="strip_countdown",
        )
    register_integration(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    number_id = registry.async_get_entity_id(
        "number", DOMAIN, f"{client.device_id}_countdown"
    )
    light_id = registry.async_get_entity_id("light", DOMAIN, client.device_id)
    assert number_id and light_id
    assert not any(request["cmd"] == 3 for request in device.requests)
    coordinator = entry.runtime_data.coordinators[client.device_id]
    await coordinator.async_refresh()

    if not enabled:
        assert (
            registry.async_get(number_id).disabled_by
            == er.RegistryEntryDisabler.INTEGRATION
        )
        assert hass.states.get(number_id) is None
        assert not any(
            request["msg"].get("attr") == [13] for request in device.requests
        )
    else:
        assert registry.async_get(number_id).disabled_by is None
        assert hass.states.get(number_id).state == "0"
        assert hass.states.get(number_id).attributes["unit_of_measurement"] == "s"
        assert coordinator.update_interval.total_seconds() == 60
        original_state = dict(device.state)
        await hass.services.async_call(
            "number", "set_value", {"entity_id": number_id, "value": 60}, blocking=True
        )
        assert device.state == {**original_state, "13": 60}
        assert hass.states.get(number_id).state == "60"
        assert hass.states.get(light_id).state == "off"
        assert hass.states.get(light_id).attributes["last_effect"] == "manual"
        device.state["13"] = 45
        await coordinator.async_refresh()
        assert hass.states.get(number_id).state == "45"
        await hass.services.async_call(
            "number", "set_value", {"entity_id": number_id, "value": 0}, blocking=True
        )
        assert device.state == original_state
        device.requests.clear()
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert (
            registry.async_get_entity_id("light", DOMAIN, client.device_id) == light_id
        )
        assert (
            registry.async_get_entity_id(
                "number", DOMAIN, f"{client.device_id}_countdown"
            )
            == number_id
        )
        assert not any(request["cmd"] == 3 for request in device.requests)

    assert await hass.config_entries.async_unload(entry.entry_id)
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
    coordinator = entry.runtime_data.coordinators[client.device_id]
    assert not coordinator.last_update_success
    assert await async_unload_entry(hass, entry)
    assert coordinator._shutdown_requested


async def test_actions_registered_without_loading_devices(hass):
    from custom_components.cozylife import async_setup

    assert await async_setup(hass, {})
    assert hass.services.has_service(DOMAIN, "set_effect")
    assert hass.services.has_service(DOMAIN, "set_all_effect")
    await hass.services.async_call(
        DOMAIN, "set_all_effect", {"effect": "manual"}, blocking=True
    )


async def test_cancelled_initial_refresh_closes_connection(
    hass, entry, mock_device, mocker
):
    from custom_components.cozylife import async_setup_entry

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
    queried = asyncio.Event()
    original_query = client.query

    async def block_after_query():
        await original_query()
        queried.set()
        await asyncio.Event().wait()

    client.query = AsyncMock(side_effect=block_after_query)
    mocker.patch("custom_components.cozylife.tcp_client", return_value=client)
    forward = mocker.patch.object(hass.config_entries, "async_forward_entry_setups")
    task = asyncio.create_task(async_setup_entry(hass, entry))
    await asyncio.wait_for(queried.wait(), timeout=1)
    runtime = entry.runtime_data
    assert client.available
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not client.available
    assert not hasattr(entry, "runtime_data")
    assert all(coord._shutdown_requested for coord in runtime.coordinators.values())
    forward.assert_not_called()


async def test_platform_setup_failure_cleans_runtime(hass, entry, mocker):
    from custom_components.cozylife import async_setup_entry

    client = tcp_client("192.168.88.18")
    client.query = AsyncMock(return_value={"1": 0})
    client.disconnect = AsyncMock()
    mocker.patch("custom_components.cozylife.tcp_client", return_value=client)
    mocker.patch.object(
        hass.config_entries,
        "async_forward_entry_setups",
        side_effect=RuntimeError("setup failed"),
    )
    with pytest.raises(RuntimeError, match="setup failed"):
        await async_setup_entry(hass, entry)
    client.disconnect.assert_awaited_once()
    assert not hasattr(entry, "runtime_data")


async def test_failed_unload_keeps_runtime(hass, entry, mocker):
    from custom_components.cozylife import async_setup_entry, async_unload_entry

    client = tcp_client("192.168.88.18")
    client.query = AsyncMock(return_value={"1": 0})
    client.disconnect = AsyncMock()
    mocker.patch("custom_components.cozylife.tcp_client", return_value=client)
    mocker.patch.object(hass.config_entries, "async_forward_entry_setups")
    unload = mocker.patch.object(
        hass.config_entries, "async_unload_platforms", return_value=False
    )
    assert await async_setup_entry(hass, entry)
    runtime = entry.runtime_data
    assert not await async_unload_entry(hass, entry)
    assert entry.runtime_data is runtime
    client.disconnect.assert_not_awaited()
    assert not runtime.coordinators[client.device_id]._shutdown_requested
    unload.return_value = True
    assert await async_unload_entry(hass, entry)
    client.disconnect.assert_awaited_once()


@pytest.mark.parametrize("platform", ["light", "switch"])
async def test_legacy_optimistic_warns_without_disabling_polling(
    hass, caplog, platform
):
    from importlib import import_module

    module = import_module(f"custom_components.cozylife.{platform}")
    await module.async_setup_platform(hass, {"optimistic": True}, lambda entities: None)
    assert "Polling remains enabled" in caplog.text
