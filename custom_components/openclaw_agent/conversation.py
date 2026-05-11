"""HA conversation agent backed by OpenClaw's native /v1/responses endpoint."""

from __future__ import annotations

import logging

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .chain_store import ChainStore
from .client import OpenClawAgentClient, OpenClawAgentError
from .const import (
    CONF_AGENT_NAME,
    CONF_EXTRA_INSTRUCTIONS,
    CONF_INCLUDE_INSTRUCTIONS,
    CONF_SESSION_KEY,
    DEFAULT_AGENT_NAME,
    DEFAULT_INCLUDE_INSTRUCTIONS,
    DEFAULT_SESSION_KEY,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register the conversation agent."""
    bag = hass.data[DOMAIN][entry.entry_id]
    agent = OpenClawAgentConversation(
        hass, entry, bag["client"], bag["chain_store"]
    )
    conversation.async_set_agent(hass, entry, agent)


async def async_unload_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    conversation.async_unset_agent(hass, entry)
    return True


class OpenClawAgentConversation(conversation.AbstractConversationAgent):
    """Conversation agent that chats with one OpenClaw agent over /v1/responses.

    Routing strategy:
      - `model = openclaw/<agent_name>` selects the OpenClaw agent.
      - `user = <stable session_key>` makes the gateway derive a stable
        per-client session bucket.
      - On each turn we send `previous_response_id = <last response.id we saw>`
        so the gateway chains the conversation server-side. Combined with the
        gateway's built-in memory layer, this gives both short-term
        (chain) and long-term (memory) continuity — without HA having to
        ship the full message history each turn.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: OpenClawAgentClient,
        chain_store: ChainStore,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._client = client
        self._chain_store = chain_store

    @property
    def attribution(self) -> dict[str, str]:
        return {
            "name": "Powered by OpenClaw (native)",
            "url": "https://openclaw.ai",
        }

    @property
    def supported_languages(self) -> list[str] | str:
        return conversation.MATCH_ALL

    def _opt(self, key: str, default):
        opts = self.entry.options
        data = self.entry.data
        if key in opts:
            return opts[key]
        return data.get(key, default)

    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        agent_name = self._opt(CONF_AGENT_NAME, DEFAULT_AGENT_NAME)
        session_key = self._opt(CONF_SESSION_KEY, DEFAULT_SESSION_KEY)
        include_instructions = self._opt(
            CONF_INCLUDE_INSTRUCTIONS, DEFAULT_INCLUDE_INSTRUCTIONS
        )
        extra_instructions = self._opt(CONF_EXTRA_INSTRUCTIONS, "") or ""

        previous_response_id = self._chain_store.get_last(session_key)

        # The instructions field is honored only on the first turn of a chain
        # (when there is no previous_response_id). After that the gateway
        # carries the system context server-side as part of the chain.
        instructions: str | None = None
        if previous_response_id is None and include_instructions:
            base = (
                f"Sos el agente conversacional de Home Assistant "
                f"\"{self.hass.config.location_name or 'Home'}\". "
                "Respondé en el idioma del usuario. "
                "Si te pide accionar dispositivos o leer estados, usá las "
                "tools del MCP de Home Assistant."
            )
            instructions = (
                f"{base}\n\n{extra_instructions}".strip()
                if extra_instructions
                else base
            )

        _LOGGER.debug(
            "openclaw_agent send | agent=%s | session_key=%s | "
            "previous=%s | sending_instructions=%s",
            agent_name,
            session_key,
            previous_response_id,
            instructions is not None,
        )

        try:
            response = await self._client.async_send(
                model=f"openclaw/{agent_name}",
                input_text=user_input.text,
                user=session_key,
                previous_response_id=previous_response_id,
                instructions=instructions,
            )
        except OpenClawAgentError as err:
            _LOGGER.error("openclaw_agent error: %s", err)
            return self._error(user_input, str(err))

        text = OpenClawAgentClient.extract_text(response) or ""
        new_response_id = response.get("id")
        if new_response_id:
            await self._chain_store.async_set_last(session_key, new_response_id)
            _LOGGER.debug(
                "openclaw_agent chain advanced | session_key=%s | id=%s",
                session_key,
                new_response_id,
            )

        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(text)
        return conversation.ConversationResult(
            response=intent_response,
            conversation_id=user_input.conversation_id or session_key,
        )

    def _error(
        self,
        user_input: conversation.ConversationInput,
        message: str,
    ) -> conversation.ConversationResult:
        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_error(
            intent.IntentResponseErrorCode.UNKNOWN, message
        )
        return conversation.ConversationResult(
            response=intent_response,
            conversation_id=user_input.conversation_id,
        )
