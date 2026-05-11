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

# v2.1.6: cap on how many prior chat_log items we replay to the gateway
# at the start of each user turn. Each Assist conversation typically has
# under ~30 items unless the user is doing a long-running back-and-forth.
# Capping protects against worst-case payload bloat while keeping enough
# context for sensible follow-up handling.
MAX_HISTORY_ITEMS = 40


def _convert_chat_log_to_input_items(
    chat_log_content,
) -> list[dict[str, Any]]:
    """Convert HA ``chat_log.content`` into OpenAI-Responses ``input`` items.

    Maps each content type to its OpenResponses counterpart:
    - ``UserContent`` → ``{"type": "message", "role": "user", "content": …}``
    - ``AssistantContent``: text → message item;
      ``tool_calls`` (if any) → ``function_call`` items
    - ``ToolResultContent`` → ``function_call_output`` item
    - ``SystemContent`` → skipped (we ship those via ``instructions``)

    Defensive against unknown shapes — anything we don't recognize is
    skipped silently rather than crashing the whole turn.
    """
    items: list[dict[str, Any]] = []
    for entry in chat_log_content or []:
        if isinstance(entry, conversation.UserContent):
            text = (getattr(entry, "content", None) or "").strip()
            if text:
                items.append(
                    {"type": "message", "role": "user", "content": text}
                )
        elif isinstance(entry, conversation.AssistantContent):
            text = (getattr(entry, "content", None) or "").strip()
            if text:
                items.append(
                    {"type": "message", "role": "assistant", "content": text}
                )
            for tc in (getattr(entry, "tool_calls", None) or []):
                call_id = getattr(tc, "id", None) or getattr(tc, "tool_name", "")
                items.append(
                    {
                        "type": "function_call",
                        "call_id": str(call_id),
                        "name": getattr(tc, "tool_name", "") or "",
                        "arguments": json.dumps(
                            getattr(tc, "tool_args", None) or {},
                            default=str,
                            ensure_ascii=False,
                        ),
                    }
                )
        elif isinstance(entry, conversation.ToolResultContent):
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(getattr(entry, "tool_call_id", "")),
                    "output": json.dumps(
                        getattr(entry, "tool_result", None),
                        default=str,
                        ensure_ascii=False,
                    ),
                }
            )
        # SystemContent and any unknown types are intentionally skipped.
    if len(items) > MAX_HISTORY_ITEMS:
        items = items[-MAX_HISTORY_ITEMS:]
    return items


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

    def _resolve_session_key(
        self,
        agent_name: str,
        user_input: conversation.ConversationInput | None = None,
    ) -> str:
        """Pick the OpenResponses ``user`` field for this turn.

        v2.1.8: hypothesis (sub-agent diagnosis) is that the OpenClaw
        gateway uses the ``user`` field as a server-side session key —
        loading prior context for the same string and accumulating into
        it. Keeping ``user`` constant across Assist conversations
        therefore re-introduces the chain-contamination we already
        eliminated on the HA side in v2.1.6/v2.1.7.

        Fix: rotate ``user`` by HA's ``conversation_id`` so each
        distinct Assist conversation maps to a fresh server-side
        session, while turns *within* a single conversation still share
        one bucket. The configured "Assist Session ID override" still
        acts as the prefix (so the user can recognise their bucket
        family in OpenClaw web), with the conversation_id suffix giving
        per-conversation isolation.
        """
        configured = self._opt(CONF_ASSIST_SESSION_ID, DEFAULT_ASSIST_SESSION_ID)
        base = (
            configured.strip()
            if isinstance(configured, str) and configured.strip()
            else f"openclaw-{self.entry.entry_id[:8]}-{agent_name}"
        )
        conv_id = (
            (getattr(user_input, "conversation_id", None) or "").strip()
            if user_input
            else ""
        )
        if not conv_id:
            return base
        return f"{base}:{conv_id[:24]}"

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
        session_key = self._resolve_session_key(agent_name, user_input)
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

        # v2.1.7: NEVER pass `previous_response_id` — not even WITHIN
        # the tool-call loop of a single user turn.
        #
        # v2.1.6 still used it as a local optimization inside the loop
        # so we wouldn't have to resend the growing history between
        # tool iterations. Sub-agent debugging found that even that
        # in-loop chaining causes OpenClaw's gateway to inject a
        # "[Chat messages since your last reply - for context]" text
        # wrapper into the user message, which Qwen3 reads as "you
        # already replied" and answers with NO_REPLY. Removing the
        # chain entirely (in-loop and cross-turn) eliminates both the
        # wrapper and the cross-turn contamination from v2.1.0–v2.1.5.
        #
        # Each iteration's request is fully self-contained: it carries
        # the whole history accumulated so far in `input_items`
        # (initial chat_log.content, plus every `message`/`function_call`
        # item the model emitted in prior iterations of this turn, plus
        # every `function_call_output` we produced). That mirrors the
        # OpenAI Responses non-streaming pattern that does not rely on
        # `previous_response_id`.
        input_items = _convert_chat_log_to_input_items(chat_log.content)

        # Always send `instructions` on iteration 0 of each user turn
        # (kept from v2.1.4).
        send_instructions: str | None = instructions

        # v2.1.2: accumulate text across ALL iterations rather than only
        # taking the last one. Qwen3.6 sometimes emits its useful summary
        # in the iteration that also contains the function_call, and then
        # follows up with a useless one-word response ("NO", "OK") in the
        # post-tool iteration, which used to overwrite the good text.
        all_text_pieces: list[str] = []
        final_response_id: str | None = None

        for iteration in range(MAX_TOOL_ITERATIONS):
            if iteration == 0:
                # v2.1.8: visibility without enabling debug logs. Helps
                # tell at a glance whether the gateway is being asked
                # what we think we're asking, and whether `user`
                # actually rotates per Assist conversation (Bug B fix).
                _LOGGER.warning(
                    "OpenClaw v2.1.8 → POST /v1/responses | "
                    "model=openclaw/%s | user=%r | input_items=%d | "
                    "tools=%d | instructions_len=%d",
                    agent_name,
                    session_key,
                    len(input_items),
                    len(tools) if tools else 0,
                    len(instructions) if instructions else 0,
                )
            try:
                response = await self._client.async_send_responses(
                    model=f"openclaw/{agent_name}",
                    input_items=input_items,
                    user=session_key,
                    previous_response_id=None,  # v2.1.7: zero chain
                    instructions=send_instructions,
                    tools=tools or None,
                )
            except OpenClawApiError as err:
                _LOGGER.error(
                    "OpenClaw error on iteration %d: %s", iteration, err
                )
                return self._error(user_input, str(err))

            # After the first call, no point re-shipping `instructions`
            # for the same user turn — the model has them already and
            # they would just consume tokens.
            send_instructions = None

            response_id = response.get("id")
            if response_id:
                final_response_id = response_id

            # Walk the response items, accumulating BOTH the textual
            # content for our `final_text` AND the items themselves into
            # `input_items` so the NEXT iteration carries them as the
            # model's previous turn-in-progress. This is what replaces
            # `previous_response_id` chaining in v2.1.7.
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
                    # Replay this call back to the model in the next
                    # iteration so it knows what it just decided.
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": item.get("call_id"),
                            "name": item.get("name"),
                            "arguments": item.get("arguments", "{}"),
                        }
                    )
                elif item_type == "message":
                    # v2.1.8 Fix A: convert the output-side message item
                    # into a valid INPUT-side message item before
                    # appending to input_items for the next iteration.
                    #
                    # In the OpenAI Responses schema, output messages
                    # carry content as a list of {type:"output_text",
                    # text:"..."} pieces, but input messages expect
                    # content as a string (or a list of `input_text`
                    # pieces). Re-shipping the raw output item produces
                    # a request the gateway rejects/misinterprets — in
                    # multi-iteration tool loops that derails the chain
                    # silently.
                    collected: list[str] = []
                    for piece in item.get("content", []) or []:
                        if piece.get("type") == "output_text":
                            txt = piece.get("text") or ""
                            if txt:
                                collected.append(txt)
                    if collected:
                        text_pieces.extend(collected)
                        input_items.append(
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": "\n".join(collected),
                            }
                        )

            if text_pieces:
                all_text_pieces.extend(text_pieces)

            if not tool_calls:
                # Model is done — no more tools requested this turn.
                break

            # Execute each requested tool and append its result to
            # `input_items` (paired by call_id with the function_call
            # already appended above).
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

        # v2.1.6: chain_store no longer drives cross-turn continuity.
        # Each user turn rebuilds its input from chat_log.content, so we
        # don't persist a `last_response_id` here. The ChainStore stays
        # imported / instantiated for backwards compatibility (older
        # config entries reference it) but we leave it untouched.
        # See CHANGELOG v2.1.6 for the rationale.
        _ = final_response_id  # consumed only within the loop now

        self._coordinator.update_last_activity()

        # v2.1.5: register the assistant content with the chat_log.
        #
        # In modern ConversationEntity, HA uses chat_log.content to
        # render the assistant's reply in the UI, not just the speech
        # attached to the IntentResponse. When a turn included
        # function_calls, HA's call to chat_log.llm_api.async_call_tool
        # internally pushes tool items into chat_log, which seems to
        # also cause our text to be picked up — that's why turns WITH
        # tools rendered OK. But on a pure-text turn (no tool_calls, or
        # all output filtered down to the trivial fallback), chat_log
        # had no assistant entry → HA rendered "No response from
        # OpenClaw" even though intent_response.speech was set.
        #
        # The fix is to explicitly attach the assistant content to the
        # chat_log before returning. We use the *_without_tools variant
        # because by this point all tool calls of the turn have already
        # been executed and their results recorded by HA inside the
        # loop; only the final assistant text is missing.
        try:
            chat_log.async_add_assistant_content_without_tools(
                conversation.AssistantContent(
                    agent_id=self.entity_id,
                    content=final_text,
                )
            )
        except Exception as err:  # noqa: BLE001 — best-effort, never break the turn
            _LOGGER.debug(
                "Could not push assistant content into chat_log "
                "(non-fatal, falling back to intent_response.speech): %s",
                err,
            )

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
