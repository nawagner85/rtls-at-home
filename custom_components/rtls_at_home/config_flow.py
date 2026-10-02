"""Config flow for RTLS@Home."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
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

APP_TITLE = "RTLS@Home (App)"


class RtlsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Connect Home Assistant to an RTLS@Home engine."""

    VERSION = 1

    _discovered: dict[str, str]

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

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """The RTLS@Home App announced itself (Supervisor discovery): connect to it, or move an entry to it."""
        cfg = discovery_info.config or {}
        host, port, token = cfg.get("host"), cfg.get("port"), cfg.get("token")
        if not host or not port or not token:
            return self.async_abort(reason="invalid_discovery")
        url = f"http://{host}:{port}"
        self._discovered = {CONF_URL: url, CONF_TOKEN: token}
        await self.async_set_unique_id(url)
        self._abort_if_unique_id_configured(updates={CONF_TOKEN: token})  # the App's entry: a new token, reloaded
        if self._async_current_entries(include_ignore=False):
            return await self.async_step_hassio_move()
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title=APP_TITLE, data=self._discovered)
        return self.async_show_form(step_id="hassio_confirm")

    async def async_step_hassio_move(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Move the existing entry to the App in place: the same entry, so every device and entity keeps its id."""
        entry = self._async_current_entries(include_ignore=False)[0]
        if user_input is not None:
            self.hass.config_entries.async_update_entry(entry, data={**entry.data, **self._discovered},
                                                        unique_id=self._discovered[CONF_URL], title=APP_TITLE)
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_abort(reason="moved")
        return self.async_show_form(step_id="hassio_move",
                                    description_placeholders={"current": entry.data.get(CONF_URL, "")})

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
