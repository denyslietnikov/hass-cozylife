"""CozyLife light platform."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    ATTR_TRANSITION,
    PLATFORM_SCHEMA,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EFFECT
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import color as colorutil

from .const import (
    BRIGHT,
    CONF_DEVICE_TYPE_CODE,
    DEFAULT_MAX_KELVIN,
    DEFAULT_MIN_KELVIN,
    DOMAIN,
    HUE,
    LIGHT_TYPE_CODE,
    SAT,
    TEMP,
)
from .coordinator import CozyLifeCoordinator
from .tcp_client import tcp_client

LIGHT_SCHEMA = vol.Schema(
    {
        vol.Required("ip"): cv.string,
        vol.Required("did"): cv.string,
        vol.Optional("dmn", default="Smart Bulb Light"): cv.string,
        vol.Optional("pid", default="p93sfg"): cv.string,
        vol.Optional("dpid", default=[1, 2, 3, 4, 5, 7, 8, 9, 13, 14]): vol.All(
            cv.ensure_list, [int]
        ),
    }
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional("lights", default=[]): vol.All(cv.ensure_list, [LIGHT_SCHEMA]),
        vol.Optional("optimistic", default=False): cv.boolean,
    }
)

SCAN_INTERVAL = timedelta(seconds=60)
MIN_INTERVAL = 0.2

CIRCADIAN_BRIGHTNESS = True
try:
    import custom_components.circadian_lighting as cir

    DATA_CIRCADIAN_LIGHTING = cir.DOMAIN
except Exception:
    CIRCADIAN_BRIGHTNESS = False

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_EFFECT = "set_effect"
SCENES = ["manual", "natural", "sleep", "warm", "study", "chrismas"]
SERVICE_SCHEMA_SET_EFFECT = {
    vol.Required(CONF_EFFECT): vol.In([mode.lower() for mode in SCENES])
}


def _dpid_set(client: tcp_client) -> set[str]:
    """Return DPID values as strings."""
    return {str(item) for item in client.dpid or []}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up CozyLife lights from a hub config entry."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinators = entry_data["coordinators"]
    devices = entry_data["devices"]

    entities: list[LightEntity] = []
    for dev in devices:
        if dev.get(CONF_DEVICE_TYPE_CODE, LIGHT_TYPE_CODE) != LIGHT_TYPE_CODE:
            continue

        coordinator = coordinators.get(dev["did"])
        if coordinator is None:
            continue

        if "switch" in dev.get("dmn", "").lower():
            entities.append(CozyLifeSwitchAsLight(coordinator, hass))
        else:
            entities.append(CozyLifeLight(coordinator, hass, SCENES))

    if entities:
        async_add_entities(entities)

    entry_data.setdefault("light_entities", [])
    entry_data["light_entities"].extend(
        entity for entity in entities if isinstance(entity, CozyLifeLight)
    )

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_EFFECT, SERVICE_SCHEMA_SET_EFFECT, "async_set_effect"
    )


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_devices: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Import YAML light configuration as config entries."""
    _LOGGER.warning(
        "Configuration of CozyLife lights via YAML is deprecated. "
        "The YAML config will be imported as config entries."
    )
    for item in config.get("lights", []):
        import_data = {
            "ip": item["ip"],
            "did": item["did"],
            "pid": item.get("pid", "p93sfg"),
            "dmn": item.get("dmn", "Smart Bulb Light"),
            "dpid": item.get("dpid", [1, 2, 3, 4, 5, 7, 8, 9, 13, 14]),
            CONF_DEVICE_TYPE_CODE: LIGHT_TYPE_CODE,
            "rockers": 1,
        }
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "import"},
                data=import_data,
            )
        )


