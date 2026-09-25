"""The integration loads in Home Assistant."""

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from custom_components.rtls_at_home.const import DOMAIN


async def test_integration_is_discoverable(hass: HomeAssistant) -> None:
    integration = await async_get_integration(hass, DOMAIN)
    assert integration.name == "RTLS@Home"
    assert integration.config_flow is True
