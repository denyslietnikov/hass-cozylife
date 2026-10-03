"""Config flow for CozyLife integration."""

from __future__ import annotations

import asyncio
import logging
import math
from ipaddress import IPv4Address

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback

from .const import (
    BRIGHT,
    CONF_DEFAULT_TRANSITION,
    CONF_DEVICE_TYPE_CODE,
    CONF_DEVICES,
    CONF_LIGHT_INTERVAL,
    CONF_SUBNET,
    CONF_SWITCH_INTERVAL,
    DEFAULT_LIGHT_INTERVAL,
    DEFAULT_SWITCH_INTERVAL,
    DEFAULT_TRANSITION_SECONDS,
    DOMAIN,
    LIGHT_TYPE_CODE,
    MAX_DEFAULT_TRANSITION_SECONDS,
    SUPPORT_DEVICE_CATEGORY,
    SWITCH_TYPE_CODE,
)
from .tcp_client import tcp_client

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("start_ip"): str,
        vol.Required("end_ip"): str,
    }
)

SCAN_CONCURRENCY = 32


def _get_subnet(ip: str) -> str:
    """Return the /24 subnet prefix for an IP address."""
    return ".".join(ip.split(".")[:3])


def _merge_device(existing: dict, incoming: dict) -> dict:
    """Merge imported metadata for the same physical device."""
    merged = {
        **existing,
        **{key: value for key, value in incoming.items() if value is not None},
    }
    if existing.get("name"):
        merged["name"] = existing["name"]
    merged["dpid"] = sorted(
        {int(dpid) for dpid in existing.get("dpid", []) + incoming.get("dpid", [])}
    )
    merged["rockers"] = max(existing.get("rockers", 1), incoming.get("rockers", 1))
    return merged


def _validate_range(
    start_ip: str, end_ip: str, subnet: str | None = None
) -> str | None:
    try:
        start, end = IPv4Address(start_ip), IPv4Address(end_ip)
    except ValueError:
        return "invalid_ip"
    if _get_subnet(start_ip) != _get_subnet(end_ip) or (
        subnet and _get_subnet(start_ip) != subnet
    ):
        return "different_subnet"
    if int(start) > int(end):
        return "invalid_range"
    return None


def _transition_seconds(value) -> float:
    """Accept finite durations, including zero for instant commands."""
    if isinstance(value, bool):
        raise vol.Invalid("Transition must be a number")
    try:
        seconds = float(value)
    except (ValueError, TypeError, OverflowError) as err:
        raise vol.Invalid("Transition must be a number") from err
    if not math.isfinite(seconds) or not 0 <= seconds <= MAX_DEFAULT_TRANSITION_SECONDS:
        raise vol.Invalid("Transition must be between 0 and 60 seconds")
    return seconds


class CozyLifeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for CozyLife."""

    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return CozyLifeOptionsFlow()

    @staticmethod
    async def _probe_device(ip: str) -> dict | None:
        """Probe a single device using the async TCP client."""
        client = tcp_client(ip, timeout=0.5)
        try:
            await client._connect(start_heartbeat=False)
            if not client.available:
                return None
            await client._device_info()
            if not client.device_id:
                return None
            if client.device_type_code not in SUPPORT_DEVICE_CATEGORY:
                return None
            return {
                "ip": ip,
                "did": client.device_id,
                "pid": client._pid,
                "dmn": client.device_model_name,
                "dpid": client.dpid,
                CONF_DEVICE_TYPE_CODE: client.device_type_code,
                "rockers": 2 if client._pid == "e5aHVS" else 1,
            }
        except Exception:
            _LOGGER.exception("Error probing CozyLife device at %s", ip)
            return None
        finally:
            await client.disconnect()

    @classmethod
    async def _scan_range(cls, start_ip: str, end_ip: str) -> list[dict]:
        """Scan an IP range and return discovered devices."""
        start_int = int(IPv4Address(start_ip))
        end_int = int(IPv4Address(end_ip))
        semaphore = asyncio.Semaphore(SCAN_CONCURRENCY)

        async def probe(ip_int: int) -> dict | None:
            async with semaphore:
                return await cls._probe_device(str(IPv4Address(ip_int)))

        results = await asyncio.gather(
            *(probe(ip_int) for ip_int in range(start_int, end_int + 1))
        )
        devices = []
        seen_dids = set()
        for result in results:
            if result is None or result["did"] in seen_dids:
                continue
            devices.append(result)
            seen_dids.add(result["did"])
        return devices

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Handle the user step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            start_ip = user_input["start_ip"].strip()
            end_ip = user_input["end_ip"].strip()

            try:
                start_addr = IPv4Address(start_ip)
                end_addr = IPv4Address(end_ip)
            except ValueError:
                errors["base"] = "invalid_ip"
                return self.async_show_form(
                    step_id="user",
                    data_schema=STEP_USER_DATA_SCHEMA,
                    errors=errors,
                )

            if _get_subnet(start_ip) != _get_subnet(end_ip):
                errors["base"] = "different_subnet"
                return self.async_show_form(
                    step_id="user",
                    data_schema=STEP_USER_DATA_SCHEMA,
                    errors=errors,
                )

            if int(start_addr) > int(end_addr):
                errors["base"] = "invalid_range"
                return self.async_show_form(
                    step_id="user",
                    data_schema=STEP_USER_DATA_SCHEMA,
                    errors=errors,
                )

            subnet = _get_subnet(start_ip)
            await self.async_set_unique_id(subnet)
            self._abort_if_unique_id_configured()

            devices = await self._scan_range(start_ip, end_ip)
            if not devices:
                errors["base"] = "cannot_connect"
                return self.async_show_form(
                    step_id="user",
                    data_schema=STEP_USER_DATA_SCHEMA,
                    errors=errors,
                )

            return self.async_create_entry(
                title=f"CozyLife Hub ({subnet}.0/24)",
                data={
                    CONF_SUBNET: subnet,
                    "start_ip": start_ip,
                    "end_ip": end_ip,
                    CONF_DEVICES: devices,
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_import(self, import_data: dict) -> ConfigFlowResult:
        """Import YAML platform configuration into a subnet hub."""
        subnet = _get_subnet(import_data["ip"])

        for entry in self._async_current_entries():
            if entry.data.get(CONF_SUBNET) != subnet:
                continue

            devices = list(entry.data.get(CONF_DEVICES, []))
            for index, device in enumerate(devices):
                if device["did"] == import_data["did"]:
                    merged_device = _merge_device(device, import_data)
                    if merged_device == device:
                        return self.async_abort(reason="already_configured")
                    devices[index] = merged_device
                    self.hass.config_entries.async_update_entry(
                        entry,
                        data={**entry.data, CONF_DEVICES: devices},
                    )
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(entry.entry_id)
                    )
                    return self.async_abort(reason="device_added_to_hub")

            devices.append(import_data)
            self.hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, CONF_DEVICES: devices},
            )
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(entry.entry_id)
            )
            return self.async_abort(reason="device_added_to_hub")

        await self.async_set_unique_id(subnet)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=f"CozyLife Hub ({subnet}.0/24)",
            data={
                CONF_SUBNET: subnet,
                "start_ip": f"{subnet}.1",
                "end_ip": f"{subnet}.254",
                CONF_DEVICES: [import_data],
            },
        )


class CozyLifeOptionsFlow(OptionsFlowWithReload):
    """Manage polling and hub devices without recreating their identities."""

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(
            step_id="init", menu_options=["settings", "rescan", "device"]
        )

    async def async_step_settings(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                values = {
                    key: vol.All(vol.Coerce(int), vol.Range(min=1, max=300))(
                        user_input[key]
                    )
                    for key in (CONF_SWITCH_INTERVAL, CONF_LIGHT_INTERVAL)
                }
            except (vol.Invalid, KeyError, TypeError):
                errors["base"] = "invalid_interval"
            else:
                return self.async_create_entry(
                    title="", data={**self.config_entry.options, **values}
                )
        return self.async_show_form(
            step_id="settings",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SWITCH_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_SWITCH_INTERVAL, DEFAULT_SWITCH_INTERVAL
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=300)),
                    vol.Required(
                        CONF_LIGHT_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_LIGHT_INTERVAL, DEFAULT_LIGHT_INTERVAL
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=300)),
                }
            ),
        )

    def _save_devices(self, devices: list[dict], **updates):
        data = {**self.config_entry.data, **updates, CONF_DEVICES: devices}
        if data != dict(self.config_entry.data):
            self.hass.config_entries.async_update_entry(self.config_entry, data=data)
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self.config_entry.entry_id)
            )
        # Options did not change, so OptionsFlowWithReload will not reload twice.
        return self.async_create_entry(title="", data=dict(self.config_entry.options))

    async def async_step_rescan(self, user_input=None):
        errors = {}
        if user_input is not None:
            start, end = user_input["start_ip"].strip(), user_input["end_ip"].strip()
            error = _validate_range(start, end, self.config_entry.data[CONF_SUBNET])
            if error:
                errors["base"] = error
            else:
                discovered = await CozyLifeConfigFlow._scan_range(start, end)
                if not discovered:
                    errors["base"] = "cannot_connect"
                else:
                    devices = {
                        device["did"]: dict(device)
                        for device in self.config_entry.data[CONF_DEVICES]
                    }
                    for device in discovered:
                        devices[device["did"]] = _merge_device(
                            devices.get(device["did"], {}), device
                        )
                    return self._save_devices(
                        list(devices.values()), start_ip=start, end_ip=end
                    )
        return self.async_show_form(
            step_id="rescan",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "start_ip", default=self.config_entry.data["start_ip"]
                    ): str,
                    vol.Required(
                        "end_ip", default=self.config_entry.data["end_ip"]
                    ): str,
                }
            ),
        )

    async def async_step_device(self, user_input=None):
        devices = self.config_entry.data[CONF_DEVICES]
        if user_input is not None:
            self._device_id = user_input["did"]
            return await self.async_step_edit_device()
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    vol.Required("did"): vol.In(
                        {
                            device[
                                "did"
                            ]: f"{device.get('dmn', device['did'])} ({device['ip']})"
                            for device in devices
                        }
                    )
                }
            ),
        )

    async def async_step_edit_device(self, user_input=None):
        devices = [dict(device) for device in self.config_entry.data[CONF_DEVICES]]
        device = next(device for device in devices if device["did"] == self._device_id)
        is_switch = device.get(CONF_DEVICE_TYPE_CODE) == SWITCH_TYPE_CODE
        supports_transition = (
            device.get(CONF_DEVICE_TYPE_CODE, LIGHT_TYPE_CODE) == LIGHT_TYPE_CODE
            and BRIGHT in {str(dpid) for dpid in device.get("dpid", [])}
            and "switch" not in device.get("dmn", "").lower()
        )
        errors = {}
        if user_input is not None:
            ip = user_input["ip"].strip()
            error = _validate_range(ip, ip, self.config_entry.data[CONF_SUBNET])
            if error:
                errors["base"] = error
            elif any(
                other["did"] != self._device_id and other["ip"] == ip
                for other in devices
            ):
                errors["base"] = "duplicate_ip"
            else:
                try:
                    if is_switch:
                        device["rockers"] = vol.All(
                            vol.Coerce(int), vol.Range(min=1, max=8)
                        )(user_input.get("rockers", device.get("rockers", 1)))
                    elif supports_transition:
                        device[CONF_DEFAULT_TRANSITION] = _transition_seconds(
                            user_input.get(
                                CONF_DEFAULT_TRANSITION,
                                device.get(
                                    CONF_DEFAULT_TRANSITION, DEFAULT_TRANSITION_SECONDS
                                ),
                            )
                        )
                except vol.Invalid:
                    errors["base"] = (
                        "invalid_rockers" if is_switch else "invalid_transition"
                    )
                else:
                    device["ip"] = ip
                    return self._save_devices(devices)
        schema = {vol.Required("ip", default=device["ip"]): str}
        if is_switch:
            schema[vol.Required("rockers", default=device.get("rockers", 1))] = vol.All(
                vol.Coerce(int), vol.Range(min=1, max=8)
            )
        elif supports_transition:
            schema[
                vol.Required(
                    CONF_DEFAULT_TRANSITION,
                    default=device.get(
                        CONF_DEFAULT_TRANSITION, DEFAULT_TRANSITION_SECONDS
                    ),
                )
            ] = vol.All(
                vol.Coerce(float), vol.Range(min=0, max=MAX_DEFAULT_TRANSITION_SECONDS)
            )
        return self.async_show_form(
            step_id="edit_device", data_schema=vol.Schema(schema), errors=errors
        )
