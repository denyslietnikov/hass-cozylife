"""Shared polling, sibling safety and availability on real HA core."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.cozylife.coordinator import CozyLifeCoordinator
from custom_components.cozylife.switch import CozyLifeSwitch
from custom_components.cozylife.tcp_client import tcp_client


@pytest.fixture
async def coordinator(hass, entry):
    client = tcp_client("192.168.88.18")
    client._device_id = "switch_77f8"
    client._dpid = [1, 18, 19]
    client.query = AsyncMock(return_value={"1": 2})
    client.control = AsyncMock(return_value=True)
    coordinator = CozyLifeCoordinator(hass, entry, client, 5)
    await coordinator.async_refresh()
    yield coordinator
    await coordinator.async_shutdown()


async def test_two_rockers_use_one_query(coordinator):
    first, second = (
        CozyLifeSwitch(coordinator, "wippe1"),
        CozyLifeSwitch(coordinator, "wippe2"),
    )
    assert first.unique_id == "switch_77f8_wippe1"
    assert second.unique_id == "switch_77f8_wippe2"
    assert first.is_on is False
    assert second.is_on is True
    assert first.should_poll is False
    coordinator.client.query.reset_mock()
    await coordinator.async_refresh()
    coordinator.client.query.assert_awaited_once()
    assert coordinator.update_interval.total_seconds() == 5


@pytest.mark.parametrize(
    "state", [None, {}, {"1": "2"}, {"1": True}, {"1": 256}, {"1": -1}]
)
async def test_failed_read_never_clears_sibling(coordinator, state):
    coordinator.client.query.return_value = state
    with pytest.raises(HomeAssistantError):
        await CozyLifeSwitch(coordinator, "wippe1").async_turn_on()
    coordinator.client.control.assert_not_awaited()
    assert coordinator.data == {"1": 2}
    assert not coordinator.last_update_success


async def test_failed_write_not_published(coordinator):
    coordinator.client.control.return_value = False
    with pytest.raises(HomeAssistantError):
        await CozyLifeSwitch(coordinator, "wippe1").async_turn_on()
    assert coordinator.data == {"1": 2}
    assert not coordinator.last_update_success


async def test_concurrent_rockers_preserve_bits(coordinator):
    state = {"1": 0}

    async def query():
        await asyncio.sleep(0)
        return dict(state)

    async def control(payload):
        await asyncio.sleep(0)
        state.update(payload)
        return True

    coordinator.client.query.side_effect = query
    coordinator.client.control.side_effect = control
    await asyncio.gather(
        CozyLifeSwitch(coordinator, "wippe1").async_turn_on(),
        CozyLifeSwitch(coordinator, "wippe2").async_turn_on(),
        coordinator.async_refresh(),
    )
    assert state["1"] == 3
    assert coordinator.data["1"] == 3


async def test_availability_recovers_with_poll(coordinator):
    entity = CozyLifeSwitch(coordinator, "wippe1")
    coordinator.client.query.return_value = None
    await coordinator.async_refresh()
    assert not entity.available
    coordinator.client.query.return_value = {"1": 3}
    await coordinator.async_refresh()
    assert entity.available
    assert entity.is_on


async def test_repeated_failures_back_off_but_explicit_command_retries(
    coordinator, mocker
):
    clock = mocker.patch("custom_components.cozylife.coordinator.time")
    clock.monotonic.return_value = 100
    coordinator.client.query.return_value = None
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert coordinator._retry_at == 105
    calls = coordinator.client.query.await_count
    await coordinator.async_refresh()
    assert coordinator.client.query.await_count == calls
    coordinator.client.query.return_value = {"1": 2}
    await CozyLifeSwitch(coordinator, "wippe1").async_turn_on()
    assert coordinator.data["1"] == 3
    assert coordinator._retry_at == 0
    clock.monotonic.return_value = 110
    await coordinator.async_refresh()
    assert coordinator.last_update_success
