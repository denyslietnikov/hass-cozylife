"""The standalone scanner must not leave connections or heartbeats running."""

from unittest.mock import AsyncMock

import pytest

from custom_components.cozylife.tcp_client import tcp_client
from getconfig import scan_device


async def test_scan_closes_discovered_device(mock_device, mocker):
    device, host, port = mock_device
    client = tcp_client(host)
    client._port = port
    mocker.patch("getconfig.tcp_client", return_value=client)
    result = await scan_device(host)
    assert result is client
    assert result.device_id == device.device_info["did"]
    assert not client.available
    assert client._heartbeat_task is None


async def test_scan_closes_client_when_discovery_fails(mock_device, mocker):
    _, host, port = mock_device
    client = tcp_client(host)
    client._port = port
    client._device_info = AsyncMock(side_effect=RuntimeError("discovery failed"))
    mocker.patch("getconfig.tcp_client", return_value=client)
    with pytest.raises(RuntimeError, match="discovery failed"):
        await scan_device(host)
    assert not client.available
    assert client._heartbeat_task is None
