from unittest.mock import AsyncMock

import pytest

from custom_components.cozylife.tcp_client import tcp_client


@pytest.mark.asyncio
async def test_tcp_client_connect(mock_device):
    """Test connecting to mock device."""
    device, host, port = mock_device
    client = tcp_client(host, timeout=1.0)
    client._port = port

    # Test connection
    await client._connect()
    assert client._writer is not None
    assert client._reader is not None

    await client.disconnect()


@pytest.mark.asyncio
async def test_tcp_client_device_info(mock_device, mocker):
    """Test getting device info."""
    device, host, port = mock_device
    client = tcp_client(host, timeout=5.0)
    client._port = port

    # Mock get_pid_list to avoid network calls
    mock_pid_list = mocker.patch(
        "custom_components.cozylife.tcp_client.get_pid_list", new_callable=AsyncMock
    )
    mock_pid_list.return_value = [
        {
            "device_type_code": "01",
            "device_model": [
                {
                    "device_product_id": "p93sfg",
                    "icon": "https://example.com/icon.png",
                    "device_model_name": "Smart Bulb Light",
                    "dpid": [1, 2, 3, 4, 5, 6],
                }
            ],
        }
    ]

    await client._connect()
    await client._device_info()

    # Check that device info was set
    assert client._device_id == "mock_device_123"
    assert client._pid == "p93sfg"

    await client.disconnect()


@pytest.mark.asyncio
async def test_tcp_client_query(mock_device):
    """Test querying device state."""
    device, host, port = mock_device
    client = tcp_client(host, timeout=1.0)
    client._port = port

    await client._connect()
    state = await client.query()

    assert state is not None
    assert "1" in state  # switch state
    assert state["1"] == 0  # initial state

    await client.disconnect()


@pytest.mark.asyncio
async def test_tcp_client_control(mock_device):
    """Test controlling device."""
    device, host, port = mock_device
    client = tcp_client(host, timeout=1.0)
    client._port = port

    await client._connect()

    # Turn on
    result = await client.control({"1": 1})
    assert result is True

    # Query to verify
    state = await client.query()
    assert state["1"] == 1

    # Turn off
    result = await client.control({"1": 0})
    assert result is True

    # Query to verify
    state = await client.query()
    assert state["1"] == 0

    await client.disconnect()


@pytest.mark.asyncio
async def test_tcp_client_reconnect(mock_device):
    """Test reconnection after disconnect."""
    device, host, port = mock_device
    client = tcp_client(host, timeout=1.0)
    client._port = port

    # First connection
    await client._connect()
    assert client._writer is not None

    # Disconnect
    await client.disconnect()
    assert client._writer is None

    # Reconnect
    await client._connect()
    assert client._writer is not None

    await client.disconnect()


@pytest.mark.asyncio
async def test_tcp_client_available(mock_device):
    """Test available property."""
    device, host, port = mock_device
    client = tcp_client(host, timeout=1.0)
    client._port = port

    # Initially not connected
    assert not client.available

    await client._connect()
    assert client.available

    await client.disconnect()
    assert not client.available
