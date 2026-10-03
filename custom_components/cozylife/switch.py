"""CozyLife switch platform."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.switch import PLATFORM_SCHEMA, SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_TYPE_CODE, DOMAIN, SWITCH_TYPE_CODE
from .coordinator import CozyLifeCoordinator

if TYPE_CHECKING:
    from .runtime import CozyLifeConfigEntry

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

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CozyLifeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up CozyLife switches from a hub config entry."""
    coordinators = entry.runtime_data.coordinators
    devices = entry.runtime_data.devices

    entities: list[CozyLifeSwitch] = []
    for dev in devices:
        if dev.get(CONF_DEVICE_TYPE_CODE, "01") != SWITCH_TYPE_CODE:
            continue

        coordinator = coordinators.get(dev["did"])
        if coordinator is None:
            continue

        rockers = int(dev.get("rockers", 1))
        for index in range(1, rockers + 1):
            entities.append(CozyLifeSwitch(coordinator, f"wippe{index}"))

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
    if config.get("optimistic"):
        _LOGGER.warning(
            "CozyLife ignores the legacy optimistic option. "
            "Polling remains enabled to track physical device changes; "
            "commands update state after acknowledgement."
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


class CozyLifeSwitch(CoordinatorEntity[CozyLifeCoordinator], SwitchEntity):
    """CozyLife switch entity with optional dual-rocker bitmask support."""

    def __init__(self, coordinator: CozyLifeCoordinator, wippe: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._tcp_client = client = coordinator.client
        self._wippe = wippe
        self._mask = 1 << (int(wippe.removeprefix("wippe")) - 1)
        self._unique_id = f"{client.device_id}_{wippe}"
        base_name = getattr(client, "name", None) or client.device_id[-4:]
        self._name = f"{base_name} {wippe}"

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
    def is_on(self) -> bool | None:
        """Derive every rocker from the same polled bitmask."""
        state = self.coordinator.data
        if not state or type(state.get("1")) is not int:
            return None
        return bool(state["1"] & self._mask)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        await self.coordinator.async_set_relay(self._mask, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        await self.coordinator.async_set_relay(self._mask, False)
