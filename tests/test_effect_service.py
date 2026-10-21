"""Bulk effects isolate device failures and cancellation while limiting traffic."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.cozylife import async_setup
from custom_components.cozylife.const import SCENES
from custom_components.cozylife.coordinator import CozyLifeCoordinator
from custom_components.cozylife.light import CozyLifeLight
from custom_components.cozylife.runtime import CozyLifeRuntimeData
from custom_components.cozylife.tcp_client import tcp_client


@pytest.fixture
async def effect_lights(hass, entry, mocker):
    await async_setup(hass, {})
    clients, coordinators, lights = {}, {}, []
    for index in range(6):
        client = tcp_client("127.0.0.1")
        client._device_id = f"light_{index}"
        client._device_type_code = "01"
        client._dpid = [1, 2, 4, 5, 6]
        client.control = AsyncMock(return_value=True)
        coordinator = CozyLifeCoordinator(hass, entry, client, 60)
        coordinator.async_set_updated_data({"1": 255, "2": 1, "4": 400, "5": 0, "6": 0})
        light = CozyLifeLight(coordinator, hass, SCENES, default_transition=0)
        light._apply_state(coordinator.data)
        clients[client.device_id] = client
        coordinators[client.device_id] = coordinator
        lights.append(light)
    entry.runtime_data = CozyLifeRuntimeData(clients, coordinators, [], lights)
    mocker.patch.object(
        hass.config_entries, "async_loaded_entries", return_value=[entry]
    )
    yield lights
    await entry.runtime_data.async_shutdown()


async def call_effect(hass):
    await hass.services.async_call(
        "cozylife", "set_all_effect", {"effect": "warm"}, blocking=True
    )


async def test_bulk_effect_completes_healthy_lights_before_reporting_errors(
    hass, effect_lights
):
    effect_lights[0]._tcp_client.control.return_value = False
    with pytest.raises(HomeAssistantError):
        await call_effect(hass)
    assert effect_lights[0].effect == "manual"
    assert all(light.effect == "warm" for light in effect_lights[1:])


async def test_bulk_effect_runs_concurrently_with_a_bound(hass, effect_lights):
    release, four_started = asyncio.Event(), asyncio.Event()
    active, peak, started = 0, 0, 0

    async def control(payload):
        nonlocal active, peak, started
        active += 1
        started += 1
        peak = max(peak, active)
        if active == 4:
            four_started.set()
        try:
            await release.wait()
            return True
        finally:
            active -= 1

    for light in effect_lights:
        light._tcp_client.control.side_effect = control
    task = asyncio.create_task(call_effect(hass))
    try:
        await asyncio.wait_for(four_started.wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert started == 4
    finally:
        release.set()
        await task
    assert peak == 4
    assert all(light.effect == "warm" for light in effect_lights)


async def test_new_command_on_one_light_does_not_cancel_bulk_effect(
    hass, effect_lights
):
    started = asyncio.Event()
    first = effect_lights[0]

    async def control(payload):
        if payload.get("1") != 0:
            started.set()
            await asyncio.Event().wait()
        return True

    first._tcp_client.control.side_effect = control
    task = asyncio.create_task(call_effect(hass))
    try:
        await asyncio.wait_for(started.wait(), timeout=0.5)
        await first.async_turn_off(transition=0)
        await asyncio.wait_for(task, timeout=1)
        assert first.is_on is False
        assert all(light.effect == "warm" for light in effect_lights[1:])
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancelling_bulk_effect_cleans_up_device_tasks(hass, effect_lights):
    started = asyncio.Event()
    active = 0

    async def control(payload):
        nonlocal active
        active += 1
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    for light in effect_lights:
        light._tcp_client.control.side_effect = control
    task = asyncio.create_task(call_effect(hass))
    await asyncio.wait_for(started.wait(), timeout=0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0
    assert all(light._transition_task is None for light in effect_lights)


async def test_bulk_effect_does_not_command_a_light_unloaded_while_queued(
    hass, effect_lights
):
    started, release = asyncio.Event(), asyncio.Event()

    async def control(payload):
        started.set()
        await release.wait()
        return True

    for light in effect_lights[:4]:
        light._tcp_client.control.side_effect = control
    task = asyncio.create_task(call_effect(hass))
    await asyncio.wait_for(started.wait(), timeout=0.5)
    removed = effect_lights[-1]
    await removed.async_will_remove_from_hass()
    release.set()
    try:
        await task
    except HomeAssistantError:
        pass
    removed._tcp_client.control.assert_not_awaited()
    assert removed.effect == "manual"
    assert all(light.effect == "warm" for light in effect_lights[:-1])
