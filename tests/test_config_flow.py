"""Hub options and discovery without touching existing entity identities."""

import pytest

from custom_components.cozylife.config_flow import (
    CozyLifeConfigFlow,
    CozyLifeOptionsFlow,
    _validate_range,
)
from custom_components.cozylife.const import DEFAULT_TRANSITION_SECONDS
from custom_components.cozylife.tcp_client import tcp_client


@pytest.mark.parametrize(
    "start,end,error",
    [
        ("::1", "::2", "invalid_ip"),
        ("bad", "192.168.88.2", "invalid_ip"),
        ("192.168.88.2", "192.168.89.2", "different_subnet"),
        ("192.168.88.3", "192.168.88.2", "invalid_range"),
        ("192.168.88.2", "192.168.88.2", None),
    ],
)
def test_range_validation(start, end, error):
    assert _validate_range(start, end) == error


@pytest.fixture
def flow(hass, entry, mocker):
    flow = CozyLifeOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    mocker.patch.object(hass.config_entries, "async_reload", return_value=True)
    return flow


async def test_rescan_updates_ip_without_losing_devices_or_rockers(
    flow, entry, mocker, hass
):
    known = {**entry.data["devices"][0], "ip": "192.168.88.27", "rockers": 1}
    new = {**known, "did": "new_device", "ip": "192.168.88.30"}
    mocker.patch.object(CozyLifeConfigFlow, "_scan_range", return_value=[known, new])
    result = await flow.async_step_rescan(
        {"start_ip": "192.168.88.1", "end_ip": "192.168.88.254"}
    )
    assert result["type"] == "create_entry"
    assert len(entry.data["devices"]) == 2
    assert entry.data["devices"][0]["did"] == "switch_77f8"
    assert entry.data["devices"][0]["rockers"] == 2
    assert entry.data["devices"][0]["ip"] == "192.168.88.27"
    await hass.async_block_till_done()
    hass.config_entries.async_reload.assert_awaited_once()


async def test_empty_rescan_does_not_delete_devices(flow, entry, mocker):
    before = dict(entry.data)
    mocker.patch.object(CozyLifeConfigFlow, "_scan_range", return_value=[])
    result = await flow.async_step_rescan(
        {"start_ip": "192.168.88.1", "end_ip": "192.168.88.254"}
    )
    assert result["errors"]["base"] == "cannot_connect"
    assert dict(entry.data) == before


async def test_device_edit_preserves_did_and_metadata(flow, entry):
    flow._device_id = "switch_77f8"
    result = await flow.async_step_edit_device({"ip": "192.168.88.30", "rockers": 2})
    assert result["type"] == "create_entry"
    assert entry.data["devices"][0]["did"] == "switch_77f8"
    assert entry.data["devices"][0]["pid"] == "e5aHVS"
    assert entry.data["devices"][0]["ip"] == "192.168.88.30"


async def test_known_dual_switch_discovery(mocker):
    client = tcp_client("192.168.88.18")
    client._device_id = "switch_77f8"
    client._pid = "e5aHVS"
    client._device_type_code = "00"
    mocker.patch(
        "custom_components.cozylife.config_flow.tcp_client", return_value=client
    )
    mocker.patch.object(client, "_connect")
    mocker.patch.object(client, "_device_info")
    mocker.patch.object(
        type(client), "available", new_callable=mocker.PropertyMock, return_value=True
    )
    result = await CozyLifeConfigFlow._probe_device("192.168.88.18")
    assert result["rockers"] == 2
    assert result["did"] == "switch_77f8"


async def test_options_offer_existing_intervals(flow):
    result = await flow.async_step_init()
    assert result["menu_options"] == ["settings", "rescan", "device"]
    result = await flow.async_step_settings(
        {"switch_interval": 5, "light_interval": 30}
    )
    assert result["data"] == {"switch_interval": 5, "light_interval": 30}


@pytest.mark.parametrize("value", [0, 301, "bad"])
async def test_invalid_poll_interval_not_saved(flow, value):
    result = await flow.async_step_settings(
        {"switch_interval": value, "light_interval": 60}
    )
    assert result["errors"]["base"] == "invalid_interval"


async def test_edit_cannot_assign_another_devices_ip(flow, entry, hass):
    other = {**entry.data["devices"][0], "did": "another", "ip": "192.168.88.27"}
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "devices": [entry.data["devices"][0], other]}
    )
    flow._device_id = "switch_77f8"
    result = await flow.async_step_edit_device({"ip": other["ip"], "rockers": 2})
    assert result["errors"]["base"] == "duplicate_ip"


@pytest.mark.parametrize("value", [0, 9, "bad"])
async def test_invalid_relay_count_not_saved(flow, entry, value):
    flow._device_id = "switch_77f8"
    result = await flow.async_step_edit_device(
        {"ip": "192.168.88.18", "rockers": value}
    )
    assert result["errors"]["base"] == "invalid_rockers"
    assert entry.data["devices"][0]["rockers"] == 2


@pytest.fixture
def light_device(hass, entry, flow):
    metadata = {
        "did": "strip_c8dc",
        "ip": "192.168.88.31",
        "pid": "o0mmpn",
        "dmn": "Smart led Strip",
        "device_type_code": "01",
        "dpid": [1, 2, 4, 5, 6],
    }
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "devices": [metadata]}
    )
    flow._device_id = metadata["did"]
    return metadata


async def test_light_options_default_transition_is_two_seconds(flow, light_device):
    import json

    from homeassistant.helpers import config_validation as cv
    from probatio import to_field_list

    result = await flow.async_step_edit_device()
    values = result["data_schema"]({"ip": light_device["ip"]})
    assert values["default_transition"] == DEFAULT_TRANSITION_SECONDS == 2
    assert "rockers" not in values
    # Verify the schema can reach the HA frontend, not just validate in Python.
    json.dumps(
        to_field_list(result["data_schema"], custom_serializer=cv.custom_serializer)
    )


@pytest.mark.parametrize("duration", [0, 0.5, 2, 60])
async def test_save_light_transition_preserves_identity_and_reloads(
    flow, entry, hass, light_device, duration
):
    result = await flow.async_step_edit_device(
        {"ip": light_device["ip"], "default_transition": duration}
    )
    assert result["type"] == "create_entry"
    assert entry.data["devices"][0] == {**light_device, "default_transition": duration}
    await hass.async_block_till_done()
    hass.config_entries.async_reload.assert_awaited_once()


@pytest.mark.parametrize(
    "duration", [-1, 61, "bad", None, True, float("nan"), float("inf")]
)
async def test_invalid_light_transition_does_not_save(
    flow, entry, light_device, duration
):
    before = dict(entry.data)
    result = await flow.async_step_edit_device(
        {"ip": light_device["ip"], "default_transition": duration}
    )
    assert result["errors"]["base"] == "invalid_transition"
    assert dict(entry.data) == before


async def test_rescan_keeps_configured_light_transition(
    flow, entry, hass, light_device, mocker
):
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "devices": [{**light_device, "default_transition": 4}]},
    )
    mocker.patch.object(CozyLifeConfigFlow, "_scan_range", return_value=[light_device])
    await flow.async_step_rescan(
        {"start_ip": "192.168.88.1", "end_ip": "192.168.88.254"}
    )
    assert entry.data["devices"][0]["default_transition"] == 4


async def test_switch_options_do_not_offer_transition(flow):
    flow._device_id = "switch_77f8"
    result = await flow.async_step_edit_device()
    values = result["data_schema"]({"ip": "192.168.88.18", "rockers": 2})
    assert "default_transition" not in values