class CozyLifeSwitchAsLight(CoordinatorEntity[CozyLifeCoordinator], LightEntity):
    """Switch-like CozyLife device exposed as a light."""

    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_is_on = None
    _unrecorded_attributes = frozenset({"brightness", "color_temp_kelvin"})

    def __init__(self, coordinator: CozyLifeCoordinator, hass) -> None:
        """Initialize."""
        super().__init__(coordinator)
        tcp_client = coordinator.client
        self.hass = hass
        self._tcp_client = tcp_client
        self._unique_id = tcp_client.device_id
        self._name = tcp_client.device_id[-4:]
        self._state: dict[str, Any] | None = None

    @property
    def device_info(self) -> DeviceInfo:
        """Return device registry information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._tcp_client.device_id)},
            name=self._tcp_client.device_model_name,
            manufacturer="CozyLife",
            model=self._tcp_client._pid,
        )

    @property
    def unique_id(self) -> str | None:
        """Return a unique ID."""
        return self._unique_id

    @property
    def name(self) -> str:
        """Return entity name."""
        return f"cozylife:{self._name}"

    async def async_added_to_hass(self) -> None:
        """Fetch initial state when entity is added."""
        await super().async_added_to_hass()
        self._apply_state(self.coordinator.data)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._apply_state(self.coordinator.data)
        super()._handle_coordinator_update()

    async def async_update(self) -> None:
        """Poll device state."""
        await self.coordinator.async_request_refresh()
        self._apply_state(self.coordinator.data)

    async def _refresh_state(self) -> None:
        """Query device and update state attributes."""
        await self.async_update()

    def _apply_state(self, state: dict | None) -> None:
        self._state = state
        if self._state:
            self._attr_is_on = self._state.get("1", 0) > 0

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        await self.coordinator.async_control({"1": 1})
        self._apply_state(self.coordinator.data)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        await self.coordinator.async_control({"1": 0})
        self._apply_state(self.coordinator.data)


class CozyLifeLight(CozyLifeSwitchAsLight, RestoreEntity):
    """CozyLife RGB/CCT light."""

    _attr_brightness: int | None = None
    _attr_color_mode: ColorMode | None = None
    _attr_color_temp_kelvin: int | None = None
    _attr_hs_color: tuple[float, float] | None = None
    _attr_supported_features = LightEntityFeature.EFFECT | LightEntityFeature.TRANSITION
    _unrecorded_attributes = frozenset({"brightness", "color_temp_kelvin"})

    def __init__(
        self, coordinator: CozyLifeCoordinator, hass, scenes: list[str]
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, hass)
        tcp_client = coordinator.client
        self._scenes = list(scenes)
        if not {"7", "8"} <= _dpid_set(tcp_client):
            self._scenes = [scene for scene in scenes if scene != "chrismas"]
        self._transition_task = None
        self._command_lock = asyncio.Lock()
        self._effect = "manual"
        self._cl = None
        self._max_brightness = 255
        self._min_brightness = 1
        self._transitioning = 0
        self._attr_is_on = False
        self._attr_brightness = 0
        self._attr_hs_color = (0, 0)
        self._attr_min_color_temp_kelvin = DEFAULT_MIN_KELVIN
        self._attr_max_color_temp_kelvin = DEFAULT_MAX_KELVIN
        self._kelvin_ratio = (DEFAULT_MAX_KELVIN - DEFAULT_MIN_KELVIN) / 1000
        self._attr_color_temp_kelvin = DEFAULT_MAX_KELVIN

        dpid = _dpid_set(tcp_client)
        supported: set[ColorMode] = set()
        if TEMP in dpid:
            supported.add(ColorMode.COLOR_TEMP)
        if HUE in dpid or SAT in dpid:
            supported.add(ColorMode.HS)
        if BRIGHT in dpid and not supported:
            supported.add(ColorMode.BRIGHTNESS)
        if not supported:
            supported = {ColorMode.ONOFF}

        self._attr_supported_color_modes = supported
        if BRIGHT not in dpid:
            self._attr_supported_features &= ~LightEntityFeature.TRANSITION
        if ColorMode.HS in supported:
            self._attr_color_mode = ColorMode.HS
        elif ColorMode.COLOR_TEMP in supported:
            self._attr_color_mode = ColorMode.COLOR_TEMP
        elif ColorMode.BRIGHTNESS in supported:
            self._attr_color_mode = ColorMode.BRIGHTNESS
        else:
            self._attr_color_mode = ColorMode.ONOFF

    @property
    def effect(self) -> str:
        """Return current effect."""
        return self._effect

    @property
    def effect_list(self) -> list[str]:
        """Return supported effects."""
        return self._scenes

    @property
    def assumed_state(self) -> bool:
        """Return whether the state is assumed."""
        return False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        return {
            "last_effect": self._effect,
            "transitioning": self._transitioning,
        }

    def _device_temp_from_kelvin(self, kelvin: int) -> int:
        """Convert Kelvin to protocol 0-1000 color temperature."""
        value = round((kelvin - DEFAULT_MIN_KELVIN) / self._kelvin_ratio)
        return max(0, min(1000, value))

    def _kelvin_from_device_temp(self, value: int) -> int:
        """Convert protocol 0-1000 color temperature to Kelvin."""
        value = max(0, min(1000, value))
        return round(DEFAULT_MIN_KELVIN + value * self._kelvin_ratio)

    def calc_color_temp_kelvin(self) -> int | None:
        """Calculate circadian color temperature in Kelvin."""
        if not CIRCADIAN_BRIGHTNESS:
            return None
        if self._cl is None:
            self._cl = self.hass.data.get(DATA_CIRCADIAN_LIGHTING)
            if self._cl is None:
                return None
        return self._cl._colortemp

    def calc_brightness(self) -> int | None:
        """Calculate circadian brightness."""
        if not CIRCADIAN_BRIGHTNESS:
            return None
        if self._cl is None:
            self._cl = self.hass.data.get(DATA_CIRCADIAN_LIGHTING)
            if self._cl is None:
                return None
        if self._cl._percent > 0:
            return self._max_brightness
        return round(
            (
                (self._max_brightness - self._min_brightness)
                * ((100 + self._cl._percent) / 100)
            )
            + self._min_brightness
        )

    async def async_added_to_hass(self) -> None:
        """Restore effect and fetch initial state."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.attributes.get("last_effect") in self.effect_list:
            self._effect = last_state.attributes["last_effect"]
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_natural_update, SCAN_INTERVAL
            )
        )

    async def async_set_effect(self, effect: str) -> None:
        """Set effect, applying it immediately if the light is on."""
        if effect not in self.effect_list:
            raise HomeAssistantError("Unsupported CozyLife effect")
        if self._attr_is_on:
            await self.async_turn_on(effect=effect)
        else:
            self._effect = effect
            self.async_write_ha_state()

    async def _async_natural_update(self, _now) -> None:
        """Keep the optional circadian effect independent from state polling."""
        if self._effect == "natural" and not self._transition_task:
            await self.coordinator.async_refresh()
            self._apply_state(self.coordinator.data)
            if not self.available or not self.is_on:
                return
            try:
                await self.async_turn_on(effect="natural")
            except HomeAssistantError:
                _LOGGER.debug("Could not update the natural effect", exc_info=True)

    async def async_will_remove_from_hass(self) -> None:
        if (
            self._transition_task
            and self._transition_task is not asyncio.current_task()
        ):
            self._transition_task.cancel()
            try:
                await self._transition_task
            except asyncio.CancelledError:
                pass
        await super().async_will_remove_from_hass()

    def _apply_state(self, state: dict | None) -> None:
        """Apply shared, acknowledged or polled device state."""
        super()._apply_state(state)
        if not state:
            return
        if isinstance(state.get("4"), (int, float)):
            self._attr_brightness = max(0, min(255, round(state["4"] / 1000 * 255)))
        if (
            state.get("2", 0) == 0
            and ColorMode.COLOR_TEMP in self.supported_color_modes
        ):
            if isinstance(state.get("3"), (int, float)) and 0 <= state["3"] <= 1000:
                self._attr_color_mode = ColorMode.COLOR_TEMP
                self._attr_color_temp_kelvin = self._kelvin_from_device_temp(state["3"])
        elif ColorMode.HS in self.supported_color_modes:
            if isinstance(state.get("5"), (int, float)) and isinstance(
                state.get("6"), (int, float)
            ):
                self._attr_color_mode = ColorMode.HS
                self._attr_hs_color = (
                    max(0, min(360, state["5"])),
                    max(0, min(100, state["6"] / 10)),
                )

    def _white_payload(self, kelvin: int) -> dict:
        if ColorMode.COLOR_TEMP in self.supported_color_modes:
            return {"2": 0, "3": self._device_temp_from_kelvin(kelvin)}
        if ColorMode.HS in self.supported_color_modes:
            hue, saturation = colorutil.color_temperature_to_hs(kelvin)
            return {"2": 1, "5": round(hue), "6": round(saturation * 10)}
        return {}

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Build a target without changing HA state before acknowledgement."""
        dpids = _dpid_set(self._tcp_client)
        payload = {"1": 255}
        if "2" in dpids:
            payload["2"] = 1 if self.color_mode == ColorMode.HS else 0
        effect = kwargs.get(ATTR_EFFECT, self._effect)
        if effect not in self.effect_list:
            raise HomeAssistantError("Unsupported CozyLife effect")
        changed = False
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if brightness is not None and BRIGHT in dpids:
            payload["4"] = round(max(0, min(255, brightness)) / 255 * 1000)
            changed = True
        kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
        if kelvin is not None:
            if ColorMode.COLOR_TEMP not in self.supported_color_modes:
                raise HomeAssistantError(
                    "Color temperature is not supported by this device"
                )
            payload.update(self._white_payload(kelvin))
            changed = True
        hs = kwargs.get(ATTR_HS_COLOR)
        if hs is not None:
            if ColorMode.HS not in self.supported_color_modes:
                raise HomeAssistantError("HS color is not supported by this device")
            payload.update({"2": 1, "5": round(hs[0]), "6": round(hs[1] * 10)})
            changed = True
        transition = kwargs.get(ATTR_TRANSITION, 0)
        if changed:
            effect = "manual"
        elif effect in ("sleep", "warm", "study"):
            if BRIGHT in dpids:
                payload["4"] = 12 if effect == "sleep" else 1000
            payload.update(
                self._white_payload(
                    DEFAULT_MAX_KELVIN if effect == "study" else DEFAULT_MIN_KELVIN
                )
            )
        elif effect == "natural":
            brightness = self.calc_brightness()
            kelvin = self.calc_color_temp_kelvin()
            if brightness is not None and BRIGHT in dpids:
                payload["4"] = round(brightness / 255 * 1000)
            if kelvin is not None:
                payload.update(self._white_payload(kelvin))
            transition = kwargs.get(ATTR_TRANSITION, 5 if CIRCADIAN_BRIGHTNESS else 0)
        elif effect == "chrismas":
            payload.update(
                {
                    "2": 1,
                    "4": 1000,
                    "8": 500,
                    "7": "03000003E8FFFF007803E8FFFF00F003E8FFFF003C03E8FFFF00B403E8FFFF010E03E8FFFF002603E8FFFF",
                }
            )
            transition = 0
        await self._async_run_command(payload, transition, effect)

    async def _async_run_command(
        self,
        payload: dict,
        transition: float,
        effect: str | None = None,
        turn_off: bool = False,
    ) -> None:
        """Cancel an older fade and interpolate against a monotonic deadline."""
        task = asyncio.current_task()
        previous = self._transition_task
        self._transition_task = task
        try:
            if previous and previous is not task:
                previous.cancel()
                try:
                    await previous
                except asyncio.CancelledError:
                    if task.cancelling():
                        raise
            async with self._command_lock:
                self._transitioning = max(0, transition or 0)
                state = dict(self.coordinator.data or {})
                if not state.get("1") and "4" in payload:
                    state["4"] = 0
                fields = {
                    key: state[key]
                    for key in ("3", "4", "5", "6")
                    if key in payload
                    and isinstance(state.get(key), (int, float))
                    and state[key] != payload[key]
                }
                if self._transitioning and fields:
                    loop = asyncio.get_running_loop()
                    start = loop.time()
                    deadline = start + self._transitioning
                    while loop.time() < deadline:
                        fraction = min(1, (loop.time() - start) / self._transitioning)
                        frame = {
                            **payload,
                            **{
                                key: round(value + (payload[key] - value) * fraction)
                                for key, value in fields.items()
                            },
                        }
                        await self.coordinator.async_control(frame)
                        await asyncio.sleep(
                            max(0, min(MIN_INTERVAL, deadline - loop.time()))
                        )
                # Always send the final command, even for a zero-distance fade.
                await self.coordinator.async_control(payload)
                if turn_off:
                    await self.coordinator.async_control({"1": 0})
                if effect is not None:
                    self._effect = effect
                self._apply_state(self.coordinator.data)
        finally:
            if self._transition_task is task:
                self._transition_task = None
                self._transitioning = 0
                if self.entity_id:
                    self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Fade brightness in the current color mode before switching off."""
        transition = kwargs.get(ATTR_TRANSITION, 5 if self._effect == "natural" else 0)
        if transition and BRIGHT in _dpid_set(self._tcp_client):
            payload = {"1": 255, "4": 0}
            if "2" in _dpid_set(self._tcp_client):
                payload["2"] = 1 if self.color_mode == ColorMode.HS else 0
            await self._async_run_command(payload, transition, turn_off=True)
        else:
            await self._async_run_command({"1": 0}, 0)
