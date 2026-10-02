"""Download diagnostics, and every entity's name and icon (spec 2026-10-01-ha-ui, decisions 5 and 6)."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from homeassistant.core import HomeAssistant

from custom_components.rtls_at_home import binary_sensor, sensor
from custom_components.rtls_at_home.diagnostics import async_get_config_entry_diagnostics

from .test_ha_ui import reply, setup
from .test_meta import META, PROXY_MAC, SHOW_ADDR
from .test_runner_sensor import KEY, ROW

PKG = Path(__file__).parent.parent / "custom_components" / "rtls_at_home"


async def test_diagnostics_carry_the_meta_and_the_devices_without_token_or_addresses(hass: HomeAssistant,
                                                                                     aioclient_mock):
    """Diagnostics are for bug reports, which may be public: no token, no phone, tag or receiver address."""
    entry = await setup(hass, aioclient_mock, reply(dict(ROW, area="kitchen", status="present")))
    d = await async_get_config_entry_diagnostics(hass, entry)
    assert d["entry"]["data"]["token"] == "**REDACTED**" and d["entry"]["data"]["url"]
    assert d["runner"]["available"] is True and d["meta"]["rooms"] == META["rooms"]
    rx = d["meta"]["receivers"][0]
    assert rx["name"] == "Kitchen Proxy" and rx["mac"] == "**REDACTED**" and rx["alive"] is True
    (row,) = d["tracked"].values()
    assert row["room"] == "Kitchen" and row["area"] == "kitchen" and row["name"] == "Tag 1"
    text = json.dumps(d).lower()
    for secret in (KEY, PROXY_MAC, SHOW_ADDR):
        assert secret.lower() not in text


def _keys():
    """(platform, translation key) of every entity class that names itself by translation key."""
    out = set()
    for platform, module in (("sensor", sensor), ("binary_sensor", binary_sensor)):
        for _, cls in inspect.getmembers(module, inspect.isclass):
            key = cls.__dict__.get("_attr_translation_key")             # set by the class itself
            if cls.__module__ == module.__name__ and isinstance(key, str):
                out.add((platform, key))
    out |= {("sensor", k) for k in ("room", "floor", "location")}          # RtlsSensor sets its key per kind
    return out


def test_every_entity_has_a_name_and_an_icon_and_the_english_translation_matches():
    strings = json.loads((PKG / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((PKG / "icons.json").read_text(encoding="utf-8"))
    assert json.loads((PKG / "translations" / "en.json").read_text(encoding="utf-8")) == strings
    for platform, key in _keys():
        assert strings["entity"][platform][key]["name"], (platform, key)
        assert icons["entity"][platform][key]["default"].startswith("mdi:"), (platform, key)
