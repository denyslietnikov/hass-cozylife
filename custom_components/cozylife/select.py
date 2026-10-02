"""Optional device configuration, exposed only for advertised DPIDs."""

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_TYPE_CODE, DOMAIN, SWITCH_TYPE_CODE
from .coordinator import CozyLifeCoordinator

SETTINGS = {
    "18": (
        "power_on_state",
        "Power-on state",
        {"off": 0, "on": 1, "previous_state": 2},
    ),
    "19": ("led_status", "Indicator LED", {"off": 0, "state": 1, "locator": 2}),
}


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for device in data["devices"]:
        if device.get(CONF_DEVICE_TYPE_CODE) != SWITCH_TYPE_CODE:
            continue
        dpids = {str(dpid) for dpid in device.get("dpid", [])}
        for dpid in sorted(SETTINGS.keys() & dpids):
            entities.append(CozyLifeSetting(data["coordinators"][device["did"]], dpid))
    async_add_entities(entities)


class CozyLifeSetting(CoordinatorEntity[CozyLifeCoordinator], SelectEntity):
    """Use acknowledged commands; never alter settings during setup."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    _attr_has_entity_name = True

    def __init__(self, coordinator, dpid):
        super().__init__(coordinator, context=dpid)
        self._dpid = dpid
        suffix, _name, self._values = SETTINGS[dpid]
        self._attr_translation_key = suffix
        self._attr_unique_id = f"{coordinator.client.device_id}_{suffix}"
        self._attr_options = list(self._values)

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
    def current_option(self):
        value = (self.coordinator.data or {}).get(self._dpid)
        return next(
            (
                name
                for name, raw in self._values.items()
                if type(value) is int and value == raw
            ),
            None,
        )

    async def async_select_option(self, option):
        if option not in self._values:
            raise HomeAssistantError("Invalid CozyLife setting")
        await self.coordinator.async_control({self._dpid: self._values[option]})
