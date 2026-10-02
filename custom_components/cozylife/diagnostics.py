"""Read-only, redacted diagnostics; never trigger device commands."""

from homeassistant.components.diagnostics import async_redact_data

from .const import DOMAIN

TO_REDACT = {"ip", "did", "start_ip", "end_ip", "subnet", "unique_id", "name"}


async def async_get_config_entry_diagnostics(hass, entry):
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    devices = []
    for device in entry.data.get("devices", []):
        coordinator = runtime.get("coordinators", {}).get(device["did"])
        client = runtime.get("clients", {}).get(device["did"])
        devices.append(
            {
                "metadata": dict(device),
                "connected": client.available if client else False,
                "last_error": client.last_error if client else None,
                "last_update_success": (
                    coordinator.last_update_success if coordinator else False
                ),
                "poll_interval": (
                    coordinator.update_interval.total_seconds() if coordinator else None
                ),
                "state": coordinator.data if coordinator else None,
            }
        )
    return async_redact_data(
        {"options": dict(entry.options), "devices": devices}, TO_REDACT
    )
