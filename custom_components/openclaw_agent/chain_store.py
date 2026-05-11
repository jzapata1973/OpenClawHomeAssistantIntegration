"""Persistent storage for the OpenResponses chain head per session_key.

OpenClaw's gateway maintains conversation memory server-side, but the
client must remember the most recent `response.id` to send back as
`previous_response_id` on the next turn. Without that pointer the chain
breaks and the gateway treats the next message as a new conversation
(losing the working context, even though the agent's long-term memory
layer still recalls user facts).

This stores the pointer to disk (HA's `.storage/openclaw_agent.chain`)
so the chain survives Home Assistant restarts.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)


class ChainStore:
    """Maps session_key -> last seen response_id, persisted to disk."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, str]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY
        )
        self._cache: dict[str, str] = {}
        self._loaded = False

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if isinstance(data, dict):
            # Defensive: keep only str->str pairs.
            self._cache = {
                str(k): str(v) for k, v in data.items() if isinstance(v, str)
            }
        self._loaded = True
        _LOGGER.debug(
            "ChainStore loaded with %d session(s)", len(self._cache)
        )

    def get_last(self, session_key: str) -> str | None:
        return self._cache.get(session_key)

    async def async_set_last(
        self, session_key: str, response_id: str
    ) -> None:
        self._cache[session_key] = response_id
        await self._store.async_save(self._cache)

    async def async_clear(self, session_key: str) -> None:
        if session_key in self._cache:
            self._cache.pop(session_key, None)
            await self._store.async_save(self._cache)
