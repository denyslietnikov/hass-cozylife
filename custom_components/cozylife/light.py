"""CozyLife light platform."""

from __future__ import annotations

import asyncio
import logging
import time
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
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
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
    clients = entry_data["clients"]
    devices = entry_data["devices"]

    entities: list[LightEntity] = []
    for dev in devices:
        if dev.get(CONF_DEVICE_TYPE_CODE, LIGHT_TYPE_CODE) != LIGHT_TYPE_CODE:
            continue

        client = clients.get(dev["did"])
        if client is None:
            continue

        if "switch" in dev.get("dmn", "").lower():
            entities.append(CozyLifeSwitchAsLight(client, hass))
        else:
            entities.append(CozyLifeLight(client, hass, SCENES))

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


class CozyLifeSwitchAsLight(LightEntity):
    """Switch-like CozyLife device exposed as a light."""

    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_is_on = True
    _unrecorded_attributes = frozenset({"brightness", "color_temp_kelvin"})

    def __init__(self, tcp_client: tcp_client, hass) -> None:
        """Initialize."""
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

    @property
    def available(self) -> bool:
        """Return whether the device is available."""
        return self._tcp_client.available

    async def async_added_to_hass(self) -> None:
        """Fetch initial state when entity is added."""
        await super().async_added_to_hass()
        await self._refresh_state()

    async def async_update(self) -> None:
        """Poll device state."""
        await self._refresh_state()

    async def _refresh_state(self) -> None:
        """Query device and update state attributes."""
        self._state = await self._tcp_client.query()
        if self._state:
            self._attr_is_on = self._state.get("1", 0) > 0

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        self._attr_is_on = True
        await self._tcp_client.control({"1": 1})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        self._attr_is_on = False
        await self._tcp_client.control({"1": 0})


