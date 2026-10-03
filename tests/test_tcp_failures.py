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
    client._connect = AsyncMock()
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
    assert client.last_error == "Device rejected query"
    assert not await client.control({"1": 1})
    assert client.last_error == "Device rejected command"


@pytest.mark.parametrize(
    "error", ["ConnectionError", "ConnectionResetError", "BrokenPipeError"]
)
async def test_query_retries_broken_stream_once(mocker, error):
    client = tcp_client("127.0.0.1")

    async def send(cmd, payload):
        if send_mock.await_count == 1:
            client.last_error = error
            return None
        client.last_error = None
        return {"res": 0, "msg": {"data": {"1": 2}}}

    send_mock = mocker.patch.object(client, "_send_and_read", side_effect=send)
    assert await client.query([1, 18]) == {"1": 2}
    assert send_mock.await_count == 2
    assert all(
        call.args == (2, {1: None, 18: None}) for call in send_mock.await_args_list
    )
    assert client.last_error is None


async def test_query_retry_stops_after_second_failure(mocker):
    client = tcp_client("127.0.0.1")

    async def send(cmd, payload):
        client.last_error = "ConnectionError"
        return None

    send_mock = mocker.patch.object(client, "_send_and_read", side_effect=send)
    assert await client.query() is None
    assert send_mock.await_count == 2


@pytest.mark.parametrize(
    "error", ["TimeoutError", "ConnectionRefusedError", "Device identity mismatch"]
)
async def test_query_does_not_retry_outage_or_wrong_identity(mocker, error):
    client = tcp_client("127.0.0.1")

    async def send(cmd, payload):
        client.last_error = error
        return None

    send_mock = mocker.patch.object(client, "_send_and_read", side_effect=send)
    assert await client.query() is None
    send_mock.assert_awaited_once()


async def test_ambiguous_write_is_never_replayed(mocker):
    client = tcp_client("127.0.0.1")

    async def send(cmd, payload):
        client.last_error = "ConnectionResetError"
        return None

    send_mock = mocker.patch.object(client, "_send_and_read", side_effect=send)
    assert not await client.control({"1": 1})
    send_mock.assert_awaited_once_with(3, {"1": 1})


@pytest.mark.parametrize(
    "response,error",
    [
        ({"res": 1}, "Device rejected query"),
        ({"res": 0}, "Invalid query response: missing msg"),
        ({"res": 0, "msg": {}}, "Invalid query response: missing data"),
    ],
)
async def test_invalid_query_response_is_reported_without_retry(
    mocker, response, error
):
    client = tcp_client("127.0.0.1")
    send_mock = mocker.patch.object(client, "_send_and_read", return_value=response)
    assert await client.query() is None
    assert client.last_error == error
    send_mock.assert_awaited_once()


async def test_query_recovers_after_eof_during_request(mock_device, mocker):
    device, host, port = mock_device
    client = tcp_client(host)
    client._port = port
    client._heartbeat_enabled = False
    client._expected_device_id = device.device_info["did"]
    await client.query()
    device.requests.clear()
    original_process = device.process_request

    async def close_once(request):
        if request["cmd"] == 2 and len(device.requests) == 1:
            for writer in list(device.writers):
                writer.close()
            return {}
        return await original_process(request)

    mocker.patch.object(device, "process_request", side_effect=close_once)
    try:
        assert await client.query() == device.state
        assert [request["cmd"] for request in device.requests] == [2, 0, 2]
        assert client.last_error is None
        assert client.available
    finally:
        await client.disconnect()


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


@pytest.mark.parametrize("failure", ["timeout", "eof", "nack", "missing_msg"])
async def test_identity_read_error_is_not_reported_as_wrong_device(mocker, failure):
    client = connected_client()
    reader, writer = client._reader, client._writer
    client._reader = None
    client._writer = None
    client._expected_device_id = "expected_device"

    async def reconnect(start_heartbeat):
        client._reader, client._writer = reader, writer

    async def respond():
        if failure == "timeout":
            await asyncio.sleep(10)
        if failure == "eof":
            return b""
        response = {"sn": client._sn, "cmd": 0, "res": 1 if failure == "nack" else 0}
        return json.dumps(response).encode() + b"\r\n"

    mocker.patch.object(client, "_connect", side_effect=reconnect)
    reader.readline = AsyncMock(side_effect=respond)
    assert not await client.control({"1": 1})
    assert (
        client.last_error
        == {
            "timeout": "TimeoutError",
            "eof": "ConnectionError",
            "nack": "Invalid device identity response",
            "missing_msg": "Invalid device identity response",
        }[failure]
    )
    assert not client.available
    assert [
        json.loads(call.args[0])["cmd"] for call in writer.write.call_args_list
    ] == [0]


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
