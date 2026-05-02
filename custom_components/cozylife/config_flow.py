"""Config flow for CozyLife integration."""

from __future__ import annotations

import asyncio
import logging
from ipaddress import IPv4Address, ip_address

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_DEVICE_TYPE_CODE,
    CONF_DEVICES,
    CONF_SUBNET,
    DOMAIN,
    SUPPORT_DEVICE_CATEGORY,
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
    merged = {**existing, **incoming}
    merged["rockers"] = max(existing.get("rockers", 1), incoming.get("rockers", 1))
    return merged


class CozyLifeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for CozyLife."""

    VERSION = 2

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
                "rockers": 1,
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

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Handle the user step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            start_ip = user_input["start_ip"].strip()
            end_ip = user_input["end_ip"].strip()

            try:
                start_addr = ip_address(start_ip)
                end_addr = ip_address(end_ip)
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

    async def async_step_import(self, import_data: dict) -> FlowResult:
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