class CozyLifeLight(CozyLifeSwitchAsLight, RestoreEntity):
    """CozyLife RGB/CCT light."""

    _attr_brightness: int | None = None
    _attr_color_mode: ColorMode | None = None
    _attr_color_temp_kelvin: int | None = None
    _attr_hs_color: tuple[float, float] | None = None
    _attr_supported_features = LightEntityFeature.EFFECT | LightEntityFeature.TRANSITION
    _unrecorded_attributes = frozenset({"brightness", "color_temp_kelvin"})

    def __init__(self, tcp_client: tcp_client, hass, scenes: list[str]) -> None:
        """Initialize."""
        super().__init__(tcp_client, hass)
        self._scenes = scenes
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
        return True

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
        if self._cl is None:
            self._cl = self.hass.data.get(DATA_CIRCADIAN_LIGHTING)
            if self._cl is None:
                return None
        return self._cl._colortemp

    def calc_brightness(self) -> int | None:
        """Calculate circadian brightness."""
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
        if last_state and "last_effect" in last_state.attributes:
            self._effect = last_state.attributes["last_effect"]
        await self._refresh_state()

    async def async_set_effect(self, effect: str) -> None:
        """Set effect, applying it immediately if the light is on."""
        self._effect = effect
        if self._attr_is_on:
            await self.async_turn_on(effect=effect)

    async def async_update(self) -> None:
        """Poll device state, preserving natural effect behavior."""
        if self._attr_is_on and self._effect == "natural":
            await self.async_turn_on(effect="natural")
        else:
            await self._refresh_state()

    async def _refresh_state(self) -> None:
        """Query device and set attributes."""
        self._state = await self._tcp_client.query()
        if not self._state:
            return

        self._attr_is_on = self._state.get("1", 0) > 0

        if "4" in self._state:
            self._attr_brightness = int(self._state["4"] / 1000 * 255)

        mode = self._state.get("2", 0)
        if mode == 0:
            if (
                "3" in self._state
                and ColorMode.COLOR_TEMP in self._attr_supported_color_modes
            ):
                color_temp = self._state["3"]
                if color_temp < 60000:
                    self._attr_color_mode = ColorMode.COLOR_TEMP
                    self._attr_color_temp_kelvin = self._kelvin_from_device_temp(
                        color_temp
                    )
        elif mode == 1:
            if (
                "5" in self._state
                and "6" in self._state
                and ColorMode.HS in self._attr_supported_color_modes
            ):
                color = self._state["5"]
                if color < 60000:
                    self._attr_color_mode = ColorMode.HS
                    r, g, b = colorutil.color_hs_to_RGB(
                        round(self._state["5"]), round(self._state["6"] / 10)
                    )
                    self._attr_hs_color = colorutil.color_RGB_to_hs(r, g, b)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        colortemp_kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
        hs_color = kwargs.get(ATTR_HS_COLOR)
        transition = kwargs.get(ATTR_TRANSITION)
        effect = kwargs.get(ATTR_EFFECT)

        original_kelvin = self._attr_color_temp_kelvin or DEFAULT_MAX_KELVIN
        original_hs = self._attr_hs_color or (0, 0)
        original_brightness = self._attr_brightness if self._attr_is_on else 0

        self._attr_is_on = True
        self.async_write_ha_state()

        payload = {"1": 255, "2": 0}
        changed = 0

        if brightness is not None:
            self._effect = "manual"
            payload["4"] = round(brightness / 255 * 1000)
            self._attr_brightness = brightness
            changed += 1

        if (
            colortemp_kelvin is not None
            and ColorMode.COLOR_TEMP in self._attr_supported_color_modes
        ):
            self._effect = "manual"
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_color_temp_kelvin = colortemp_kelvin
            payload["3"] = self._device_temp_from_kelvin(colortemp_kelvin)
            changed += 1

        if hs_color is not None and ColorMode.HS in self._attr_supported_color_modes:
            self._effect = "manual"
            self._attr_color_mode = ColorMode.HS
            self._attr_hs_color = hs_color
            r, g, b = colorutil.color_hs_to_RGB(*hs_color)
            normalized_hs = colorutil.color_RGB_to_hs(r, g, b)
            payload["5"] = round(normalized_hs[0])
            payload["6"] = round(normalized_hs[1] * 10)
            changed += 1

        if changed == 0:
            if effect is not None:
                self._effect = effect

            if self._effect == "natural":
                payload["2"] = 0
                if CIRCADIAN_BRIGHTNESS:
                    brightness = self.calc_brightness()
                    colortemp_kelvin = self.calc_color_temp_kelvin()
                    if brightness is not None:
                        payload["4"] = round(brightness / 255 * 1000)
                        self._attr_brightness = brightness
                    if (
                        colortemp_kelvin is not None
                        and ColorMode.COLOR_TEMP in self._attr_supported_color_modes
                    ):
                        self._attr_color_mode = ColorMode.COLOR_TEMP
                        self._attr_color_temp_kelvin = colortemp_kelvin
                        payload["3"] = self._device_temp_from_kelvin(colortemp_kelvin)
                    if self._transitioning != 0:
                        return
                    if transition is None:
                        transition = 5
            elif self._effect == "sleep":
                payload["4"] = 12
                payload["3"] = 0
                self._attr_color_mode = ColorMode.COLOR_TEMP
                self._attr_brightness = round(12 / 1000 * 255)
                self._attr_color_temp_kelvin = DEFAULT_MIN_KELVIN
            elif self._effect == "study":
                payload["4"] = 1000
                payload["3"] = 1000
                self._attr_color_mode = ColorMode.COLOR_TEMP
                self._attr_brightness = 255
                self._attr_color_temp_kelvin = DEFAULT_MAX_KELVIN
            elif self._effect == "warm":
                payload["4"] = 1000
                payload["3"] = 0
                self._attr_color_mode = ColorMode.COLOR_TEMP
                self._attr_brightness = 255
                self._attr_color_temp_kelvin = DEFAULT_MIN_KELVIN
            elif self._effect == "chrismas":
                payload["2"] = 1
                payload["4"] = 1000
                payload["8"] = 500
                payload["7"] = (
                    "03000003E8FFFF007803E8FFFF00F003E8FFFF003C03E8FFFF00B4"
                    "03E8FFFF010E03E8FFFF002603E8FFFF"
                )

        if hs_color is not None or self._attr_color_mode == ColorMode.HS:
            payload["2"] = 1
        elif self._effect == "chrismas":
            payload["2"] = 1
        else:
            payload["2"] = 0

        self._transitioning = 0
        if transition:
            await self._transition_on(
                payload,
                transition,
                original_brightness or 0,
                original_kelvin,
                original_hs,
            )
        else:
            await self._tcp_client.control(payload)

        self._transitioning = 0

    async def _transition_on(
        self,
        payload: dict,
        transition: float,
        original_brightness: int,
        original_kelvin: int,
        original_hs: tuple[float, float],
    ) -> None:
        """Apply a smooth transition to the target payload."""
        self._transitioning = time.time()
        now = self._transitioning

        if self._effect == "chrismas":
            await self._tcp_client.control(payload)
            return

        payloadtemp = {"1": 255, "2": payload.get("2", 0)}
        p4steps = 0
        p4i = round(original_brightness / 255 * 1000)
        p4f = payload.get("4", p4i)
        if "4" in payload:
            p4steps = abs(round((p4i - p4f) / 4))

        if self._attr_color_mode == ColorMode.COLOR_TEMP:
            p3i = self._device_temp_from_kelvin(original_kelvin)
            p3f = payload.get("3", p3i)
            p3steps = abs(round((p3i - p3f) / 4)) if "3" in payload else 0
            steps = max(p3steps, p4steps)
            if steps <= 0:
                return
            stepseconds = max(transition / steps, MIN_INTERVAL)
            steps = max(1, round(transition / stepseconds))

            for step in range(1, steps + 1):
                if "4" in payload:
                    payloadtemp["4"] = round(p4i + (p4f - p4i) * step / steps)
                if "3" in payload:
                    payloadtemp["3"] = round(p3i + (p3f - p3i) * step / steps)
                if now != self._transitioning:
                    return
                await self._tcp_client.control(payloadtemp)
                if step < steps:
                    await asyncio.sleep(stepseconds)

        elif self._attr_color_mode == ColorMode.HS:
            p5i = original_hs[0]
            p6i = original_hs[1] * 10
            p5f = payload.get("5", p5i)
            p6f = payload.get("6", p6i)
            p5steps = abs(round((p5i - p5f) / 3)) if "5" in payload else 0
            p6steps = abs(round((p6i - p6f) / 10)) if "6" in payload else 0
            steps = max(p4steps, p5steps, p6steps)
            if steps <= 0:
                return
            stepseconds = transition / steps
            if stepseconds < MIN_INTERVAL:
                stepseconds = MIN_INTERVAL
                steps = max(1, round(transition / stepseconds))

            for step in range(1, steps + 1):
                if "4" in payload:
                    payloadtemp["4"] = round(p4i + (p4f - p4i) * step / steps)
                if "5" in payload:
                    payloadtemp["5"] = round(p5i + (p5f - p5i) * step / steps)
                    payloadtemp["6"] = round(p6i + (p6f - p6i) * step / steps)
                if now != self._transitioning:
                    return
                await self._tcp_client.control(payloadtemp)
                if step < steps:
                    await asyncio.sleep(stepseconds)
        else:
            await self._tcp_client.control(payload)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        self._transitioning = 0
        self._attr_is_on = False
        self.async_write_ha_state()

        transition = kwargs.get(ATTR_TRANSITION)
        original_brightness = self._attr_brightness or 0
        if self._effect == "natural" and transition is None:
            transition = 5

        if not transition:
            await super().async_turn_off()
            return

        self._transitioning = time.time()
        now = self._transitioning
        payloadtemp = {"1": 255, "2": 0}
        p4i = round(original_brightness / 255 * 1000)
        p4f = 0
        steps = abs(round((p4i - p4f) / 4))
        if steps <= 0:
            self._transitioning = 0
            await super().async_turn_off()
            return

        stepseconds = max(transition / steps, MIN_INTERVAL)
        steps = max(1, round(transition / stepseconds))
        for step in range(1, steps + 1):
            payloadtemp["4"] = round(p4i + (p4f - p4i) * step / steps)
            if now != self._transitioning:
                return
            await self._tcp_client.control(payloadtemp)
            if step < steps:
                await asyncio.sleep(stepseconds)
            else:
                await super().async_turn_off()

        self._transitioning = 0
