# -*- coding: utf-8 -*-
"""Async TCP client for CozyLife devices."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import Any, Optional, Union

try:
    from .utils import get_pid_list, get_sn
except ImportError:
    from utils import get_pid_list, get_sn

CMD_INFO = 0
CMD_QUERY = 2
CMD_SET = 3
CMD_LIST = [CMD_INFO, CMD_QUERY, CMD_SET]
_LOGGER = logging.getLogger(__name__)


class tcp_client(object):
    """
    Represents a CozyLife TCP device.

    Protocol examples:
    send:{"cmd":0,"pv":0,"sn":"1636463553873","msg":{}}
    recv:{"cmd":0,"pv":0,"sn":"1636463553873","msg":{"did":"...","pid":"..."},"res":0}
    """

    _ip = str
    _port = 5555
    _reader: Optional[asyncio.StreamReader] = None
    _writer: Optional[asyncio.StreamWriter] = None

    _device_id = None
    _pid = None
    _device_type_code = None
    _icon = None
    _device_model_name = None
    _dpid = []
    _sn = None
    _heartbeat_task: Optional[asyncio.Task] = None

    def __init__(self, ip, timeout=3):
        self._ip = ip
        self.timeout = timeout
        self._heartbeat_task = None
        self._io_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()

    async def _close_writer(self) -> None:
        """Close the stream writer without touching heartbeat state."""
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer:
            with suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def disconnect(self) -> None:
        """Close the connection and stop heartbeat."""
        current_task = asyncio.current_task()
        heartbeat_task = self._heartbeat_task
        if (
            heartbeat_task
            and not heartbeat_task.done()
            and heartbeat_task is not current_task
        ):
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            self._heartbeat_task = None
        elif heartbeat_task is not current_task:
            self._heartbeat_task = None

        await self._close_writer()

    def __del__(self):
        if self._writer:
            self._writer.close()

    def _start_heartbeat(self) -> None:
        """Start the heartbeat task if not already running."""
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat())

    async def _connect(self, start_heartbeat: bool = True) -> None:
        """Connect to the device."""
        async with self._connect_lock:
            if self.available:
                if start_heartbeat:
                    self._start_heartbeat()
                return

            await self._close_writer()
            try:
                self._reader, self._writer = await asyncio.open_connection(
                    self._ip, self._port
                )
            except Exception as err:
                _LOGGER.info("_connect error, ip=%s: %s", self._ip, err)
                await self._close_writer()
            finally:
                if start_heartbeat:
                    self._start_heartbeat()

    async def _ensure_connected(self) -> bool:
        """Ensure device is connected, attempting reconnect if needed."""
        if self.available:
            return True

        _LOGGER.info("Ensuring connection for %s", self._ip)
        await self._connect()
        if self.available:
            _LOGGER.info("Reconnected to %s", self._ip)
            return True

        _LOGGER.warning("Failed to reconnect to %s", self._ip)
        return False

    async def _heartbeat(self) -> None:
        """Maintain connection and reconnect unavailable devices."""
        while True:
            await asyncio.sleep(30)
            try:
                await self._ping()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.info(
                    "Heartbeat failed for %s (%s), attempting reconnect",
                    self._ip,
                    err,
                )
                await self._connect()

    async def _read_response_for_sn(self, sn: str) -> dict | None:
        """Read responses until one matching the expected serial is found."""
        if self._reader is None:
            return None

        attempts = 10
        while attempts > 0:
            attempts -= 1
            try:
                response = await asyncio.wait_for(
                    self._reader.readline(), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                continue

            if not response:
                return None

            response_text = response.decode("utf-8", errors="ignore")
            if sn not in response_text:
                continue

            try:
                return json.loads(response.strip())
            except json.JSONDecodeError:
                _LOGGER.info("Failed to parse response from %s", self._ip)
                return None

        return None

    async def _send_and_read(self, cmd: int, payload: dict) -> dict | None:
        """Send a command and read the matching response."""
        if not await self._ensure_connected():
            return None
        if self._writer is None:
            return None

        package = self._get_package(cmd, payload)
        sn = self._sn
        try:
            self._writer.write(package)
            await self._writer.drain()
        except Exception:
            await self._close_writer()
            if not await self._ensure_connected() or self._writer is None:
                return None
            package = self._get_package(cmd, payload)
            sn = self._sn
            try:
                self._writer.write(package)
                await self._writer.drain()
            except Exception:
                await self._close_writer()
                return None

        return await self._read_response_for_sn(sn)

    async def _ping(self) -> None:
        """Send a ping to check connection and clear the matching response."""
        async with self._io_lock:
            response = await self._send_and_read(CMD_INFO, {})
            if response is None or response.get("res") != 0:
                raise ConnectionError("Ping failed")

    @property
    def check(self) -> bool:
        """Return whether this device is usable."""
        return True

    @property
    def dpid(self):
        return self._dpid

    @property
    def device_model_name(self):
        return self._device_model_name

    @property
    def icon(self):
        return self._icon

    @property
    def device_type_code(self) -> str:
        return self._device_type_code

    @property
    def device_id(self):
        return self._device_id

    @property
    def available(self) -> bool:
        """Return whether the TCP stream is currently open."""
        return self._writer is not None and not self._writer.is_closing()

    async def _device_info(self) -> None:
        """Fetch and cache device model information."""
        async with self._io_lock:
            response = await self._send_and_read(CMD_INFO, {})

        if response is None or not isinstance(response.get("msg"), dict):
            _LOGGER.info("_device_info.recv.error")
            return

        msg = response["msg"]
        if msg.get("did") is None or msg.get("pid") is None:
            _LOGGER.info("_device_info.recv.missing_fields")
            return

        self._device_id = msg["did"]
        self._pid = msg["pid"]

        pid_list = await get_pid_list()
        for item in pid_list:
            match = False
            for model in item["device_model"]:
                if model["device_product_id"] == self._pid:
                    match = True
                    self._icon = model["icon"]
                    self._device_model_name = model["device_model_name"]
                    self._dpid = model["dpid"]
                    break

            if match:
                self._device_type_code = item["device_type_code"]
                break

        _LOGGER.debug(
            "Device discovered: did=%s pid=%s type=%s model=%s",
            self._device_id,
            self._pid,
            self._device_type_code,
            self._device_model_name,
        )

    def _get_package(self, cmd: int, payload: dict) -> bytes:
        """Build a protocol package."""
        self._sn = get_sn()
        if CMD_SET == cmd:
            message = {
                "pv": 0,
                "cmd": cmd,
                "sn": self._sn,
                "msg": {
                    "attr": [int(item) for item in payload.keys()],
                    "data": payload,
                },
            }
        elif CMD_QUERY == cmd:
            message = {
                "pv": 0,
                "cmd": cmd,
                "sn": self._sn,
                "msg": {
                    "attr": [0],
                },
            }
        elif CMD_INFO == cmd:
            message = {"pv": 0, "cmd": cmd, "sn": self._sn, "msg": {}}
        else:
            raise ValueError("CMD is not valid")

        payload_str = json.dumps(message, separators=(",", ":"))
        return bytes(payload_str + "\r\n", encoding="utf8")

    async def _send_receiver(self, cmd: int, payload: dict) -> Union[dict, Any]:
        """Send a command and return response data."""
        async with self._io_lock:
            response = await self._send_and_read(cmd, payload)

        if response is None or not isinstance(response.get("msg"), dict):
            return None

        data = response["msg"].get("data")
        if not isinstance(data, dict):
            return None

        return data

    async def _only_send(self, cmd: int, payload: dict) -> None:
        """Send a command without reading a response."""
        async with self._io_lock:
            if not await self._ensure_connected() or self._writer is None:
                return
            try:
                self._writer.write(self._get_package(cmd, payload))
                await self._writer.drain()
            except Exception:
                await self._close_writer()

    async def _send_receive_ack(self, cmd: int, payload: dict) -> bool:
        """Send a command and return whether the device acknowledged it."""
        async with self._io_lock:
            response = await self._send_and_read(cmd, payload)

        if response is None:
            return False
        return response.get("res", -1) == 0

    async def control(self, payload: dict) -> bool:
        """Control device DPID values."""
        return await self._send_receive_ack(CMD_SET, payload)

    async def query(self) -> dict:
        """Query device state."""
        return await self._send_receiver(CMD_QUERY, {})
