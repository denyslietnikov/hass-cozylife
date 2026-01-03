# -*- coding: utf-8 -*-
import asyncio
import json
import logging
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
    Represents a device
    send:{"cmd":0,"pv":0,"sn":"1636463553873","msg":{}}
    receiver:{"cmd":0,"pv":0,"sn":"1636463553873","msg":{"did":"629168597cb94c4c1d8f","dtp":"02","pid":"e2s64v",
    "mac":"7cb94c4c1d8f","ip":"192.168.123.57","rssi":-33,"sv":"1.0.0","hv":"0.0.1"},"res":0}

    send:{"cmd":2,"pv":0,"sn":"1636463611798","msg":{"attr":[0]}}
    receiver:{"cmd":2,"pv":0,"sn":"1636463611798","msg":{"attr":[1,2,3,4,5,6],"data":{"1":0,"2":0,"3":1000,"4":1000,
    "5":65535,"6":65535}},"res":0}

    send:{"cmd":3,"pv":0,"sn":"1636463662455","msg":{"attr":[1],"data":{"1":0}}}
    receiver:{"cmd":3,"pv":0,"sn":"1636463662455","msg":{"attr":[1],"data":{"1":0}},"res":0}
    receiver:{"cmd":10,"pv":0,"sn":"1636463664000","res":0,"msg":{"attr":[1,2,3,4,5,6],"data":{"1":0,"2":0,"3":1000,
    "4":1000,"5":65535,"6":65535}}}
    """

    _ip = str
    _port = 5555
    _reader: Optional[asyncio.StreamReader] = None
    _writer: Optional[asyncio.StreamWriter] = None

    _device_id = None  # str
    # _device_key = str
    _pid = None
    _device_type_code = None
    _icon = None
    _device_model_name = None
    _dpid = []
    # last sn
    _sn = None
    _heartbeat_task: Optional[asyncio.Task] = None

    def __init__(self, ip, timeout=3):
        self._ip = ip
        self.timeout = timeout
        self._heartbeat_task = None

    async def disconnect(self):
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None
        self._heartbeat_task = None

    def __del__(self):
        # Note: __del__ cannot be async, but we can close synchronously if needed
        if self._writer:
            self._writer.close()

    def _start_heartbeat(self):
        """Start the heartbeat task if not already running."""
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat())

    async def _ping(self) -> None:
        """Send a ping to check connection and clear response buffer."""
        # Send CMD_INFO and read a single response line to avoid buffer accumulation
        if not await self._ensure_connected():
            raise ConnectionError("Ping failed: not connected")
        self._writer.write(self._get_package(CMD_INFO, {}))
        await self._writer.drain()
        await asyncio.wait_for(self._reader.readline(), timeout=self.timeout)

    async def _heartbeat(self):
        """Heartbeat task to maintain connection."""
        while True:
            await asyncio.sleep(30)  # Check every 30 seconds
            if not self.available:
                _LOGGER.info(
                    f"Heartbeat: Connection not available for {self._ip}, attempting reconnect"
                )
                try:
                    await self._connect()
                    if self.available:
                        _LOGGER.info(f"Heartbeat: Reconnected to {self._ip}")
                    else:
                        _LOGGER.warning(f"Heartbeat: Failed to reconnect to {self._ip}")
                except Exception as e:
                    _LOGGER.warning(f"Heartbeat: Reconnect failed for {self._ip}: {e}")
                continue
            try:
                # Send a ping to verify connection is alive
                await self._ping()
            except Exception as e:
                _LOGGER.info(
                    f"Heartbeat: Ping failed for {self._ip} ({e}), attempting reconnect"
                )
                try:
                    await self._connect()
                    if self.available:
                        _LOGGER.info(f"Heartbeat: Reconnected to {self._ip}")
                    else:
                        _LOGGER.warning(f"Heartbeat: Failed to reconnect to {self._ip}")
                except Exception as e2:
                    _LOGGER.warning(f"Heartbeat: Reconnect failed for {self._ip}: {e2}")

    async def _ensure_connected(self):
        """Ensure device is connected, attempt reconnect if needed."""
        if not self.available:
            _LOGGER.info(f"Ensuring connection for {self._ip}")
            try:
                await self._connect()
                if self.available:
                    _LOGGER.info(f"Reconnected to {self._ip}")
                    # Start heartbeat if not running
                    self._start_heartbeat()
                else:
                    _LOGGER.warning(f"Failed to reconnect to {self._ip}")
                    return False
            except Exception as e:
                _LOGGER.warning(f"Reconnect failed for {self._ip}: {e}")
                return False
        return True

    async def _connect(self):
        try:
            self._reader, self._writer = await asyncio.open_connection(
                self._ip, self._port
            )
            # Start heartbeat after successful connection
            self._start_heartbeat()
        except Exception as e:
            _LOGGER.info(f"_connect error, ip={self._ip}: {e}")
            await self.disconnect()

    @property
    def check(self) -> bool:
        """
        Determine whether the device is filtered
        :return:
        """
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
        """Check if device is connected and available."""
        return self._writer is not None and not self._writer.is_closing()

    async def _device_info(self) -> None:
        """
        get info for device model
        :return:
        """
        if not await self._ensure_connected():
            return
        package = self._get_package(CMD_INFO, {})
        try:
            self._writer.write(package)
            await self._writer.drain()
        except Exception:
            try:
                await self.disconnect()
                await self._connect()
                if self._writer:
                    self._writer.write(package)
                    await self._writer.drain()
            except Exception:
                return

        try:
            resp = await asyncio.wait_for(self._reader.read(1024), timeout=self.timeout)
            if not resp:
                return
            resp_json = json.loads(resp.strip())
        except asyncio.TimeoutError:
            _LOGGER.info("_device_info: timeout")
            return
        except Exception:
            _LOGGER.info("_device_info.recv.error")
            return

        if resp_json.get("msg") is None or type(resp_json["msg"]) is not dict:
            _LOGGER.info("_device_info.recv.error1")
            return

        if resp_json["msg"].get("did") is None:
            _LOGGER.info("_device_info.recv.error2")
            return
        self._device_id = resp_json["msg"]["did"]

        if resp_json["msg"].get("pid") is None:
            _LOGGER.info("_device_info.recv.error3")
            return

        self._pid = resp_json["msg"]["pid"]

        pid_list = await get_pid_list()
        for item in pid_list:
            match = False
            for item1 in item["device_model"]:
                if item1["device_product_id"] == self._pid:
                    match = True
                    self._icon = item1["icon"]
                    self._device_model_name = item1["device_model_name"]
                    self._dpid = item1["dpid"]
                    break

            if match:
                self._device_type_code = item["device_type_code"]
                break

        # _LOGGER.info(pid_list)
        _LOGGER.info(self._device_id)
        _LOGGER.info(self._device_type_code)
        _LOGGER.info(self._pid)
        _LOGGER.info(self._device_model_name)
        _LOGGER.info(self._icon)

    def _get_package(self, cmd: int, payload: dict) -> bytes:
        """
        package message
        :param cmd:int:
        :param payload:
        :return:
        """
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
            raise Exception("CMD is not valid")

        payload_str = json.dumps(
            message,
            separators=(
                ",",
                ":",
            ),
        )
        # _LOGGER.info(f'_package={payload_str}')
        return bytes(payload_str + "\r\n", encoding="utf8")

    async def _send_receiver(self, cmd: int, payload: dict) -> Union[dict, Any]:
        """
        send & receiver
        :param cmd:
        :param payload:
        :return:
        """
        if not await self._ensure_connected():
            return None
        try:
            self._writer.write(self._get_package(cmd, payload))
            await self._writer.drain()
        except Exception:
            try:
                await self.disconnect()
                await self._connect()
                if self._writer:
                    self._writer.write(self._get_package(cmd, payload))
                    await self._writer.drain()
            except Exception:
                pass
        try:
            i = 10
            while i > 0:
                try:
                    res = await asyncio.wait_for(
                        self._reader.readline(), timeout=self.timeout
                    )
                except asyncio.TimeoutError:
                    i -= 1
                    continue
                # print(f'res={res},sn={self._sn},{self._sn in str(res)}')
                i -= 1
                # only allow same sn
                if self._sn in res.decode("utf-8"):
                    payload = json.loads(res.strip())
                    if payload is None or len(payload) == 0:
                        return None

                    if payload.get("msg") is None or type(payload["msg"]) is not dict:
                        return None

                    if (
                        payload["msg"].get("data") is None
                        or type(payload["msg"]["data"]) is not dict
                    ):
                        return None

                    return payload["msg"]["data"]

            return None

        except Exception as e:
            # print(f'e={e}')
            _LOGGER.info(f"_send_receiver.error:{e}")
            return None

    async def _only_send(self, cmd: int, payload: dict) -> None:
        """
        send but not receiver
        :param cmd:
        :param payload:
        :return:
        """
        if not await self._ensure_connected():
            return
        try:
            self._writer.write(self._get_package(cmd, payload))
            await self._writer.drain()
        except Exception:
            try:
                await self.disconnect()
                await self._connect()
                if self._writer:
                    self._writer.write(self._get_package(cmd, payload))
                    await self._writer.drain()
            except Exception:
                await self.disconnect()

    async def _send_receive_ack(self, cmd: int, payload: dict) -> bool:
        """
        send & receive ack (for commands that return simple ack)
        :param cmd:
        :param payload:
        :return:
        """
        if not await self._ensure_connected():
            return False
        try:
            self._writer.write(self._get_package(cmd, payload))
            await self._writer.drain()
        except Exception:
            try:
                await self.disconnect()
                await self._connect()
                if self._writer:
                    self._writer.write(self._get_package(cmd, payload))
                    await self._writer.drain()
            except Exception:
                pass
        try:
            i = 10
            while i > 0:
                try:
                    res = await asyncio.wait_for(
                        self._reader.readline(), timeout=self.timeout
                    )
                except asyncio.TimeoutError:
                    i -= 1
                    continue
                i -= 1
                # only allow same sn
                if self._sn in res.decode("utf-8"):
                    payload = json.loads(res.strip())
                    if payload is None or len(payload) == 0:
                        return False
                    # For SET command, just check that we got a response
                    return payload.get("res", -1) == 0
            return False
        except Exception as e:
            _LOGGER.info(f"_send_receive_ack.error:{e}")
            return False

    async def control(self, payload: dict) -> bool:
        """
        control use dpid
        :param payload:
        :return:
        """
        return await self._send_receive_ack(CMD_SET, payload)

    async def query(self) -> dict:
        """
        query device state
        :return:
        """
        return await self._send_receiver(CMD_QUERY, {})
