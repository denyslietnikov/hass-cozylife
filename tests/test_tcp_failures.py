"""Transport regressions: bounded transactions and safe recovery."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.cozylife.tcp_client import tcp_client


def connected_client():
    client = tcp_client("127.0.0.1", timeout=0.03)
    client._heartbeat_enabled = False
    client._reader = MagicMock()
    client._reader.at_eof.return_value = False
    client._writer = MagicMock()
    client._writer.is_closing.return_value = False
    client._writer.drain = AsyncMock()
    client._writer.wait_closed = AsyncMock()
    return client


@pytest.mark.parametrize("failure", [ConnectionResetError(), b""])
async def test_reset_and_eof_close_stream(failure):
    client = connected_client()
    if isinstance(failure, Exception):
        client._reader.readline = AsyncMock(side_effect=failure)
    else:
        client._reader.readline = AsyncMock(return_value=failure)
    assert await client.query() is None
    assert not client.available
    assert client._writer is None


async def test_whole_request_has_one_deadline():
    client = connected_client()

    async def silent():
        await asyncio.sleep(10)

    client._reader.readline = AsyncMock(side_effect=silent)
    start = asyncio.get_running_loop().time()
    assert await client.query() is None
    assert asyncio.get_running_loop().time() - start < 0.15
    assert client._reader is None


async def test_connect_is_bounded(mocker):
    async def silent(*args, **kwargs):
        await asyncio.sleep(10)

    mocker.patch("asyncio.open_connection", side_effect=silent)
    client = tcp_client("127.0.0.1", timeout=0.03)
    client._heartbeat_enabled = False
    start = asyncio.get_running_loop().time()
    assert await client.query() is None
    assert asyncio.get_running_loop().time() - start < 0.15
    assert not client.available


async def test_serial_matching_is_exact(mocker):
    client = connected_client()
    mocker.patch("custom_components.cozylife.tcp_client.get_sn", return_value="123")
    good = {"sn": "123", "res": 0, "msg": {"data": {"1": 2}}}
    client._reader.readline = AsyncMock(
        side_effect=[
            b"not json\r\n",
            b"[]\r\n",
            json.dumps({**good, "sn": "9123"}).encode() + b"\r\n",
            json.dumps(good).encode() + b"\r\n",
        ]
    )
    assert await client.query() == {"1": 2}
    await client.disconnect()


async def test_serials_unique_within_millisecond(mocker):
    client = tcp_client("127.0.0.1")
    mocker.patch("custom_components.cozylife.tcp_client.get_sn", return_value="123")
    assert json.loads(client._get_package(2, {}))["sn"] == "123"
    assert json.loads(client._get_package(2, {}))["sn"] == "124"


async def test_negative_ack_does_not_return_state(mocker):
    client = tcp_client("127.0.0.1")
    mocker.patch.object(
        client, "_send_and_read", return_value={"res": 1, "msg": {"data": {"1": 0}}}
    )
    assert await client.query() is None
    assert not await client.control({"1": 1})


async def test_probe_does_not_start_heartbeat(mock_device):
    _, host, port = mock_device
    client = tcp_client(host)
    client._port = port
    await client._connect(start_heartbeat=False)
    await client.query()
    assert client._heartbeat_task is None
    await client.disconnect()


async def test_reconnect_after_peer_closes(mock_device):
    device, host, port = mock_device
    client = tcp_client(host)
    client._port = port
    client._heartbeat_enabled = False
    await client.query()
    for writer in list(device.writers):
        writer.close()
        await writer.wait_closed()
    await asyncio.sleep(0.01)
    assert await client.query() == device.state
    await client.disconnect()


async def test_wrong_device_identity_blocks_control(mock_device):
    device, host, port = mock_device
    client = tcp_client(host)
    client._port = port
    client._heartbeat_enabled = False
    client._expected_device_id = "another_device"
    assert not await client.control({"1": 1})
    assert device.state["1"] == 0
    assert not client.available


@pytest.mark.parametrize(
    "type_code,expected", [("00", "00"), ("02", "01"), ("03", "03")]
)
async def test_unknown_pid_uses_advertised_type_and_dpids(
    mock_device, mocker, type_code, expected
):
    device, host, port = mock_device
    device.device_info.update(pid="unknown", dtp=type_code)
    mocker.patch("custom_components.cozylife.tcp_client.get_pid_list", return_value=[])
    client = tcp_client(host)
    client._port = port
    await client._connect(start_heartbeat=False)
    await client._device_info()
    assert client.device_type_code == expected
    assert client.dpid == [1, 2, 3, 4, 5, 6]
    await client.disconnect()
