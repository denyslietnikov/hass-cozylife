"""Native light countdowns; never run or restore a timer in Home Assistant."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfTime
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_TYPE_CODE, DOMAIN, LIGHT_COUNTDOWN, LIGHT_TYPE_CODE
from .coordinator import CozyLifeCoordinator

MAX_COUNTDOWN_SECONDS = 24 * 60 * 60

if TYPE_CHECKING:
    from .runtime import CozyLifeConfigEntry


async def async_setup_entry(hass, entry: CozyLifeConfigEntry, async_add_entities):
    data = entry.runtime_data
    entities = []
    for device in data.devices:
        if device.get(CONF_DEVICE_TYPE_CODE, LIGHT_TYPE_CODE) != LIGHT_TYPE_CODE:
            continue
        if LIGHT_COUNTDOWN not in {str(dpid) for dpid in device.get("dpid", [])}:
            continue
        entities.append(CozyLifeCountdown(data.coordinators[device["did"]]))
    async_add_entities(entities)


class CozyLifeCountdown(CoordinatorEntity[CozyLifeCoordinator], NumberEntity):
    """Set DPID 13 in seconds; zero cancels the device's countdown."""

    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_native_min_value = 0
    _attr_native_max_value = MAX_COUNTDOWN_SECONDS
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:timer-outline"
    _attr_has_entity_name = True
    _attr_translation_key = "countdown"
    # DPID semantics must be confirmed per model before enabling this control.
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: CozyLifeCoordinator):
        super().__init__(coordinator, context=LIGHT_COUNTDOWN)
        self._attr_unique_id = f"{coordinator.client.device_id}_countdown"

    @property
    def device_info(self):
        client = self.coordinator.client
        return DeviceInfo(
            identifiers={(DOMAIN, client.device_id)},
            manufacturer="CozyLife",
            model=client._pid,
            name=client.device_model_name,
        )

    @property
    def native_value(self) -> int | None:
        value = (self.coordinator.data or {}).get(LIGHT_COUNTDOWN)
        if type(value) is int and 0 <= value <= MAX_COUNTDOWN_SECONDS:
            return value
        return None

    async def async_set_native_value(self, value: float) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= value <= MAX_COUNTDOWN_SECONDS
            or not math.isfinite(value)
            or value != int(value)
        ):
            raise HomeAssistantError(
                "Countdown must be an integer between 0 and 86400 seconds"
            )
        await self.coordinator.async_control({LIGHT_COUNTDOWN: int(value)})
