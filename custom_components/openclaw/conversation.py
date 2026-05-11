"""OpenClaw conversation entity (v2.1.0+).

What changed in v2.1.0
----------------------

v2.0.0 routed Assist to OpenClaw via ``/v1/responses`` with a stable
``user``/session_key + ``previous_response_id`` chain. Routing and
continuity worked, but tools were NOT injected — the integration only
shipped ``input_text`` plus a "use the agent's MCP tools" instruction
and hoped the agent would pick the right path. In practice the agent
sometimes improvised raw HTTP calls to Home Assistant's REST API
(causing the "Login attempt failed from 192.168.10.40" notification on
2026-05-10) instead of using the configured MCP server.

v2.1.0 fixes this at the protocol level: HA's Assist tools
(``HassTurnOn``, ``HassClimateSetTemperature``, ``HassMediaPause``,
…) are converted to OpenAI Responses function-tool schema and
forwarded with each request. The model gets explicit, structured
tools and HA executes them locally via ``chat_log.llm_api.async_call_tool``.
This:

- Eliminates the "improvised HTTP" path → no more 401s against HA.
- Drops a chunk of the system prompt: HA's tool descriptions replace
  long text instructions about how to talk to the home.
- Aligns with how ``openai_conversation`` in HA core does it.

We also migrate from ``conversation.async_set_agent(...)`` (the old
agent registration) to ``conversation.ConversationEntity``, the
modern entity-platform pattern.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent, llm
from homeassistant.helpers.entity_platform import AddEntitiesCallback

try:  # voluptuous_openapi is bundled with HA; very small fallback otherwise.
    from voluptuous_openapi import convert as _voluptuous_to_openapi
except ImportError:  # pragma: no cover

    def _voluptuous_to_openapi(_schema, **_kwargs):
        return {"type": "object", "properties": {}}


from .api import OpenClawApiClient, OpenClawApiError
from .chain_store import ChainStore
from .const import (
    CONF_AGENT_ID,
    CONF_ASSIST_SESSION_ID,
    CONF_INCLUDE_EXPOSED_CONTEXT,
    CONF_VOICE_AGENT_ID,
    DEFAULT_AGENT_ID,
    DEFAULT_ASSIST_SESSION_ID,
    DEFAULT_INCLUDE_EXPOSED_CONTEXT,
    DOMAIN,
)
from .coordinator import OpenClawCoordinator

_LOGGER = logging.getLogger(__name__)

# Hard ceiling on tool-call iterations within a single user turn. Stops
# pathological loops where the model keeps requesting tools forever.
MAX_TOOL_ITERATIONS = 8


def _format_tool_for_responses(tool: llm.Tool) -> dict[str, Any]:
    """Convert an HA ``llm.Tool`` into an OpenAI-Responses function tool dict.

    OpenClaw's gateway forwards the ``tools`` array verbatim to the
    underlying model (vLLM Qwen3.6 with ``--tool-call-parser qwen3_coder``
    in this user's setup), so the schema must follow the OpenAI Responses
    function-tool shape.
    """
    unsupported_keys = {"oneOf", "anyOf", "allOf", "enum", "not"}
    schema: dict[str, Any] = _voluptuous_to_openapi(tool.parameters)
    if isinstance(schema, dict) and unsupported_keys.intersection(schema):
        schema = {k: v for k, v in schema.items() if k not in unsupported_keys}
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description or "",
        "parameters": schema,
        "strict": False,
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the conversation entity from a config entry."""
    bag = hass.data[DOMAIN][entry.entry_id]
    entity = OpenClawConversationEntity(
        hass=hass,
        entry=entry,
        client=bag["client"],
        chain_store=bag["chain_store"],
        coordinator=bag["coordinator"],
    )
    async_add_entities([entity])


class OpenClawConversationEntity(
    conversation.ConversationEntity,
    conversation.AbstractConversationAgent,
):
    """OpenClaw conversation, with HA Assist tools injected per request."""

    _attr_supports_streaming = False
    _attr_has_entity_name = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: OpenClawApiClient,
        chain_store: ChainStore,
        coordinator: OpenClawCoordinator,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._client = client
        self._chain_store = chain_store
        self._coordinator = coordinator
        self._attr_name = "OpenClaw Conversation"
        self._attr_unique_id = f"{entry.entry_id}_conversation"
        self._attr_supported_features = (
            conversation.ConversationEntityFeature.CONTROL
        )

    @property
    def attribution(self) -> dict[str, str]:
        return {"name": "Powered by OpenClaw", "url": "https://openclaw.ai"}

    @property
    def supported_languages(self) -> list[str] | str:
        return conversation.MATCH_ALL

    # ── option / config helpers ──────────────────────────────────────

    def _opt(self, key: str, default: Any) -> Any:
        opts = self.entry.options
        data = self.entry.data
        if key in opts:
            return opts[key]
        return data.get(key, default)

    def _resolve_agent_name(self) -> str:
        voice_agent = self._opt(CONF_VOICE_AGENT_ID, "")
        configured = self._opt(CONF_AGENT_ID, DEFAULT_AGENT_ID)
        return (voice_agent or configured or DEFAULT_AGENT_ID).strip()

    def _resolve_session_key(self, agent_name: str) -> str:
        configured = self._opt(CONF_ASSIST_SESSION_ID, DEFAULT_ASSIST_SESSION_ID)
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
        return f"openclaw-{self.entry.entry_id[:8]}-{agent_name}"

    def _build_instructions(self) -> str:
        location = self.hass.config.location_name or "Home"
        return (
            f'Sos el agente conversacional de Home Assistant "{location}". '
            "Respondé en el idioma del usuario, breve y al grano. "
            "Tenés tools nativas de Home Assistant (HassTurnOn, "
            "HassClimateSetTemperature, etc.) para leer estados y accionar "
            "dispositivos — usá esas tools, NUNCA improvises HTTP requests. "
            "Después de ejecutar una tool, SIEMPRE cerrá con una frase "
            "breve en el idioma del usuario describiendo qué pasó (ej. "
            '"Listo, encendí 5 luces" o "La temperatura es 23°C"). '
            "Nunca respondas solamente con 'NO', 'OK', 'YES', 'NO_REPLY' "
            "u otros tokens internos cortos — siempre una oración descriptiva."
        )

    # ── core message handler ─────────────────────────────────────────

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Handle one user turn — possibly with several tool-call iterations."""
        agent_name = self._resolve_agent_name()
        session_key = self._resolve_session_key(agent_name)
        instructions = self._build_instructions()

        # Load HA's Assist API. After this, chat_log.llm_api.tools holds the
        # set of HassXxx tools, automatically filtered by the entities the
        # user has exposed to Assist.
        await chat_log.async_provide_llm_data(
            user_input.as_llm_context(DOMAIN),
            "assist",
            user_llm_prompt=instructions,
            user_extra_system_prompt=getattr(user_input, "extra_system_prompt", None),
        )

        tools: list[dict[str, Any]] = []
        if chat_log.llm_api:
            tools = [_format_tool_for_responses(t) for t in chat_log.llm_api.tools]

        previous_response_id = self._chain_store.get_last(session_key)

        # First call's input is just the new user message. Subsequent
        # calls (driven by function_calls in the response) replace
        # input_items with function_call_output items.
        input_items: list[dict[str, Any]] = [
            {"type": "message", "role": "user", "content": user_input.text}
        ]

        # The gateway carries the chain server-side. ``instructions`` is
        # only honored on the very first turn of a chain — sending it
        # again with previous_response_id is a no-op (and uses tokens).
        send_instructions: str | None = (
            instructions if previous_response_id is None else None
        )

        # v2.1.2: accumulate text across ALL iterations rather than only
        # taking the last one. Qwen3.6 sometimes emits its useful summary
        # in the iteration that also contains the function_call, and then
        # follows up with a useless one-word response ("NO", "OK") in the
        # post-tool iteration, which used to overwrite the good text.
        all_text_pieces: list[str] = []
        final_response_id: str | None = None

        for iteration in range(MAX_TOOL_ITERATIONS):
            try:
                response = await self._client.async_send_responses(
                    model=f"openclaw/{agent_name}",
                    input_items=input_items,
                    user=session_key,
                    previous_response_id=previous_response_id,
                    instructions=send_instructions,
                    tools=tools or None,
                )
            except OpenClawApiError as err:
                _LOGGER.error(
                    "OpenClaw error on iteration %d: %s", iteration, err
                )
                return self._error(user_input, str(err))

            # After the first call, only the `input` and the chain
            # advance. Stop sending `instructions` again.
            send_instructions = None

            response_id = response.get("id")
            if response_id:
                final_response_id = response_id
                previous_response_id = response_id

            # Extract function_calls + any partial text from this response.
            tool_calls: list[dict[str, Any]] = []
            text_pieces: list[str] = []
            for item in response.get("output", []) or []:
                item_type = item.get("type")
                if item_type == "function_call":
                    tool_calls.append(
                        {
                            "call_id": item.get("call_id"),
                            "name": item.get("name"),
                            "arguments": item.get("arguments", "{}"),
                        }
                    )
                elif item_type == "message":
                    for piece in item.get("content", []) or []:
                        if piece.get("type") == "output_text":
                            txt = piece.get("text") or ""
                            if txt:
                                text_pieces.append(txt)

            if text_pieces:
                all_text_pieces.extend(text_pieces)

            if not tool_calls:
                # Model is done — no more tools requested this turn.
                break

            # Execute the requested tools and stage their outputs as the
            # input for the next iteration. The gateway already has the
            # function_call items in the chain via previous_response_id.
            input_items = []
            for call in tool_calls:
                try:
                    args = (
                        json.loads(call["arguments"])
                        if call.get("arguments")
                        else {}
                    )
                except (json.JSONDecodeError, TypeError) as err:
                    _LOGGER.warning(
                        "Could not parse arguments for tool %s: %s — sending {}",
                        call.get("name"),
                        err,
                    )
                    args = {}

                tool_input = llm.ToolInput(
                    tool_name=call["name"],
                    tool_args=args,
                )
                try:
                    tool_result = await chat_log.llm_api.async_call_tool(
                        tool_input
                    )
                except Exception as err:  # noqa: BLE001 — surface as JSON error
                    _LOGGER.warning(
                        "Tool %s raised: %s", call["name"], err
                    )
                    tool_result = {"error": str(err)}

                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": json.dumps(
                            tool_result, default=str, ensure_ascii=False
                        ),
                    }
                )
        else:
            _LOGGER.warning(
                "Reached MAX_TOOL_ITERATIONS=%d without a final answer for "
                "session_key=%s",
                MAX_TOOL_ITERATIONS,
                session_key,
            )
            if not all_text_pieces:
                all_text_pieces.append(
                    "No pude completar la consulta — se alcanzó el límite "
                    "de iteraciones de tools."
                )

        # Compose the final reply for HA Assist.
        #
        # Qwen3 occasionally emits internal control tokens like `NO_REPLY`
        # (its "done, nothing to say" signal) or one-word filler ("NO",
        # "OK", "YES") at the END of a tool-call cycle. Those tokens leak
        # into output_text and, when shipped to HA Assist as the agent's
        # reply, get rendered as "No response from OpenClaw" — even when
        # the action actually succeeded and a useful prior piece exists.
        # See CHANGELOG v2.1.3.
        #
        # Strategy: dedupe adjacent repeats, then drop any piece that is
        # *only* a trivial/control token (case-insensitive, ignoring
        # surrounding punctuation/underscore/space). If filtering empties
        # the response, fall back to "Listo." rather than handing HA an
        # empty speech.
        _TRIVIAL_TOKENS = {
            "no", "yes", "ok",
            "sí", "si",
            "no_reply", "noreply", "no reply",
            "done", "ack",
        }

        def _is_trivial_token(piece: str) -> bool:
            return piece.lower().strip(".!?_- ") in _TRIVIAL_TOKENS

        deduped: list[str] = []
        for piece in all_text_pieces:
            cleaned = piece.strip()
            if not cleaned:
                continue
            if deduped and deduped[-1] == cleaned:
                continue
            deduped.append(cleaned)
        deduped = [p for p in deduped if not _is_trivial_token(p)]
        final_text = "\n\n".join(deduped).strip() or "Listo."

        # Persist the chain head AFTER the loop converges, so we don't
        # advance into a partial state if the loop errored mid-way.
        if final_response_id:
            await self._chain_store.async_set_last(
                session_key, final_response_id
            )

        self._coordinator.update_last_activity()

        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(final_text)
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
