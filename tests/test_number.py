"""Native light countdown control, gating and shared state polling."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.number import NumberDeviceClass, NumberMode
from homeassistant.const import UnitOfTime
from homeassistant.exceptions import HomeAssistantError

from custom_components.cozylife.const import DOMAIN
from custom_components.cozylife.coordinator import CozyLifeCoordinator
from custom_components.cozylife.number import CozyLifeCountdown, async_setup_entry
from custom_components.cozylife.runtime import CozyLifeRuntimeData
from custom_components.cozylife.tcp_client import tcp_client


@pytest.fixture
async def countdown(hass, entry):
    client = tcp_client("192.168.88.31")
    client._device_id = "strip_c8dc"
    client._pid = "o0mmpn"
    client._device_type_code = "01"
    client._dpid = [1, 13]
    client._device_model_name = "Smart led Strip"
    client.query = AsyncMock(return_value={"1": 1, "13": 30})
    client.control = AsyncMock(return_value=True)
    coordinator = CozyLifeCoordinator(hass, entry, client, 60)
    await coordinator.async_refresh()
    yield CozyLifeCountdown(coordinator)
    await coordinator.async_shutdown()


async def test_countdown_metadata_and_no_setup_write(countdown):
    assert countdown.unique_id == "strip_c8dc_countdown"
    assert countdown.device_info["identifiers"] == {(DOMAIN, "strip_c8dc")}
    assert countdown.device_class == NumberDeviceClass.DURATION
    assert countdown.native_unit_of_measurement == UnitOfTime.SECONDS
    assert countdown.native_min_value == 0
    assert countdown.native_max_value == 86400
    assert countdown.native_step == 1
    assert countdown.mode == NumberMode.BOX
    assert not countdown.entity_registry_enabled_default
    assert not countdown.should_poll
    assert countdown.native_value == 30
    countdown.coordinator.client.control.assert_not_awaited()


@pytest.mark.parametrize("value", [0, 1, 60.0, 86400])
async def test_countdown_only_writes_native_timer(countdown, value):
    coordinator = countdown.coordinator
    await countdown.async_set_native_value(value)
    coordinator.client.control.assert_awaited_once_with({"13": int(value)})
    assert type(coordinator.client.control.await_args.args[0]["13"]) is int
    assert countdown.native_value == value
    assert coordinator.data["1"] == 1


@pytest.mark.parametrize(
    "value", [-1, 86401, 1.5, True, "60", float("nan"), float("inf")]
)
async def test_invalid_countdown_never_writes(countdown, value):
    with pytest.raises(HomeAssistantError, match="Countdown must be an integer"):
        await countdown.async_set_native_value(value)
    countdown.coordinator.client.control.assert_not_awaited()
    assert countdown.native_value == 30


async def test_rejected_countdown_keeps_state_and_marks_unavailable(countdown):
    countdown.coordinator.client.control.return_value = False
    with pytest.raises(HomeAssistantError, match="did not acknowledge"):
        await countdown.async_set_native_value(60)
    assert countdown.native_value == 30
    assert not countdown.available


@pytest.mark.parametrize("value", [None, "30", True, -1, 86401, 1.5])
async def test_invalid_report_is_unknown_without_network(countdown, value):
    countdown.coordinator.async_set_updated_data({"1": 1, "13": value})
    countdown.coordinator.client.query.reset_mock()
    assert countdown.native_value is None
    countdown.coordinator.client.query.assert_not_awaited()


async def test_countdown_reads_hidden_dpid_only_while_subscribed(countdown):
    coordinator = countdown.coordinator
    client = coordinator.client
    client.query.reset_mock()
    client.query.return_value = {"1": 1}
    await coordinator.async_refresh()
    client.query.assert_awaited_once_with()
    assert countdown.native_value is None

    unsubscribe = coordinator.async_add_listener(lambda: None, context="13")
    client.query.reset_mock()
    client.query.side_effect = [{"1": 1}, {"13": 20, "1": 0, "14": "ignored"}]
    await coordinator.async_refresh()
    assert [call.args for call in client.query.await_args_list] == [(), ([13],)]
    assert coordinator.data == {"1": 1, "13": 20}
    assert countdown.native_value == 20
    unsubscribe()


async def test_failed_hidden_countdown_read_does_not_break_light(countdown):
    coordinator = countdown.coordinator
    unsubscribe = coordinator.async_add_listener(lambda: None, context="13")
    coordinator.client.query.side_effect = [{"1": 1}, None]
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.data == {"1": 1}
    assert countdown.native_value is None
    unsubscribe()


async def test_existing_countdown_needs_no_extra_query(countdown):
    coordinator = countdown.coordinator
    unsubscribe = coordinator.async_add_listener(lambda: None, context="13")
    coordinator.client.query.reset_mock()
    await coordinator.async_refresh()
    coordinator.client.query.assert_awaited_once_with()
    assert countdown.native_value == 30
    unsubscribe()


@pytest.mark.parametrize(
    "type_code,dpids,count", [("01", [1, 13], 1), ("01", [1], 0), ("00", [1, 13], 0)]
)
async def test_countdown_created_only_for_supported_lights(
    hass, entry, countdown, type_code, dpids, count
):
    coordinator = countdown.coordinator
    entry.runtime_data = CozyLifeRuntimeData(
        clients={"strip_c8dc": coordinator.client},
        devices=[{"did": "strip_c8dc", "device_type_code": type_code, "dpid": dpids}],
        coordinators={"strip_c8dc": coordinator},
    )
    add_entities = MagicMock()
    await async_setup_entry(hass, entry, add_entities)
    assert len(add_entities.call_args.args[0]) == count
