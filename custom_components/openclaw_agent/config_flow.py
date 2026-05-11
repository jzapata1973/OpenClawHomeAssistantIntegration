"""Config and Options flow for OpenClaw Agent (native)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import OpenClawAgentClient, OpenClawAgentError
from .const import (
    CONF_AGENT_NAME,
    CONF_EXTRA_INSTRUCTIONS,
    CONF_GATEWAY_TOKEN,
    CONF_GATEWAY_URL,
    CONF_INCLUDE_INSTRUCTIONS,
    CONF_SESSION_KEY,
    DEFAULT_AGENT_NAME,
    DEFAULT_GATEWAY_URL,
    DEFAULT_INCLUDE_INSTRUCTIONS,
    DEFAULT_SESSION_KEY,
    DOMAIN,
)


def _user_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_GATEWAY_URL,
                default=d.get(CONF_GATEWAY_URL, DEFAULT_GATEWAY_URL),
            ): str,
            vol.Required(CONF_GATEWAY_TOKEN, default=d.get(CONF_GATEWAY_TOKEN, "")): str,
            vol.Optional(
                CONF_AGENT_NAME, default=d.get(CONF_AGENT_NAME, DEFAULT_AGENT_NAME)
            ): str,
            vol.Optional(
                CONF_SESSION_KEY,
                default=d.get(CONF_SESSION_KEY, DEFAULT_SESSION_KEY),
            ): str,
            vol.Optional(
                CONF_INCLUDE_INSTRUCTIONS,
                default=d.get(CONF_INCLUDE_INSTRUCTIONS, DEFAULT_INCLUDE_INSTRUCTIONS),
            ): bool,
        }
    )


class OpenClawAgentConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            client = OpenClawAgentClient(
                gateway_url=user_input[CONF_GATEWAY_URL],
                token=user_input[CONF_GATEWAY_TOKEN],
                session=session,
            )
            agent_name = user_input.get(CONF_AGENT_NAME, DEFAULT_AGENT_NAME)
            session_key = user_input.get(CONF_SESSION_KEY, DEFAULT_SESSION_KEY)

            # Probe the endpoint with a tiny no-op request. We don't keep
            # the chain head from this — we just want to fail fast with a
            # helpful error if the gateway, token, or agent are wrong.
            try:
                await client.async_send(
                    model=f"openclaw/{agent_name}",
                    input_text="ping",
                    user=session_key,
                )
            except OpenClawAgentError as err:
                msg = str(err).lower()
                if "401" in msg or "403" in msg:
                    errors["base"] = "invalid_auth"
                elif "404" in msg or "html" in msg or "openresponses" in msg:
                    errors["base"] = "responses_disabled"
                else:
                    errors["base"] = "cannot_connect"

            if not errors:
                # Use a stable unique_id so the user cannot create two
                # entries pointing at the same gateway+session_key combo.
                await self.async_set_unique_id(
                    f"{user_input[CONF_GATEWAY_URL]}::{session_key}"
                )
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"OpenClaw · {agent_name}",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> "OpenClawAgentOptionsFlow":
        return OpenClawAgentOptionsFlow(config_entry)


class OpenClawAgentOptionsFlow(OptionsFlowWithReload):
    """Allow tweaking agent / session_key / instructions after setup."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        opts = self._entry.options
        data = self._entry.data

        def _d(key: str, fallback):
            if key in opts:
                return opts[key]
            return data.get(key, fallback)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_AGENT_NAME, default=_d(CONF_AGENT_NAME, DEFAULT_AGENT_NAME)
                ): str,
                vol.Optional(
                    CONF_SESSION_KEY,
                    default=_d(CONF_SESSION_KEY, DEFAULT_SESSION_KEY),
                ): str,
                vol.Optional(
                    CONF_INCLUDE_INSTRUCTIONS,
                    default=_d(CONF_INCLUDE_INSTRUCTIONS, DEFAULT_INCLUDE_INSTRUCTIONS),
                ): bool,
                vol.Optional(
                    CONF_EXTRA_INSTRUCTIONS, default=_d(CONF_EXTRA_INSTRUCTIONS, "")
                ): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
