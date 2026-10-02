import asyncio
import json
import logging
import time
from pathlib import Path

_LOGGER = logging.getLogger(__name__)


def get_sn() -> str:
    """
    message sn
    :return: str
    """
    return str(int(round(time.time() * 1000)))


# cache get_pid_list result for many calls
_CACHE_PID = []


def _extract_pid_list(pid_list: dict) -> list:
    """Extract product model list from a CozyLife API response."""
    if pid_list.get("ret") is None or pid_list["ret"] != "1":
        _LOGGER.info("get_pid_list.result is not as expected")
        return []

    info = pid_list.get("info")
    if (
        info is None
        or not isinstance(info, dict)
        or info.get("list") is None
        or not isinstance(info["list"], list)
    ):
        _LOGGER.info("get_pid_list.result structure is not as expected")
        return []

    return info["list"]


def _get_bundled_pid_list() -> list:
    """Load model metadata without requiring the vendor cloud."""
    try:
        model_path = Path(__file__).with_name("model.json")
        return _extract_pid_list(json.loads(model_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as err:
        _LOGGER.error("Error loading bundled model.json: %s", err)
        return []


async def get_pid_list(lang="en") -> list:
    """Cache the offline catalog; keep the language argument for CLI compatibility."""
    global _CACHE_PID
    if len(_CACHE_PID) != 0:
        return _CACHE_PID

    _CACHE_PID = await asyncio.to_thread(_get_bundled_pid_list)
    _CACHE_PID.extend(
        [
            {
                "device_type_code": "00",
                "device_model": [
                    {
                        "device_product_id": "e5aHVS",
                        "device_model_name": "Smart switch",
                        "icon": None,
                        "dpid": [1, 2, 3, 4, 5, 18, 19, 20],
                    }
                ],
            },
            {
                "device_type_code": "01",
                "device_model": [
                    {
                        "device_product_id": "o0mmpn",
                        "device_model_name": "Smart led Strip",
                        "icon": None,
                        "dpid": [1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14],
                    }
                ],
            },
        ]
    )
    return _CACHE_PID
