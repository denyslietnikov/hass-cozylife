"""Platform for sensor integration."""
from __future__ import annotations
import logging
from .tcp_client import tcp_client
from datetime import timedelta
import asyncio

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.event import async_track_time_interval

from typing import Any, Final, Literal, TypedDict, final
from .const import (
    DOMAIN,
    SWITCH_TYPE_CODE,
    LIGHT_TYPE_CODE,
    LIGHT_DPID,
    SWITCH,
    WORK_MODE,
    TEMP,
    BRIGHT,
    HUE,
    SAT,
)

SCAN_INTERVAL = timedelta(seconds=20)

_LOGGER = logging.getLogger(__name__)
_LOGGER.info(__name__)

SCAN_INTERVAL = timedelta(seconds=10)

async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_devices: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None
) -> None:
    """Set up the sensor platform."""
    # We only want this platform to be set up via discovery.
    # logging.info('setup_platform', hass, config, add_entities, discovery_info)
    _LOGGER.info('setup_platform')
    #_LOGGER.info(f'ip={hass.data[DOMAIN]}')
    
    #if discovery_info is None:
    #    return

    switches = []
    for item in config.get('switches') or []:
        client = tcp_client(item.get('ip'))
        client._device_id = item.get('did')
        client._pid = item.get('pid')
        client._dpid = item.get('dpid')
        client._device_model_name = item.get('dmn')
        switches.append(CozyLifeSwitch(client, hass, 'wippe1'))

    for item in config.get('switches2') or []:
        client = tcp_client(item.get('ip'))
        client._device_id = item.get('did')
        client._pid = item.get('pid')
        client._dpid = item.get('dpid')
        client._device_model_name = item.get('dmn')

        # Create two entities for each switch, one for each rocker
        switches.append(CozyLifeSwitch(client, hass, 'wippe1'))
        switches.append(CozyLifeSwitch(client, hass, 'wippe2'))
        
    async_add_devices(switches)
    for switch in switches:
        await hass.async_add_executor_job(switch._tcp_client._initSocket)
        await asyncio.sleep(0.01)

    async def async_update(now=None):
        for switch in switches:
            await hass.async_add_executor_job(switch._refresh_state)
            await asyncio.sleep(0.01)
    async_track_time_interval(hass, async_update, SCAN_INTERVAL)

class CozyLifeSwitch(SwitchEntity):
    _tcp_client = None
    _attr_is_on = True
    _wippe = None  # Add a new attribute to track the rocker

    def __init__(self, tcp_client: tcp_client, hass, wippe: str) -> None:
        """Initialize the sensor."""
        _LOGGER.info('__init__')
        self.hass = hass
        self._tcp_client = tcp_client
        self._unique_id = tcp_client.device_id + '_' + wippe
        self._name = tcp_client.device_id[-4:] + ' ' + wippe
        self._wippe = wippe  # Set the rocker attribute
        self._refresh_state()

    @property
    def unique_id(self) -> str | None:
        """Return a unique ID."""
        return self._unique_id
        
    async def async_update(self):
        await self.hass.async_add_executor_job(self._refresh_state)

    def _refresh_state(self):
        self._state = self._tcp_client.query()
        # _LOGGER.info(f'_name={self._name},_state={self._state}')
        if self._state:
            if self._wippe == 'wippe1':
                self._attr_is_on = (self._state['1'] & 0x01) == 0x01
            elif self._wippe == 'wippe2':
                self._attr_is_on = (self._state['1'] & 0x02) == 0x02

    # ---------------------------------------------------------------------
    # Helper: safely obtain current value of register '1'
    # ---------------------------------------------------------------------
    def _get_current_register_value(self) -> int:
        """Return the current value of register '1' or 0 if unavailable.

        We call _refresh_state if needed to avoid NoneType errors that were
        crashing automations when self._state was None.
        """
        if self._state is None:
            self._refresh_state()

        if self._state and '1' in self._state:
            return self._state['1']
        return 0
    
    @property
    def name(self) -> str:
        return 'cozylife:' + self._name
    
    @property
    def available(self) -> bool:
        """Return if the device is available."""
        if self._tcp_client._connect:
            return True
        else:
            return False
    
    @property
    def is_on(self) -> bool:
        """Return True if entity is on."""
        return self._attr_is_on
    
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        # Ensure we have the latest state to avoid NoneType errors
        current = self._get_current_register_value()
        _LOGGER.info("turn_on:%s  current=0x%02X  wippe=%s", kwargs, current, self._wippe)

        if self._wippe == 'wippe1':
            new_val = current | 0x01
        else:  # wippe2
            new_val = current | 0x02

        # Send the command through the executor thread
        await self.hass.async_add_executor_job(self._tcp_client.control, {'1': new_val})

        # Optimistically update the local state flag
        self._attr_is_on = True
    
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        # Ensure we have the latest state to avoid NoneType errors
        current = self._get_current_register_value()
        _LOGGER.info("turn_off  current=0x%02X  wippe=%s", current, self._wippe)

        if self._wippe == 'wippe1':
            new_val = current & ~0x01
        else:  # wippe2
            new_val = current & ~0x02

        # Send the command through the executor thread
        await self.hass.async_add_executor_job(self._tcp_client.control, {'1': new_val})

        # Optimistically update the local state flag
        self._attr_is_on = False