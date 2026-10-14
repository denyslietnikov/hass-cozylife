"""One poller and one command lock per physical device."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import LIGHT_COUNTDOWN, LIGHT_TYPE_CODE

_LOGGER = logging.getLogger(__name__)
LIGHT_OFF_RECOVERY_SECONDS = 5 * 60
TRANSPORT_ERRORS = {
    "TimeoutError",
    "ConnectionError",
    "ConnectionResetError",
    "ConnectionRefusedError",
    "ConnectionAbortedError",
    "BrokenPipeError",
    "OSError",
}


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
        self._light_off_recovery_until: float | None = None

    @property
    def light_off_recovery_pending(self) -> bool:
        """Return whether an interrupted light-off request is still queued."""
        return (
            self._light_off_recovery_until is not None
            and time.monotonic() < self._light_off_recovery_until
        )

    @callback
    def async_cancel_light_off_recovery(self) -> None:
        """A new power command supersedes an earlier interrupted fade-out."""
        self._light_off_recovery_until = None

    @callback
    def async_queue_light_off_recovery(self) -> bool:
        """Recover transport failures only, never rejected or wrong-device writes."""
        if (
            self.client.device_type_code != LIGHT_TYPE_CODE
            or self.client.last_error not in TRANSPORT_ERRORS
        ):
            return False
        self._light_off_recovery_until = time.monotonic() + LIGHT_OFF_RECOVERY_SECONDS
        return True

    async def _complete_light_off_recovery(self, state: dict) -> dict:
        """Reconcile the actual power state under the same lock as all commands."""
        if self._light_off_recovery_until is None:
            return state
        if time.monotonic() >= self._light_off_recovery_until:
            self.async_cancel_light_off_recovery()
            _LOGGER.warning("Expired interrupted light-off request for %s", self.name)
            return state
        if state["1"] == 0:
            self.async_cancel_light_off_recovery()
            return state
        if not await self.client.control({"1": 0}):
            reason = self.client.last_error or "no acknowledgement"
            if reason not in TRANSPORT_ERRORS:
                self.async_cancel_light_off_recovery()
            raise UpdateFailed(f"Unable to complete light-off request: {reason}")
        self.async_cancel_light_off_recovery()
        return {**state, "1": 0}

    async def _query(self) -> dict:
        state = await self.client.query()
        if state is None:
            reason = self.client.last_error or "no response"
            raise UpdateFailed(f"Unable to query device: {reason}")
        if (
            not isinstance(state, dict)
            or type(state.get("1")) is not int
            or not 0 <= state["1"] <= 255
        ):
            raise UpdateFailed("Invalid power state: DPID 1 must be an integer 0..255")
        optional_dpids = {"18", "19"}
        if self.client.device_type_code == LIGHT_TYPE_CODE and LIGHT_COUNTDOWN in {
            str(dpid) for dpid in self.client.dpid or []
        }:
            optional_dpids.add(LIGHT_COUNTDOWN)
        optional = sorted(
            {
                int(dpid)
                for dpid in self.async_contexts()
                if dpid in optional_dpids and dpid not in state
            }
        )
        if optional:
            settings = await self.client.query(optional)
            if isinstance(settings, dict):
                state = {
                    **state,
                    **{
                        key: value
                        for key, value in settings.items()
                        if key in {str(dpid) for dpid in optional}
                    },
                }
        return state

    async def _async_update_data(self) -> dict:
        async with self.lock:
            if self._retry_at > time.monotonic():
                raise UpdateFailed("Waiting before retrying unavailable device")
            try:
                state = await self._query()
                state = await self._complete_light_off_recovery(state)
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
            reason = self.client.last_error or "no acknowledgement"
            self.async_set_update_error(
                UpdateFailed(f"Device did not acknowledge command: {reason}")
            )
            raise HomeAssistantError(
                f"CozyLife device did not acknowledge command: {reason}"
            )
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
