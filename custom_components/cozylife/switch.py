"""Platform for sensor integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.switch import PLATFORM_SCHEMA, SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .tcp_client import tcp_client

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional("switches", default=[]): vol.All(cv.ensure_list, [dict]),
        vol.Optional("switches2", default=[]): vol.All(cv.ensure_list, [dict]),
        vol.Optional("optimistic", default=False): cv.boolean,
    }
)

SCAN_INTERVAL = timedelta(seconds=10)

_LOGGER = logging.getLogger(__name__)
_LOGGER.info(__name__)

# One lock per physical device (DPID '1' is a shared bitmask register),
# so we must serialize query/control across both rockers.
_DEVICE_LOCKS: dict[str, asyncio.Lock] = {}


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_devices: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the sensor platform."""
    # We only want this platform to be set up via discovery.
    # logging.info('setup_platform', hass, config, add_entities, discovery_info)
    _LOGGER.info("setup_platform")
    # _LOGGER.info(f'ip={hass.data[DOMAIN]}')

    # if discovery_info is None:
    #    return

    switches = []
    optimistic = config.get("optimistic", False)
    for item in config.get("switches") or []:
        client = tcp_client(item.get("ip"))
        client._device_id = item.get("did")
        client._pid = item.get("pid")
        client._dpid = item.get("dpid")
        client._device_model_name = item.get("dmn")
        switches.append(CozyLifeSwitch(client, hass, "wippe1", optimistic))

    for item in config.get("switches2") or []:
        client = tcp_client(item.get("ip"))
        client._device_id = item.get("did")
        client._pid = item.get("pid")
        client._dpid = item.get("dpid")
        client._device_model_name = item.get("dmn")

        # Create two entities for each switch, one for each rocker
        switches.append(CozyLifeSwitch(client, hass, "wippe1", optimistic))
        switches.append(CozyLifeSwitch(client, hass, "wippe2", optimistic))

    async_add_devices(switches)
    # Connect each unique tcp_client only once (switches2 creates two entities sharing one client)
    unique_clients = {}
    for sw in switches:
        unique_clients[id(sw._tcp_client)] = sw._tcp_client

    for client in unique_clients.values():
        await client._connect()
        await client._device_info()
        await asyncio.sleep(0.01)

    async def async_update(now=None):
        # Refresh once per physical device and fan-out the same state to all its entities
        client_to_state: dict[int, dict[str, Any] | None] = {}

        # Query each unique client once
        for client in unique_clients.values():
            # Serialize query with the same lock used for control
            device_key = (
                getattr(client, "device_id", None)
                or getattr(client, "_device_id", None)
                or getattr(client, "ip", None)
            )
            lock = _DEVICE_LOCKS.setdefault(str(device_key), asyncio.Lock())
            async with lock:
                try:
                    client_to_state[id(client)] = await client.query()
                except Exception:
                    _LOGGER.exception("Failed to query CozyLife device %s", device_key)
                    client_to_state[id(client)] = None
            await asyncio.sleep(0.01)

        # Apply state to all entities sharing the same client
        for sw in switches:
            sw._apply_state(client_to_state.get(id(sw._tcp_client)))

    if not optimistic:
        async_track_time_interval(hass, async_update, SCAN_INTERVAL)


class CozyLifeSwitch(SwitchEntity):
    _tcp_client = None
    _attr_is_on = True
    _wippe = None  # Add a new attribute to track the rocker

    def __init__(
        self, tcp_client: tcp_client, hass, wippe: str, optimistic: bool = False
    ) -> None:
        """Initialize the sensor."""
        _LOGGER.info("__init__")
        self.hass = hass
        self._tcp_client = tcp_client
        self._unique_id = tcp_client.device_id + "_" + wippe
        self._name = tcp_client.device_id[-4:] + " " + wippe
        self._wippe = wippe  # Set the rocker attribute
        self._optimistic = optimistic
        self._state: dict[str, Any] | None = None

        # Shared lock across both rockers for the same physical device
        device_key = tcp_client.device_id
        self._lock = _DEVICE_LOCKS.setdefault(str(device_key), asyncio.Lock())

    @property
    def unique_id(self) -> str | None:
        """Return a unique ID."""
        return self._unique_id

    async def async_update(self):
        if not self._optimistic:
            await self._refresh_state()

    async def _refresh_state(self):
        async with self._lock:
            state = await self._tcp_client.query()
        self._apply_state(state)

    def _apply_state(self, state: dict[str, Any] | None) -> None:
        """Apply a device state payload to this entity (no I/O)."""
        self._state = state

        if not self._state or "1" not in self._state:
            return

        reg = self._state["1"]
        if self._wippe == "wippe1":
            self._attr_is_on = (reg & 0x01) == 0x01
        elif self._wippe == "wippe2":
            self._attr_is_on = (reg & 0x02) == 0x02

    # ---------------------------------------------------------------------
    # Helper: safely obtain current value of register '1'
    # ---------------------------------------------------------------------
    def _get_current_register_value(self) -> int:
        """Return the current value of register '1' or 0 if unavailable.

        We call _refresh_state if needed to avoid NoneType errors that were
        crashing automations when self._state was None.
        """
        # if self._state is None:
        #     self._refresh_state()  # Remove sync call in method

        if self._state and "1" in self._state:
            return self._state["1"]
        return 0

    @property
    def name(self) -> str:
        return "cozylife:" + self._name

    @property
    def available(self) -> bool:
        """Return if the device is available."""
        return self._tcp_client._writer is not None

    @property
    def is_on(self) -> bool:
        """Return True if entity is on."""
        return self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        async with self._lock:
            # Always refresh before read-modify-write on shared register '1'
            state = await self._tcp_client.query()
            self._apply_state(state)
            current = self._get_current_register_value()

            _LOGGER.info(
                "turn_on:%s  current=0x%02X  wippe=%s", kwargs, current, self._wippe
            )

            if self._wippe == "wippe1":
                new_val = current | 0x01
            else:  # wippe2
                new_val = current | 0x02

            await self._tcp_client.control({"1": new_val})

            # Update local cached register to avoid stale next operations
            if self._state is None:
                self._state = {}
            self._state["1"] = new_val

        # Optimistically set state flag (actual bit will be re-applied on next refresh)
        self._attr_is_on = True

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        async with self._lock:
            # Always refresh before read-modify-write on shared register '1'
            state = await self._tcp_client.query()
            self._apply_state(state)
            current = self._get_current_register_value()

            _LOGGER.info("turn_off  current=0x%02X  wippe=%s", current, self._wippe)

            if self._wippe == "wippe1":
                new_val = current & ~0x01
            else:  # wippe2
                new_val = current & ~0x02

            await self._tcp_client.control({"1": new_val})

            if self._state is None:
                self._state = {}
            self._state["1"] = new_val

        self._attr_is_on = False
