"""Config flow for RTLS@Home."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from yarl import URL

from .client import CannotConnect, EngineClient, InvalidAuth
from .const import (
    CONF_CENSUS_INTERVAL,
    CONF_POLL_INTERVAL,
    CONF_TOKEN,
    CONF_URL,
    DEFAULT_CENSUS_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    PROTOCOL_VERSION,
)


class RtlsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Connect Home Assistant to an RTLS@Home engine."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].strip().rstrip("/")
            await self.async_set_unique_id(url)
            self._abort_if_unique_id_configured()
            client = EngineClient(async_get_clientsession(self.hass), url, user_input[CONF_TOKEN])
            try:
                info = await client.hello()
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                if info.get("v") != PROTOCOL_VERSION:
                    errors["base"] = "unsupported_version"
                else:
                    return self.async_create_entry(
                        title=f"RTLS@Home ({URL(url).host})",
                        data={CONF_URL: url, CONF_TOKEN: user_input[CONF_TOKEN]},
                    )
        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default=(user_input or {}).get(CONF_URL, "")): str,
                vol.Required(CONF_TOKEN): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return RtlsOptionsFlow()


class RtlsOptionsFlow(OptionsFlow):
    """Poll and census intervals."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(CONF_POLL_INTERVAL, default=opts.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)): vol.All(
                    vol.Coerce(float), vol.Range(min=0.2, max=5.0)
                ),
                vol.Required(
                    CONF_CENSUS_INTERVAL, default=opts.get(CONF_CENSUS_INTERVAL, DEFAULT_CENSUS_INTERVAL)
                ): vol.All(vol.Coerce(float), vol.Range(min=2.0, max=300.0)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
