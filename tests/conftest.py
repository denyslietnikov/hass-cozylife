import pytest

from tests.mock_device import MockCozyLifeDevice


@pytest.fixture
async def mock_device():
    """Fixture that provides a running mock CozyLife device."""
    device = MockCozyLifeDevice()
    host, port = await device.start()

    yield device, host, port

    await device.stop()
