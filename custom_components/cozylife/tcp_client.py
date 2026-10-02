# -*- coding: utf-8 -*-
"""Async TCP client for CozyLife devices."""

from __future__ import annotations

import asyncio
import json
import logging
import time
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
        self._reader = None
        self._writer = None
        self._dpid = []
        self._queried_dpids = []
        self._heartbeat_enabled = True
        self._last_response = 0.0
        self._last_serial = 0
        self.last_error = None
        self._expected_device_id = None
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
                await asyncio.wait_for(writer.wait_closed(), timeout=0.1)

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
        self._heartbeat_enabled = start_heartbeat
        async with self._connect_lock:
            if self.available:
                if start_heartbeat:
                    self._start_heartbeat()
                return

            await self._close_writer()
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self._ip, self._port), self.timeout
                )
            except (OSError, TimeoutError) as err:
                self.last_error = type(err).__name__
                _LOGGER.debug("Connect failed for %s: %s", self._ip, err)
                await self._close_writer()
            finally:
                if start_heartbeat:
                    self._start_heartbeat()

    async def _ensure_connected(self) -> bool:
        """Ensure device is connected, attempting reconnect if needed."""
        if self.available:
            return True

        _LOGGER.info("Ensuring connection for %s", self._ip)
        await self._connect(start_heartbeat=self._heartbeat_enabled)
        if self.available:
            if self._expected_device_id:
                info = await self._send_and_read(CMD_INFO, {})
                if (
                    not info
                    or info.get("res") != 0
                    or not isinstance(info.get("msg"), dict)
                    or info["msg"].get("did") != self._expected_device_id
                ):
                    self.last_error = "Device identity mismatch"
                    await self._close_writer()
                    return False
            _LOGGER.info("Reconnected to %s", self._ip)
            return True

        _LOGGER.debug("Failed to reconnect to %s", self._ip)
        return False

    async def _heartbeat(self) -> None:
        """Maintain connection and reconnect unavailable devices."""
        while True:
            await asyncio.sleep(30)
            if time.monotonic() - self._last_response < 30:
                continue
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

    async def _read_response_for_sn(
        self, sn: str, cmd: int | None = None
    ) -> dict | None:
        """Read responses until one matching the expected serial is found."""
        if self._reader is None:
            return None

        while True:
            response = await self._reader.readline()
            if not response:
                raise ConnectionError("Device closed the connection")
            try:
                message = json.loads(response)
            except (ValueError, UnicodeDecodeError):
                continue
            if (
                isinstance(message, dict)
                and str(message.get("sn")) == sn
                and (cmd is None or message.get("cmd", cmd) == cmd)
            ):
                return message

    async def _send_and_read(self, cmd: int, payload: dict) -> dict | None:
        """Send a command and read the matching response."""
        try:
            async with asyncio.timeout(self.timeout):
                if not await self._ensure_connected() or self._writer is None:
                    return None
                package = self._get_package(cmd, payload)
                self._writer.write(package)
                await self._writer.drain()
                response = await self._read_response_for_sn(self._sn, cmd)
                self._last_response = time.monotonic()
                self.last_error = None
                return response
        except asyncio.CancelledError:
            await self._close_writer()
            raise
        except (OSError, TimeoutError, ValueError) as err:
            self.last_error = type(err).__name__
            _LOGGER.debug("Command %s failed for %s: %s", cmd, self._ip, err)
            await self._close_writer()
            # A write may already have reached the device; never replay it blindly.
            return None

    async def _ping(self) -> None:
        """Send a ping to check connection and clear the matching response."""
        async with self._io_lock:
            response = await self._send_and_read(CMD_INFO, {})
            if response is None or response.get("res") != 0:
                await self._close_writer()
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
        return (
            self._writer is not None
            and not self._writer.is_closing()
            and self._reader is not None
            and not self._reader.at_eof()
        )

    async def _device_info(self) -> None:
        """Fetch and cache device model information."""
        async with self._io_lock:
            response = await self._send_and_read(CMD_INFO, {})

        if (
            response is None
            or response.get("res") != 0
            or not isinstance(response.get("msg"), dict)
        ):
            _LOGGER.info("_device_info.recv.error")
            return

        msg = response["msg"]
        if msg.get("did") is None or msg.get("pid") is None:
            _LOGGER.info("_device_info.recv.missing_fields")
            return

        self._device_id = msg["did"]
        self._pid = msg["pid"]
        if msg.get("dtp") is not None:
            self._device_type_code = str(msg["dtp"])

        pid_list = await get_pid_list()
        catalog_match = False
        for item in pid_list:
            match = False
            for model in item["device_model"]:
                if model["device_product_id"] == self._pid:
                    match = True
                    catalog_match = True
                    self._icon = model["icon"]
                    self._device_model_name = model["device_model_name"]
                    self._dpid = model["dpid"]
                    break

            if match:
                self._device_type_code = item["device_type_code"]
                break

        if not self._dpid or self._device_type_code is None:
            state = await self.query()
            if state:
                self._dpid = sorted(
                    set(self._queried_dpids)
                    | {int(key) for key in state if str(key).isdigit() and int(key) > 0}
                )
                # Type 02 can mean an RGB light, but catalogued motors also use it.
                if (
                    not catalog_match
                    and self._device_type_code == "02"
                    and ({3, 4} & set(self._dpid) or {5, 6} <= set(self._dpid))
                ):
                    self._device_type_code = "01"
        if not self._device_model_name:
            self._device_model_name = (
                "Smart Light" if self._device_type_code == "01" else "Smart Switch"
            )

        _LOGGER.debug(
            "Device discovered: did=%s pid=%s type=%s model=%s",
            self._device_id,
            self._pid,
            self._device_type_code,
            self._device_model_name,
        )

    def _get_package(self, cmd: int, payload: dict) -> bytes:
        """Build a protocol package."""
        self._last_serial = max(int(get_sn()), self._last_serial + 1)
        self._sn = str(self._last_serial)
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
                    "attr": [int(key) for key in payload] or [0],
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

        if (
            response is None
            or response.get("res") != 0
            or not isinstance(response.get("msg"), dict)
        ):
            return None

        data = response["msg"].get("data")
        if not isinstance(data, dict):
            return None
        attributes = response["msg"].get("attr", [])
        if cmd == CMD_QUERY and isinstance(attributes, list):
            self._queried_dpids = [
                int(key) for key in attributes if str(key).isdigit() and int(key) > 0
            ]

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

    async def query(self, dpids: list[int] | None = None) -> dict | None:
        """Query device state."""
        return await self._send_receiver(CMD_QUERY, dict.fromkeys(dpids or []))
