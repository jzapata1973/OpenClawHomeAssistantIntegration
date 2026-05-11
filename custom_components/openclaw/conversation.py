"""OpenClaw conversation agent for Home Assistant Assist (v2.0.0+).

v2.0.0 — major rewrite. Drops the OpenAI-compatible
``/v1/chat/completions`` path (which has known unfixable bugs around
session+agent routing on the gateway side) and uses OpenClaw's native
``/v1/responses`` endpoint instead. This gives:

- Real routing to the configured agent (the gateway honors ``model``).
- Real per-session continuity managed server-side via
  ``previous_response_id`` chaining.
- Free reuse of the gateway's built-in memory layer (so the agent
  remembers facts about the user across HA restarts, separately from
  the chain).

The chain head (last seen ``response.id``) is persisted to disk so the
working context survives Home Assistant restarts.

The pre-v2 "Assist Session ID override" option is reused as the
``user``/session_key for OpenResponses — when set, all Assist
invocations share one persistent thread. When empty, a stable default
is generated from the entry id and agent name.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import re
from typing import Any

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import intent

from .api import OpenClawApiClient, OpenClawApiError
from .chain_store import ChainStore
from .const import (
    ATTR_MESSAGE,
    ATTR_MODEL,
    ATTR_SESSION_ID,
    ATTR_TIMESTAMP,
    CONF_ASSIST_SESSION_ID,
    CONF_AGENT_ID,
    CONF_INCLUDE_EXPOSED_CONTEXT,
    CONF_VOICE_AGENT_ID,
    DEFAULT_ASSIST_SESSION_ID,
    DEFAULT_AGENT_ID,
    DEFAULT_INCLUDE_EXPOSED_CONTEXT,
    DATA_MODEL,
    DOMAIN,
    EVENT_MESSAGE_RECEIVED,
)
from .coordinator import OpenClawCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register the conversation agent."""
    bag = hass.data[DOMAIN][entry.entry_id]
    chain_store: ChainStore = bag["chain_store"]
    agent = OpenClawConversationAgent(hass, entry, chain_store)
    conversation.async_set_agent(hass, entry, agent)


async def async_unload_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    conversation.async_unset_agent(hass, entry)
    return True


