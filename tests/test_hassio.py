"""The RTLS@Home App announces itself (Supervisor discovery): the integration connects to it, or moves to it."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rtls_at_home.const import DOMAIN

APP_URL = "http://local-rtls-at-home:8765"
INFO = HassioServiceInfo(config={"host": "local-rtls-at-home", "port": 8765, "token": "app-t0ken"},
                         name="RTLS@Home", slug="local_rtls_at_home", uuid="0123456789abcdef")


async def _discover(hass, info=INFO):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_HASSIO},
                                                     data=info)


async def test_with_no_entry_discovery_connects_to_the_app(hass: HomeAssistant, aioclient_mock) -> None:
    result = await _discover(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "hassio_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"url": APP_URL, "token": "app-t0ken"} and result["title"] == "RTLS@Home (App)"
    assert result["result"].unique_id == APP_URL


async def test_an_entry_for_another_engine_moves_to_the_app_in_place(hass: HomeAssistant, aioclient_mock) -> None:
    old = MockConfigEntry(domain=DOMAIN, unique_id="http://192.0.2.10:8765", title="RTLS@Home (192.0.2.10)",
                          data={"url": "http://192.0.2.10:8765", "token": "old"}, options={"poll_interval": 0.49})
    old.add_to_hass(hass)
    result = await _discover(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "hassio_move"
    assert result["description_placeholders"]["current"] == "http://192.0.2.10:8765"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "moved"
    entries = hass.config_entries.async_entries(DOMAIN)
    assert [e.entry_id for e in entries] == [old.entry_id]                       # the same entry: entities keep ids
    assert old.data == {"url": APP_URL, "token": "app-t0ken"} and old.unique_id == APP_URL
    assert old.options == {"poll_interval": 0.49} and old.title == "RTLS@Home (App)"


async def test_the_apps_entry_takes_a_new_token_without_asking(hass: HomeAssistant, aioclient_mock) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=APP_URL, title="RTLS@Home (App)",
                            data={"url": APP_URL, "token": "older"})
    entry.add_to_hass(hass)
    result = await _discover(hass)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert entry.data["token"] == "app-t0ken" and len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_a_malformed_announcement_is_refused(hass: HomeAssistant, aioclient_mock) -> None:
    for cfg in ({"host": "x", "port": 8765}, {"port": 8765, "token": "t"}, {"host": "x", "token": "t"}, {}):
        bad = HassioServiceInfo(config=cfg, name="RTLS@Home", slug="local_rtls_at_home", uuid="0")
        result = await _discover(hass, bad)
        assert result["type"] is FlowResultType.ABORT and result["reason"] == "invalid_discovery", cfg
