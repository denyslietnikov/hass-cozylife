import asyncio
import json
import logging
from typing import Any, Dict

_LOGGER = logging.getLogger(__name__)


class MockCozyLifeDevice:
    """Mock TCP server that simulates a CozyLife device."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.host = host
        self.port = port
        self.server = None
        self.state: Dict[str, Any] = {
            "1": 0,  # switch
            "2": 0,  # work mode
            "3": 500,  # color temp (0-1000)
            "4": 500,  # brightness (0-1000)
            "5": 0,  # hue
            "6": 0,  # saturation
        }
        self.device_info = {
            "did": "mock_device_123",
            "pid": "p93sfg",
            "dtp": "02",
            "mac": "mockmac123",
            "ip": host,
            "rssi": -30,
            "sv": "1.0.0",
            "hv": "0.0.1",
        }

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        """Handle incoming TCP connection."""
        addr = writer.get_extra_info("peername")
        _LOGGER.info(f"Connection from {addr}")

        try:
            while True:
                data = await reader.read(1024)
                if not data:
                    break

                message = data.decode("utf-8").strip()
                _LOGGER.debug(f"Received: {message}")

                try:
                    request = json.loads(message)
                    response = await self.process_request(request)
                    response_str = json.dumps(response, separators=(",", ":")) + "\r\n"
                    writer.write(response_str.encode("utf-8"))
                    await writer.drain()
                    _LOGGER.debug(f"Sent: {response_str.strip()}")
                except json.JSONDecodeError as e:
                    _LOGGER.error(f"Invalid JSON: {e}")
                    break
                except Exception as e:
                    _LOGGER.error(f"Error processing request: {e}")
                    break
        except Exception as e:
            _LOGGER.error(f"Connection error: {e}")
        finally:
            writer.close()
            await writer.wait_closed()
            _LOGGER.info(f"Connection closed for {addr}")

    async def process_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Process incoming request and return response."""
        cmd = request.get("cmd")
        sn = request.get("sn")
        msg = request.get("msg", {})

        if cmd == 0:  # CMD_INFO
            return {"cmd": 0, "pv": 0, "sn": sn, "msg": self.device_info, "res": 0}
        elif cmd == 2:  # CMD_QUERY
            return {
                "cmd": 2,
                "pv": 0,
                "sn": sn,
                "msg": {"attr": [1, 2, 3, 4, 5, 6], "data": self.state},
                "res": 0,
            }
        elif cmd == 3:  # CMD_SET
            # Update state with provided data
            data = msg.get("data", {})
            self.state.update(data)
            return {
                "cmd": 3,
                "pv": 0,
                "sn": sn,
                "msg": {"attr": list(data.keys()), "data": data},
                "res": 0,
            }
        elif cmd == 10:  # Status update after SET
            return {
                "cmd": 10,
                "pv": 0,
                "sn": sn,
                "msg": {"attr": [1, 2, 3, 4, 5, 6], "data": self.state},
                "res": 0,
            }
        else:
            return {"cmd": cmd, "pv": 0, "sn": sn, "res": 1}  # Error

    async def start(self):
        """Start the mock server."""
        self.server = await asyncio.start_server(
            self.handle_client, self.host, self.port
        )
        actual_host, actual_port = self.server.sockets[0].getsockname()
        self.port = actual_port
        _LOGGER.info(f"Mock CozyLife device listening on {actual_host}:{actual_port}")
        return actual_host, actual_port

    async def stop(self):
        """Stop the mock server."""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            _LOGGER.info("Mock CozyLife device stopped")

    def set_state(self, key: str, value: Any):
        """Set device state for testing."""
        self.state[key] = value

    def get_state(self, key: str) -> Any:
        """Get device state."""
        return self.state.get(key)
