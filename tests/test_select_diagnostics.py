"""Optional settings and privacy-preserving, read-only diagnostics."""

from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.cozylife.const import DOMAIN
from custom_components.cozylife.coordinator import CozyLifeCoordinator
from custom_components.cozylife.diagnostics import async_get_config_entry_diagnostics
from custom_components.cozylife.select import CozyLifeSetting
from custom_components.cozylife.tcp_client import tcp_client


async def test_setting_ack_and_no_setup_write(hass, entry):
    client = tcp_client("192.168.88.18")
    client._device_id = "switch_77f8"
    client.control = AsyncMock(return_value=True)
    coordinator = CozyLifeCoordinator(hass, entry, client, 5)
    coordinator.async_set_updated_data({"1": 2, "18": 2})
    entity = CozyLifeSetting(coordinator, "18")
    assert entity.current_option == "previous_state"
    assert not entity.entity_registry_enabled_default
    client.control.assert_not_awaited()
    await entity.async_select_option("on")
    assert entity.current_option == "on"
    client.control.return_value = False
    with pytest.raises(HomeAssistantError):
        await entity.async_select_option("off")
    assert entity.current_option == "on"
    await coordinator.async_shutdown()


async def test_diagnostics_redact_identifiers_without_network(hass, entry):
    client = tcp_client("192.168.88.18")
    client._device_id = "switch_77f8"
    client.query = AsyncMock()
    client.control = AsyncMock()
    coordinator = CozyLifeCoordinator(hass, entry, client, 5)
    coordinator.async_set_updated_data({"1": 2})
    hass.data[DOMAIN] = {
        entry.entry_id: {
            "clients": {client.device_id: client},
            "coordinators": {client.device_id: coordinator},
        }
    }
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["devices"][0]["metadata"]["did"] == "**REDACTED**"
    assert result["devices"][0]["metadata"]["ip"] == "**REDACTED**"
    assert result["devices"][0]["state"] == {"1": 2}
    client.query.assert_not_awaited()
    client.control.assert_not_awaited()
    await coordinator.async_shutdown()
