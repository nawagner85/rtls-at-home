"""Config and options flows."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rtls_at_home.const import CONF_CENSUS_INTERVAL, CONF_POLL_INTERVAL, DOMAIN

URL = "http://192.0.2.10:8765"
INPUT = {"url": URL + "/", "token": "t0ken"}


async def _start(hass):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def test_valid_engine_creates_entry(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/api/ingest/hello", json={"v": 1, "ok": True, "mode": "shadow"})
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"url": URL, "token": "t0ken"}
    assert result["title"] == "RTLS@Home (192.0.2.10)"
    assert aioclient_mock.mock_calls[0][3]["Authorization"] == "Bearer t0ken"


async def test_bad_token(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/api/ingest/hello", status=401)
    result = await hass.config_entries.flow.async_configure((await _start(hass))["flow_id"], INPUT)
    assert result["errors"] == {"base": "invalid_auth"}


async def test_unreachable(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/api/ingest/hello", exc=TimeoutError)
    result = await hass.config_entries.flow.async_configure((await _start(hass))["flow_id"], INPUT)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_wrong_protocol_version(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/api/ingest/hello", json={"v": 2, "ok": True})
    result = await hass.config_entries.flow.async_configure((await _start(hass))["flow_id"], INPUT)
    assert result["errors"] == {"base": "unsupported_version"}


async def test_same_engine_twice_aborts(hass: HomeAssistant, aioclient_mock) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=URL, data={"url": URL, "token": "x"}).add_to_hass(hass)
    aioclient_mock.get(f"{URL}/api/ingest/hello", json={"v": 1, "ok": True})
    result = await hass.config_entries.flow.async_configure((await _start(hass))["flow_id"], INPUT)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_options_flow(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=URL, data={"url": URL, "token": "x"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_POLL_INTERVAL: 0.3, CONF_CENSUS_INTERVAL: 20})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_POLL_INTERVAL: 0.3, CONF_CENSUS_INTERVAL: 20.0}
