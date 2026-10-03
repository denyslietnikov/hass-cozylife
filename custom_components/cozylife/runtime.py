"""Per-hub runtime resources, owned by the config entry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .coordinator import CozyLifeCoordinator
    from .light import CozyLifeLight
    from .tcp_client import tcp_client

    CozyLifeConfigEntry = ConfigEntry["CozyLifeRuntimeData"]


@dataclass(slots=True)
class CozyLifeRuntimeData:
    """Keep device connections, pollers and light references together."""

    clients: dict[str, tcp_client]
    coordinators: dict[str, CozyLifeCoordinator]
    devices: list[dict]
    light_entities: list[CozyLifeLight] = field(default_factory=list)

    async def async_shutdown(self) -> None:
        """Stop polling before closing the device connections."""
        for coordinator in self.coordinators.values():
            await coordinator.async_shutdown()
        for client in self.clients.values():
            await client.disconnect()
        self.light_entities.clear()
