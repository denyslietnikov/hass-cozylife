import pytest

from tests.mock_device import MockCozyLifeDevice


@pytest.fixture
async def mock_device():
    """Fixture that provides a running mock CozyLife device."""
    device = MockCozyLifeDevice()
    host, port = await device.start()

    yield device, host, port

    await device.stop()


@pytest.fixture
async def hass(tmp_path, monkeypatch):
    """Use real HA core objects without requiring the upstream test plugin."""
    pytest.importorskip("homeassistant")
    from homeassistant import loader
    from homeassistant.bootstrap import async_load_base_functionality
    from homeassistant.config_entries import ConfigEntries
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.storage import Store

    # Exercise HA behavior in memory; persistence belongs to HA's own tests.
    monkeypatch.setattr(Store, "async_delay_save", lambda *args, **kwargs: None)

    instance = HomeAssistant(str(tmp_path))
    instance.config_entries = ConfigEntries(instance, {})
    loader.async_setup(instance)
    assert await async_load_base_functionality(instance)
    yield instance
    await instance.async_stop(force=True)


@pytest.fixture
async def entry(hass):
    from types import MappingProxyType

    from homeassistant.config_entries import ConfigEntry

    entry = ConfigEntry(
        domain="cozylife",
        title="CozyLife Hub",
        version=2,
        minor_version=1,
        unique_id="192.168.88",
        source="user",
        options={},
        discovery_keys=MappingProxyType({}),
        subentries_data=[],
        data={
            "subnet": "192.168.88",
            "start_ip": "192.168.88.1",
            "end_ip": "192.168.88.254",
            "devices": [
                {
                    "did": "switch_77f8",
                    "ip": "192.168.88.18",
                    "pid": "e5aHVS",
                    "dmn": "Smart switch",
                    "device_type_code": "00",
                    "rockers": 2,
                    "dpid": [1, 18, 19],
                }
            ],
        },
    )
    # Register without starting the integration or scheduling a storage write.
    hass.config_entries._entries[entry.entry_id] = entry
    return entry
