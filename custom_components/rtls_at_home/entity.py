"""What every RTLS@Home entity shares: the Home Assistant device for one tracked device."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN

if TYPE_CHECKING:
    from .runner import BridgeRunner


def device_info(runner: BridgeRunner, key: str) -> DeviceInfo:
    row = runner.tracked.get(key, {})
    return DeviceInfo(identifiers={(DOMAIN, key)}, name=row.get("name") or key, manufacturer="RTLS@Home",
                      model="Tracked device")
