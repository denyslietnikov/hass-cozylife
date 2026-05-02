"""CozyLife switch platform."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.switch import PLATFORM_SCHEMA, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import CONF_DEVICE_TYPE_CODE, DOMAIN, SWITCH_TYPE_CODE
from .tcp_client import tcp_client

SWITCH_SCHEMA = vol.Schema(
    {
        vol.Required("ip"): cv.string,
        vol.Required("did"): cv.string,
        vol.Optional("dmn", default="Smart Switch"): cv.string,
        vol.Optional("name"): cv.string,
        vol.Optional("pid", default="p93sfg"): cv.string,
        vol.Optional("dpid", default=[1]): vol.All(cv.ensure_list, [int]),
    }
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional("switches", default=[]): vol.All(cv.ensure_list, [SWITCH_SCHEMA]),
        vol.Optional("switches2", default=[]): vol.All(cv.ensure_list, [SWITCH_SCHEMA]),
        vol.Optional("optimistic", default=False): cv.boolean,
    }
)

SCAN_INTERVAL = timedelta(seconds=240)

_LOGGER = logging.getLogger(__name__)

_DEVICE_LOCKS: dict[str, asyncio.Lock] = {}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up CozyLife switches from a hub config entry."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    clients = entry_data["clients"]
    devices = entry_data["devices"]

    entities: list[CozyLifeSwitch] = []
    for dev in devices:
        if dev.get(CONF_DEVICE_TYPE_CODE, "01") != SWITCH_TYPE_CODE:
            continue

        client = clients.get(dev["did"])
        if client is None:
            continue

        rockers = int(dev.get("rockers", 1))
        entities.append(CozyLifeSwitch(client, hass, "wippe1"))
        if rockers >= 2:
            entities.append(CozyLifeSwitch(client, hass, "wippe2"))

    if entities:
        async_add_entities(entities)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_devices: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Import YAML switch configuration as config entries."""
    _LOGGER.warning(
        "Configuration of CozyLife switches via YAML is deprecated. "
        "The YAML config will be imported as config entries."
    )

    for item in config.get("switches", []):
        await _import_switch(hass, item, rockers=1)

    for item in config.get("switches2", []):
        await _import_switch(hass, item, rockers=2)


async def _import_switch(hass: HomeAssistant, item: dict, rockers: int) -> None:
    """Import one YAML switch device."""
    import_data = {
        "ip": item["ip"],
        "did": item["did"],
        "pid": item.get("pid", "p93sfg"),
        "dmn": item.get("dmn", "Smart Switch"),
        "dpid": item.get("dpid", [1]),
        "name": item.get("name"),
        CONF_DEVICE_TYPE_CODE: SWITCH_TYPE_CODE,
        "rockers": rockers,
    }
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "import"},
            data=import_data,
        )
    )


class CozyLifeSwitch(SwitchEntity):
    """CozyLife switch entity with optional dual-rocker bitmask support."""

    _attr_is_on = True

    def __init__(self, tcp_client: tcp_client, hass, wippe: str) -> None:
        """Initialize."""
        self.hass = hass
        self._tcp_client = tcp_client
        self._wippe = wippe
        self._unique_id = f"{tcp_client.device_id}_{wippe}"
        base_name = getattr(tcp_client, "name", None) or tcp_client.device_id[-4:]
        self._name = f"{base_name} {wippe}"
        self._state: dict[str, Any] | None = None
        self._lock = _DEVICE_LOCKS.setdefault(str(tcp_client.device_id), asyncio.Lock())

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
        """Refresh state from the physical device."""
        async with self._lock:
            state = await self._tcp_client.query()
        self._apply_state(state)

    def _apply_state(self, state: dict[str, Any] | None) -> None:
        """Apply a device state payload to this entity."""
        self._state = state
        if not self._state or "1" not in self._state:
            return

        register = self._state["1"]
        if self._wippe == "wippe1":
            self._attr_is_on = (register & 0x01) == 0x01
        elif self._wippe == "wippe2":
            self._attr_is_on = (register & 0x02) == 0x02

    def _get_current_register_value(self) -> int:
        """Return the current value of register '1' or 0 if unavailable."""
        if self._state and "1" in self._state:
            return self._state["1"]
        return 0

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        async with self._lock:
            state = await self._tcp_client.query()
            self._apply_state(state)
            current = self._get_current_register_value()
            new_value = current | (0x01 if self._wippe == "wippe1" else 0x02)
            await self._tcp_client.control({"1": new_value})
            if self._state is None:
                self._state = {}
            self._state["1"] = new_value

        self._attr_is_on = True

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        async with self._lock:
            state = await self._tcp_client.query()
            self._apply_state(state)
            current = self._get_current_register_value()
            mask = 0x01 if self._wippe == "wippe1" else 0x02
            new_value = current & ~mask
            await self._tcp_client.control({"1": new_value})
            if self._state is None:
                self._state = {}
            self._state["1"] = new_value

        self._attr_is_on = False
