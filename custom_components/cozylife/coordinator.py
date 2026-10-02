"""One poller and one command lock per physical device."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)


class CozyLifeCoordinator(DataUpdateCoordinator[dict]):
    """Share device state between relays, lights and settings."""

    def __init__(self, hass, entry, client, interval: int):
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"CozyLife {client.device_id}",
            update_interval=timedelta(seconds=interval),
            always_update=False,
        )
        self.client = client
        self.lock = asyncio.Lock()
        self._failures = 0
        self._retry_at = 0.0

    async def _query(self) -> dict:
        state = await self.client.query()
        if (
            not isinstance(state, dict)
            or type(state.get("1")) is not int
            or not 0 <= state["1"] <= 255
        ):
            raise UpdateFailed("Device returned no valid relay state")
        optional = [
            int(dpid)
            for dpid in self.async_contexts()
            if dpid in ("18", "19") and dpid not in state
        ]
        if optional:
            settings = await self.client.query(optional)
            if isinstance(settings, dict):
                state = {
                    **state,
                    **{
                        key: value
                        for key, value in settings.items()
                        if key in ("18", "19")
                    },
                }
        return state

    async def _async_update_data(self) -> dict:
        async with self.lock:
            if self._retry_at > time.monotonic():
                raise UpdateFailed("Waiting before retrying unavailable device")
            try:
                state = await self._query()
            except UpdateFailed:
                self._failures += 1
                # Retry the first failure on the next poll, then back off to 60s.
                if self._failures > 1:
                    self._retry_at = time.monotonic() + min(
                        60, 5 * 2 ** min(self._failures - 2, 4)
                    )
                raise
            self._failures = 0
            self._retry_at = 0.0
            return state

    async def _control(self, payload: dict, state: dict) -> None:
        if not await self.client.control(payload):
            self.async_set_update_error(
                UpdateFailed("Device did not acknowledge command")
            )
            raise HomeAssistantError("CozyLife device did not acknowledge command")
        self._failures = 0
        self._retry_at = 0.0
        self.async_set_updated_data({**state, **payload})

    async def async_control(self, payload: dict) -> None:
        """Publish only acknowledged writes, serialized with polling."""
        async with self.lock:
            await self._control(payload, self.data or {})

    async def async_set_relay(self, mask: int, turn_on: bool) -> None:
        """Read-modify-write atomically; never guess the sibling relay state."""
        async with self.lock:
            try:
                state = await self._query()
            except UpdateFailed as err:
                self.async_set_update_error(err)
                raise HomeAssistantError("Cannot read CozyLife relay state") from err
            register = state["1"]
            if not 0 <= register <= 255:
                self.async_set_update_error(UpdateFailed("Invalid relay bitmask"))
                raise HomeAssistantError("Invalid CozyLife relay bitmask")
            value = register | mask if turn_on else register & ~mask
            await self._control({"1": value}, state)