class OpenClawConversationAgent(conversation.AbstractConversationAgent):
    """Routes Assist conversations to OpenClaw via /v1/responses."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        chain_store: ChainStore,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._chain_store = chain_store

    @property
    def attribution(self) -> dict[str, str]:
        return {
            "name": "Powered by OpenClaw",
            "url": "https://openclaw.ai",
        }

    @property
    def supported_languages(self) -> list[str] | str:
        return conversation.MATCH_ALL

    def _opt(self, key: str, default: Any) -> Any:
        opts = self.entry.options
        data = self.entry.data
        if key in opts:
            return opts[key]
        return data.get(key, default)

    def _resolve_agent_name(self) -> str:
        """Pick the OpenClaw agent name to address."""
        voice_agent = self._opt(CONF_VOICE_AGENT_ID, "")
        configured = self._opt(CONF_AGENT_ID, DEFAULT_AGENT_ID)
        return (voice_agent or configured or DEFAULT_AGENT_ID).strip()

    def _resolve_session_key(self, agent_name: str) -> str:
        """Pick the OpenResponses ``user`` field (= our session key).

        Reuses the legacy "Assist session ID override" option when set,
        otherwise generates a stable per-entry default so the chain
        survives across Assist invocations + restarts.
        """
        configured = self._opt(CONF_ASSIST_SESSION_ID, DEFAULT_ASSIST_SESSION_ID)
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
        # Stable fallback when nothing was set in options. The entry_id
        # is opaque and won't change across HA restarts.
        return f"openclaw-{self.entry.entry_id[:8]}-{agent_name}"

    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        bag = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        if not bag:
            return self._error(user_input, "OpenClaw integration not configured")

        client: OpenClawApiClient = bag["client"]
        coordinator: OpenClawCoordinator = bag["coordinator"]

        agent_name = self._resolve_agent_name()
        session_key = self._resolve_session_key(agent_name)
        previous_response_id = self._chain_store.get_last(session_key)

        # Send instructions only on the first turn of a chain. After
        # that the gateway carries them server-side, saving tokens.
        instructions: str | None = None
        if previous_response_id is None:
            include_context = self._opt(
                CONF_INCLUDE_EXPOSED_CONTEXT, DEFAULT_INCLUDE_EXPOSED_CONTEXT
            )
            extra_system_prompt = getattr(user_input, "extra_system_prompt", None)
            base = (
                f"Sos el agente conversacional de Home Assistant "
                f'"{self.hass.config.location_name or "Home"}". '
                "Respondé en el idioma del usuario. "
                "Para leer estados o accionar dispositivos usá las tools "
                "del MCP de Home Assistant disponibles en este agente."
            ) if include_context else None
            parts = [p for p in (base, extra_system_prompt) if p]
            instructions = "\n\n".join(parts) if parts else None

        _LOGGER.debug(
            "openclaw v2 send | agent=%s | session_key=%s | "
            "previous_response_id=%s | instructions=%s",
            agent_name,
            session_key,
            previous_response_id,
            instructions is not None,
        )

        try:
            response = await client.async_send_responses(
                model=f"openclaw/{agent_name}",
                input_text=user_input.text,
                user=session_key,
                previous_response_id=previous_response_id,
                instructions=instructions,
            )
        except OpenClawApiError as err:
            _LOGGER.error("OpenClaw conversation error: %s", err)

            # Best-effort token refresh path inherited from v1.
            refresh_fn = bag.get("refresh_token")
            if refresh_fn:
                refreshed = await refresh_fn()
                if refreshed:
                    try:
                        response = await client.async_send_responses(
                            model=f"openclaw/{agent_name}",
                            input_text=user_input.text,
                            user=session_key,
                            previous_response_id=previous_response_id,
                            instructions=instructions,
                        )
                    except OpenClawApiError as retry_err:
                        return self._error(
                            user_input, f"Error talking to OpenClaw: {retry_err}"
                        )
                else:
                    return self._error(
                        user_input, f"Error talking to OpenClaw: {err}"
                    )
            else:
                return self._error(
                    user_input, f"Error talking to OpenClaw: {err}"
                )

        text = OpenClawApiClient.extract_responses_text(response) or ""
        new_response_id = response.get("id")
        if new_response_id:
            await self._chain_store.async_set_last(session_key, new_response_id)

        # Fire the legacy event so existing automations keep working.
        self.hass.bus.async_fire(
            EVENT_MESSAGE_RECEIVED,
            {
                ATTR_MESSAGE: text,
                ATTR_SESSION_ID: session_key,
                ATTR_MODEL: coordinator.data.get(DATA_MODEL) if coordinator.data else None,
                ATTR_TIMESTAMP: datetime.now(timezone.utc).isoformat(),
            },
        )
        coordinator.update_last_activity()

        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(text)
        return conversation.ConversationResult(
            response=intent_response,
            conversation_id=user_input.conversation_id or session_key,
            continue_conversation=self._should_continue(text),
        )

    @staticmethod
    def _should_continue(response: str) -> bool:
        """Heuristic from v1: should Voice PE auto-listen for a follow-up?"""
        if not response:
            return False
        text = response.strip()
        if re.search(r"\?\s*[\"'“”»)\]]*\s*$", text):
            return True
        lower = text.lower()
        for pattern in (
            "what do you think", "would you like", "do you want",
            "shall i", "should i", "can i help", "anything else", "let me know",
            "qué opinás", "querés que", "te ayudo", "algo más",
            "was meinst du", "möchtest du", "willst du", "soll ich",
            "kann ich", "noch etwas", "sonst noch",
        ):
            if pattern in lower:
                return True
        return False

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
