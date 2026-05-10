"""OpenClaw conversation agent for Home Assistant Assist pipeline.

Registers OpenClaw as a native conversation agent so it can be used
with Assist, Voice PE, and any HA voice satellite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
from .const import (
    ATTR_MESSAGE,
    ATTR_MODEL,
    ATTR_SESSION_ID,
    ATTR_TIMESTAMP,
    CONF_ASSIST_SESSION_ID,
    CONF_AGENT_ID,
    CONF_CONTEXT_MAX_CHARS,
    CONF_CONTEXT_STRATEGY,
    CONF_INCLUDE_EXPOSED_CONTEXT,
    CONF_VOICE_AGENT_ID,
    DEFAULT_ASSIST_SESSION_ID,
    DEFAULT_AGENT_ID,
    DEFAULT_CONTEXT_MAX_CHARS,
    DEFAULT_CONTEXT_STRATEGY,
    DEFAULT_INCLUDE_EXPOSED_CONTEXT,
    DATA_MODEL,
    DOMAIN,
    EVENT_MESSAGE_RECEIVED,
)
from .coordinator import OpenClawCoordinator
from .exposure import apply_context_policy, build_exposed_entities_context
from .helpers import extract_text_recursive

_LOGGER = logging.getLogger(__name__)

_VOICE_REQUEST_HEADERS = {
    "x-openclaw-source": "voice",
    "x-ha-voice": "true",
    "x-openclaw-message-channel": "voice",
}

# v1.0.3: client-side message history per HA conversation_id.
# The OpenClaw gateway forces routing to its default agent whenever ANY
# session_id reference is present in the request — see CHANGELOG v1.0.2.
# So we cannot rely on gateway-side session memory for continuity. Instead
# we keep history in HA process memory and ship it as `messages[]` on each
# request, OpenAI-style. This also lets us skip the (heavy) entities
# system prompt on follow-up turns to save tokens.
HISTORY_MAX_TURNS = 20            # last 20 user+assistant pairs (40 messages)
SYSTEM_REFRESH_EVERY = 10         # re-inject system prompt every N turns
MAX_CACHED_CONVERSATIONS = 50     # LRU eviction threshold


@dataclass
class _ConversationState:
    """Per-conversation_id rolling state held in process memory."""

    messages: list[dict[str, str]] = field(default_factory=list)
    turns_since_system: int = 0
    last_accessed: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the OpenClaw conversation agent."""
    agent = OpenClawConversationAgent(hass, entry)
    conversation.async_set_agent(hass, entry, agent)


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Unload the conversation agent."""
    conversation.async_unset_agent(hass, entry)
    return True


class OpenClawConversationAgent(conversation.AbstractConversationAgent):
    """Conversation agent that routes messages through OpenClaw.

    Enables OpenClaw to appear as a selectable agent in the Assist pipeline,
    allowing use with Voice PE, satellites, and the built-in HA Assist dialog.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the conversation agent."""
        self.hass = hass
        self.entry = entry
        # v1.0.3: in-memory chat history per HA conversation_id
        self._conversations: dict[str, _ConversationState] = {}

    @property
    def attribution(self) -> dict[str, str]:
        """Return attribution info."""
        return {"name": "Powered by OpenClaw", "url": "https://openclaw.dev"}

    @property
    def supported_languages(self) -> list[str] | str:
        """Return supported languages.

        OpenClaw handles language via its configured model, so we declare
        support for all languages and let the model handle translation.
        """
        return conversation.MATCH_ALL

    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        """Process a user message through OpenClaw.

        Tries streaming first for lower latency (first-token fast).
        Falls back to non-streaming if the stream yields nothing.

        Args:
            user_input: The conversation input from HA Assist.

        Returns:
            ConversationResult with the assistant's response.
        """
        entry_data = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        if not entry_data:
            return self._error_result(
                user_input, "OpenClaw integration not configured"
            )

        client: OpenClawApiClient = entry_data["client"]
        coordinator: OpenClawCoordinator = entry_data["coordinator"]

        message = user_input.text
        assistant_id = "conversation"
        options = self.entry.options
        voice_agent_id = self._normalize_optional_text(
            options.get(CONF_VOICE_AGENT_ID)
        )
        configured_agent_id = self._normalize_optional_text(
            options.get(
                CONF_AGENT_ID,
                self.entry.data.get(CONF_AGENT_ID, DEFAULT_AGENT_ID),
            )
        )
        resolved_agent_id = voice_agent_id or configured_agent_id
        conversation_id = self._resolve_conversation_id(user_input, resolved_agent_id)
        active_model = self._normalize_optional_text(options.get("active_model"))

        # Upstream issues #8, #24, #28: the gateway routes by `model` in the
        # OpenAI-compatible payload, not by the `x-openclaw-agent-id` header.
        # When no explicit model is selected via the active_model entity,
        # derive `openclaw/<agent_id>` so the configured agent actually
        # receives the request instead of the gateway default ("main").
        raw_active_model = options.get("active_model")
        if not active_model and resolved_agent_id and resolved_agent_id != DEFAULT_AGENT_ID:
            active_model = f"openclaw/{resolved_agent_id}"

        _LOGGER.debug(
            "OpenClaw routing | options.active_model=%r | "
            "voice_agent_id=%r | configured_agent_id=%r | resolved_agent_id=%r | "
            "final model sent=%r | conversation_id=%r",
            raw_active_model,
            voice_agent_id,
            configured_agent_id,
            resolved_agent_id,
            active_model,
            conversation_id,
        )
        include_context = options.get(
            CONF_INCLUDE_EXPOSED_CONTEXT,
            DEFAULT_INCLUDE_EXPOSED_CONTEXT,
        )
        max_chars = int(options.get(CONF_CONTEXT_MAX_CHARS, DEFAULT_CONTEXT_MAX_CHARS))
        strategy = options.get(CONF_CONTEXT_STRATEGY, DEFAULT_CONTEXT_STRATEGY)

        # v1.0.3: cache lookup / create. The user's "Assist Session ID" override
        # makes all Assist conversations share the same conversation_id, so this
        # cache effectively gives a single rolling context until HA restart.
        state = self._get_or_create_state(conversation_id)

        # Decide whether to inject the (heavy) entities system prompt this turn.
        # Skipping it on follow-ups is the main token-saver of v1.0.3.
        should_inject_system = (
            not state.messages
            or state.turns_since_system >= SYSTEM_REFRESH_EVERY
        )

        system_prompt: str | None = None
        if should_inject_system:
            raw_context = (
                build_exposed_entities_context(
                    self.hass,
                    assistant=assistant_id,
                )
                if include_context
                else None
            )
            exposed_context = apply_context_policy(raw_context, max_chars, strategy)
            extra_system_prompt = getattr(user_input, "extra_system_prompt", None)
            system_prompt = "\n\n".join(
                part for part in (exposed_context, extra_system_prompt) if part
            ) or None

        # History to ship with this request (excludes any stored system messages).
        history_to_send = [m for m in state.messages if m["role"] != "system"]

        try:
            full_response = await self._get_response(
                client,
                message,
                conversation_id,
                resolved_agent_id,
                system_prompt,
                active_model,
                history_to_send,
            )
        except OpenClawApiError as err:
            _LOGGER.error("OpenClaw conversation error: %s", err)

            # Try token refresh if we have the capability
            refresh_fn = entry_data.get("refresh_token")
            if refresh_fn:
                refreshed = await refresh_fn()
                if refreshed:
                    try:
                        full_response = await self._get_response(
                            client,
                            message,
                            conversation_id,
                            resolved_agent_id,
                            system_prompt,
                            active_model,
                            history_to_send,
                        )
                    except OpenClawApiError as retry_err:
                        return self._error_result(
                            user_input,
                            f"Error communicating with OpenClaw: {retry_err}",
                        )
                else:
                    return self._error_result(
                        user_input,
                        f"Error communicating with OpenClaw: {err}",
                    )
            else:
                return self._error_result(
                    user_input,
                    f"Error communicating with OpenClaw: {err}",
                )

        # Fire event so automations can react to the response
        self.hass.bus.async_fire(
            EVENT_MESSAGE_RECEIVED,
            {
                ATTR_MESSAGE: full_response,
                ATTR_SESSION_ID: conversation_id,
                ATTR_MODEL: coordinator.data.get(DATA_MODEL) if coordinator.data else None,
                ATTR_TIMESTAMP: datetime.now(timezone.utc).isoformat(),
            },
        )
        coordinator.update_last_activity()

        # v1.0.3: persist this turn into the rolling history
        self._record_turn(state, message, full_response, system_prompt is not None)

        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(full_response)

        return conversation.ConversationResult(
            response=intent_response,
            conversation_id=conversation_id,
            continue_conversation=self._should_continue(full_response),
        )

    def _resolve_conversation_id(
        self,
        user_input: conversation.ConversationInput,
        agent_id: str | None,
    ) -> str:
        """Return conversation id from HA with conservative agent namespacing."""
        configured_session_id = self._normalize_optional_text(
            self.entry.options.get(
                CONF_ASSIST_SESSION_ID,
                DEFAULT_ASSIST_SESSION_ID,
            )
        )
        if configured_session_id:
            return configured_session_id

        agent_suffix = self._normalize_optional_text(agent_id)

        if user_input.conversation_id:
            if agent_suffix:
                return f"{user_input.conversation_id}:{agent_suffix}"
            return user_input.conversation_id

        context = getattr(user_input, "context", None)
        user_id = getattr(context, "user_id", None)
        if user_id:
            base_id = f"assist_user_{user_id}"
            return f"{base_id}:{agent_suffix}" if agent_suffix else base_id

        device_id = getattr(user_input, "device_id", None)
        if device_id:
            base_id = f"assist_device_{device_id}"
            return f"{base_id}:{agent_suffix}" if agent_suffix else base_id

        return f"assist_default:{agent_suffix}" if agent_suffix else "assist_default"

    def _normalize_optional_text(self, value: Any) -> str | None:
        """Return a stripped string or None for blank values."""
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned or None

    async def _get_response(
        self,
        client: OpenClawApiClient,
        message: str,
        conversation_id: str,
        agent_id: str | None = None,
        system_prompt: str | None = None,
        model: str | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        """Get a response from OpenClaw, trying streaming first."""
        full_response = ""
        async for chunk in client.async_stream_message(
            message=message,
            session_id=conversation_id,
            model=model,
            system_prompt=system_prompt,
            agent_id=agent_id,
            extra_headers=_VOICE_REQUEST_HEADERS,
            history=history,
        ):
            full_response += chunk

        if full_response:
            return full_response

        response = await client.async_send_message(
            message=message,
            session_id=conversation_id,
            model=model,
            system_prompt=system_prompt,
            agent_id=agent_id,
            extra_headers=_VOICE_REQUEST_HEADERS,
            history=history,
        )
        return extract_text_recursive(response) or ""

    # ── v1.0.3 history cache helpers ──────────────────────────────────────

    def _get_or_create_state(self, conversation_id: str) -> _ConversationState:
        """Return the rolling state for ``conversation_id``, creating if absent.

        Touches ``last_accessed`` for LRU. Evicts the least-recently-used
        conversation if the cache exceeds ``MAX_CACHED_CONVERSATIONS``.
        """
        state = self._conversations.get(conversation_id)
        if state is None:
            state = _ConversationState()
            self._conversations[conversation_id] = state
            if len(self._conversations) > MAX_CACHED_CONVERSATIONS:
                oldest_key = min(
                    self._conversations,
                    key=lambda k: self._conversations[k].last_accessed,
                )
                self._conversations.pop(oldest_key, None)
                _LOGGER.debug(
                    "Evicted oldest conversation %r from cache (LRU)",
                    oldest_key,
                )
        state.last_accessed = datetime.now(timezone.utc)
        return state

    def _record_turn(
        self,
        state: _ConversationState,
        user_message: str,
        assistant_message: str,
        system_prompt_was_sent: bool,
    ) -> None:
        """Append the just-completed turn into the rolling history and trim."""
        state.messages.append({"role": "user", "content": user_message})
        state.messages.append({"role": "assistant", "content": assistant_message})
        if system_prompt_was_sent:
            state.turns_since_system = 1
        else:
            state.turns_since_system += 1

        # Trim to the last HISTORY_MAX_TURNS turns (= 2 * messages each)
        max_msgs = HISTORY_MAX_TURNS * 2
        if len(state.messages) > max_msgs:
            state.messages = state.messages[-max_msgs:]

    @staticmethod
    def _should_continue(response: str) -> bool:
        """Determine if the conversation should continue after this response.

        Returns True when the assistant's reply ends with a question or
        an explicit prompt for follow-up, so that Voice PE and other
        satellites automatically re-listen without requiring a wake word.

        The heuristic checks for:
        - Trailing question marks (including after closing quotes/parens)
        - Common conversational follow-up patterns in English and German
        """
        if not response:
            return False

        text = response.strip()

        # Check if the response ends with a question mark
        # (allow trailing punctuation like quotes, parens, or emoji)
        if re.search(r"\?\s*[\"'""»)\]]*\s*$", text):
            return True

        # Common follow-up patterns (EN + DE)
        lower = text.lower()
        follow_up_patterns = (
            "what do you think",
            "would you like",
            "do you want",
            "shall i",
            "should i",
            "can i help",
            "anything else",
            "let me know",
            "was meinst du",
            "möchtest du",
            "willst du",
            "soll ich",
            "kann ich",
            "noch etwas",
            "sonst noch",
        )
        for pattern in follow_up_patterns:
            if pattern in lower:
                return True

        return False

    def _error_result(
        self,
        user_input: conversation.ConversationInput,
        error_message: str,
    ) -> conversation.ConversationResult:
        """Build an error ConversationResult."""
        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_error(
            intent.IntentResponseErrorCode.UNKNOWN,
            error_message,
        )
        return conversation.ConversationResult(
            response=intent_response,
            conversation_id=user_input.conversation_id,
        )
