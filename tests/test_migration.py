"""Hub consolidation must preserve registry identity and load every device."""

from types import MappingProxyType
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.cozylife.tcp_client import tcp_client
from tests.test_setup import register_integration


@pytest.mark.parametrize("mode", ["loaded_hub", "legacy_startup", "duplicate_hubs"])
async def test_merge_preserves_registries_and_loads_devices(hass, entry, mocker, mode):
    register_integration(hass)
    mocker.patch.object(tcp_client, "query", new=AsyncMock(return_value={"1": 0}))
    first = dict(entry.data["devices"][0])
    second = {**first, "did": "second_device", "ip": "192.168.88.19"}
    if mode == "loaded_hub":
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    elif mode == "legacy_startup":
        hass.config_entries.async_update_entry(entry, version=1, data=first)
    extra = ConfigEntry(
        domain="cozylife",
        title="Legacy",
        version=2 if mode == "duplicate_hubs" else 1,
        minor_version=1,
        unique_id="second_device",
        source="user",
        options={},
        discovery_keys=MappingProxyType({}),
        subentries_data=[],
        data=(
            {**entry.data, "devices": [second]} if mode == "duplicate_hubs" else second
        ),
    )
    hass.config_entries._entries[extra.entry_id] = extra
    devices = dr.async_get(hass)
    original_device = devices.async_get_or_create(
        config_entry_id=extra.entry_id,
        identifiers={("cozylife", "second_device")},
        name="Original device",
    )
    devices.async_update_device(original_device.id, name_by_user="My switch")
    registry = er.async_get(hass)
    original_entity = registry.async_get_or_create(
        "switch",
        "cozylife",
        "second_device_wippe1",
        config_entry=extra,
        device_id=original_device.id,
        suggested_object_id="custom_relay",
    )
    registry.async_update_entity(
        original_entity.entity_id, name="My relay", icon="mdi:lamp"
    )
    disabled = registry.async_get_or_create(
        "switch",
        "cozylife",
        "second_device_wippe2",
        config_entry=extra,
        device_id=original_device.id,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    removed = []
    hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        lambda event: (
            removed.append(event.data["entity_id"])
            if event.data["action"] == "remove"
            else None
        ),
    )
    try:
        assert await hass.config_entries.async_setup(
            extra.entry_id if mode == "loaded_hub" else entry.entry_id
        )
        await hass.async_block_till_done()
        assert original_entity.entity_id not in removed
        assert hass.config_entries.async_get_entry(extra.entry_id) is None
        actual = registry.async_get(original_entity.entity_id)
        assert actual is not None
        assert actual.config_entry_id == entry.entry_id
        assert actual.device_id == original_device.id
        assert actual.name == "My relay" and actual.icon == "mdi:lamp"
        assert (
            registry.async_get(disabled.entity_id).disabled_by
            == er.RegistryEntryDisabler.USER
        )
        device = devices.async_get(original_device.id)
        assert device is not None
        assert device.config_entry_id == entry.entry_id
        assert device.name_by_user == "My switch"
        assert "second_device" in entry.runtime_data.clients
        assert hass.states.get(original_entity.entity_id).state == "off"
        assert len(entry.data["devices"]) == 2
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert (
            registry.async_get(original_entity.entity_id).device_id
            == original_device.id
        )
        assert len(entry.data["devices"]) == 2
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
