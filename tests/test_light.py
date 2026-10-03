"""Acknowledgements, RGB effects and transition cancellation."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.light import ColorMode
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError

from custom_components.cozylife.coordinator import CozyLifeCoordinator
from custom_components.cozylife.light import (
    SCENES,
    CozyLifeLight,
    CozyLifeSwitchAsLight,
)
from custom_components.cozylife.tcp_client import tcp_client


@pytest.fixture
async def light(hass, entry):
    client = tcp_client("192.168.88.31")
    client._device_id = "131976607c2c67d1c8dc"
    client._dpid = [1, 2, 4, 5, 6, 7, 8]
    client._pid = "o0mmpn"
    client.query = AsyncMock(return_value={"1": 0, "2": 1, "4": 1000, "5": 0, "6": 0})
    client.control = AsyncMock(return_value=True)
    coordinator = CozyLifeCoordinator(hass, entry, client, 60)
    await coordinator.async_refresh()
    entity = CozyLifeLight(coordinator, hass, SCENES, default_transition=0)
    entity._apply_state(coordinator.data)
    yield entity
    await coordinator.async_shutdown()


async def test_no_distance_transition_still_turns_on(light):
    light.coordinator.async_set_updated_data({"1": 255, "2": 1, "4": 1000})
    light._apply_state(light.coordinator.data)
    await light.async_turn_on(transition=0.01)
    light._tcp_client.control.assert_awaited_once_with({"1": 255, "2": 1})
    assert light.is_on
    assert light.unique_id == "131976607c2c67d1c8dc"


async def test_plain_turn_on_with_transition_fades_to_previous_brightness(light):
    light.coordinator.async_set_updated_data({"1": 0, "2": 1, "4": 400})
    light._apply_state(light.coordinator.data)
    await light.async_turn_on(transition=0.01)
    payloads = [call.args[0] for call in light._tcp_client.control.call_args_list]
    assert len(payloads) >= 2
    assert payloads[0]["4"] == 0
    assert payloads[-1]["4"] == 400
    assert all("13" not in payload for payload in payloads)
    assert light.is_on
    assert light.brightness == 102


@pytest.mark.parametrize("transition", [0, 0.01])
async def test_turn_on_after_fade_off_restores_brightness(light, transition):
    light.coordinator.async_set_updated_data({"1": 255, "2": 1, "4": 400})
    light._apply_state(light.coordinator.data)
    await light.async_turn_off(transition=0.01)
    assert not light.is_on
    assert light.coordinator.data["4"] == 0
    assert light.extra_state_attributes["last_brightness"] == 400
    light._tcp_client.control.reset_mock()
    await light.async_turn_on(transition=transition)
    assert light._tcp_client.control.call_args.args[0]["4"] == 400
    assert light.is_on
    assert light.brightness == 102


async def test_brightness_poll_is_used_for_next_fade(light):
    light.coordinator.async_set_updated_data({"1": 0, "2": 1, "4": 10})
    light._apply_state(light.coordinator.data)
    await light.async_turn_on(transition=0.01)
    assert light._tcp_client.control.call_args.args[0]["4"] == 10
    assert light.extra_state_attributes["last_brightness"] == 10


async def test_new_brightness_overrides_remembered_level(light):
    await light.async_turn_on(brightness=50, transition=0.01)
    assert light.extra_state_attributes["last_brightness"] == 196
    await light.async_turn_off(transition=0.01)
    await light.async_turn_on(transition=0.01)
    assert light.brightness == 50


async def test_dimmable_light_defaults_to_two_seconds(light, mocker):
    entity = CozyLifeLight(light.coordinator, light.hass, SCENES)
    entity._apply_state(light.coordinator.data)
    run = mocker.patch.object(entity, "_async_run_command")
    await entity.async_turn_on()
    assert run.call_args.args == ({"1": 255, "2": 1, "4": 1000}, 2, "manual")
    entity.coordinator.async_set_updated_data({"1": 255, "2": 1, "4": 1000})
    await entity.async_turn_off()
    assert run.call_args.args == ({"1": 255, "2": 1, "4": 0}, 2)
    assert run.call_args.kwargs == {"turn_off": True}


async def test_default_fade_and_explicit_zero_override(light):
    light._default_transition = 0.01
    await light.async_turn_on()
    assert light._tcp_client.control.await_count >= 2
    light._tcp_client.control.reset_mock()
    await light.async_turn_off()
    assert light._tcp_client.control.await_count >= 3
    light._tcp_client.control.reset_mock()
    await light.async_turn_on(transition=0)
    light._tcp_client.control.assert_awaited_once()
    light._tcp_client.control.reset_mock()
    await light.async_turn_off(transition=0)
    light._tcp_client.control.assert_awaited_once_with({"1": 0})


async def test_explicit_duration_overrides_default(light, mocker):
    light._default_transition = 2
    run = mocker.patch.object(light, "_async_run_command")
    await light.async_turn_on(transition=0.5)
    assert run.call_args.args[1] == 0.5
    light.coordinator.async_set_updated_data({"1": 255, "2": 1, "4": 1000})
    await light.async_turn_off(transition=0.5)
    assert run.call_args.args[1] == 0.5


@pytest.mark.parametrize("live_brightness,expected", [(0, 400), (10, 10)])
async def test_remembered_brightness_restores_without_device_writes(
    light, mocker, live_brightness, expected
):
    light.coordinator.async_set_updated_data({"1": 0, "2": 1, "4": live_brightness})
    light._apply_state(light.coordinator.data)
    mocker.patch.object(CozyLifeSwitchAsLight, "async_added_to_hass")
    mocker.patch.object(
        light,
        "async_get_last_state",
        return_value=State("light.strip", "off", {"last_brightness": 400}),
    )
    mocker.patch(
        "custom_components.cozylife.light.async_track_time_interval",
        return_value=lambda: None,
    )
    await light.async_added_to_hass()
    assert light.extra_state_attributes["last_brightness"] == expected
    light._tcp_client.control.assert_not_awaited()


async def test_onoff_only_light_has_no_default_fade(light, mocker):
    light._tcp_client._dpid = [1]
    entity = CozyLifeLight(light.coordinator, light.hass, SCENES)
    run = mocker.patch.object(entity, "_async_run_command")
    await entity.async_turn_on()
    assert run.call_args.args == ({"1": 255}, 0, "manual")
    await entity.async_turn_off()
    assert run.call_args.args == ({"1": 0}, 0)


async def test_turn_off_already_off_never_powers_on_for_fade(light):
    light._default_transition = 2
    await light.async_turn_off()
    light._tcp_client.control.assert_awaited_once_with({"1": 0})
    assert not light.is_on


@pytest.mark.parametrize("effect", ["sleep", "warm", "study"])
async def test_rgb_white_effect_never_sends_cct(light, effect):
    await light.async_turn_on(effect=effect)
    payload = light._tcp_client.control.call_args.args[0]
    assert "3" not in payload
    assert payload["2"] == 1
    assert {"5", "6"} <= payload.keys()
    assert light.color_mode == ColorMode.HS
    assert light.effect == effect


async def test_rejected_command_does_not_publish_fake_on(light):
    light._tcp_client.control.return_value = False
    with pytest.raises(HomeAssistantError):
        await light.async_turn_on(brightness=50, hs_color=(120, 50))
    assert not light.is_on
    assert light.brightness == 255
    assert light.effect == "manual"
    assert not light.available
    assert light._transition_task is None


async def test_command_updates_state_after_ack_without_waiting_for_poll(light):
    started = asyncio.Event()
    acknowledge = asyncio.Event()

    async def control(payload):
        started.set()
        await acknowledge.wait()
        return True

    light._tcp_client.control.side_effect = control
    light._tcp_client.query.reset_mock()
    command = asyncio.create_task(light.async_turn_on(brightness=50))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert not light.is_on
        assert light.coordinator.data["1"] == 0
        acknowledge.set()
        await command
        assert light.is_on
        assert light.brightness == 50
        light._tcp_client.query.assert_not_awaited()
        assert light.coordinator.update_interval.total_seconds() == 60
    finally:
        if not command.done():
            command.cancel()
            await asyncio.gather(command, return_exceptions=True)


async def test_rgb_fade_off_keeps_mode(light):
    light.coordinator.async_set_updated_data(
        {"1": 255, "2": 1, "4": 1000, "5": 240, "6": 500}
    )
    light._apply_state(light.coordinator.data)
    await light.async_turn_off(transition=0.01)
    payloads = [call.args[0] for call in light._tcp_client.control.call_args_list]
    assert all(payload.get("2", 1) == 1 for payload in payloads)
    assert payloads[-1] == {"1": 0}
    assert not light.is_on


async def test_new_command_cancels_fade_before_final_off(light):
    light.coordinator.async_set_updated_data(
        {"1": 255, "2": 1, "4": 1000, "5": 0, "6": 0}
    )
    light._apply_state(light.coordinator.data)
    first_frame = asyncio.Event()

    async def control(payload):
        first_frame.set()
        return True

    light._tcp_client.control.side_effect = control
    fade = asyncio.create_task(light.async_turn_off(transition=2))
    await first_frame.wait()
    await light.async_turn_on(brightness=200, transition=0)
    with pytest.raises(asyncio.CancelledError):
        await fade
    assert all(
        call.args[0] != {"1": 0} for call in light._tcp_client.control.call_args_list
    )
    assert light.is_on
    assert light.brightness == 200
    assert light._transitioning == 0


async def test_transition_uses_elapsed_time_not_added_rtt(light):
    async def slow_control(payload):
        await asyncio.sleep(0.03)
        return True

    light._tcp_client.control.side_effect = slow_control
    start = asyncio.get_running_loop().time()
    await light.async_turn_on(brightness=100, transition=0.25)
    elapsed = asyncio.get_running_loop().time() - start
    assert 0.25 <= elapsed < 0.4
    assert light.brightness == 100


async def test_cct_light_preserves_kelvin_api(light):
    light._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
    await light.async_turn_on(color_temp_kelvin=4600)
    assert light._tcp_client.control.call_args.args[0]["3"] == 500
    assert light.color_temp_kelvin == 4600
    assert light.color_mode == ColorMode.COLOR_TEMP


async def test_unsupported_effect_is_rejected(light):
    light._scenes = ["manual"]
    with pytest.raises(HomeAssistantError):
        await light.async_turn_on(effect="chrismas")
    light._tcp_client.control.assert_not_awaited()


async def test_three_commands_only_latest_fade_survives(light):
    first = asyncio.create_task(light.async_turn_on(brightness=10, transition=2))
    await asyncio.sleep(0)
    second = asyncio.create_task(light.async_turn_off(transition=2))
    third = asyncio.create_task(light.async_turn_on(brightness=200, transition=0))
    await asyncio.wait_for(third, timeout=0.3)
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert light.is_on
    assert light.brightness == 200
    assert light._transition_task is None


async def test_natural_effect_does_not_undo_physical_off(light):
    light._effect = "natural"
    light._attr_is_on = True
    light._tcp_client.query.return_value = {"1": 0, "2": 1, "4": 1000}
    await light._async_natural_update(None)
    assert not light.is_on
    light._tcp_client.control.assert_not_awaited()
