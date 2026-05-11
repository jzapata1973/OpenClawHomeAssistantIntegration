"""HTTP client for OpenClaw's native OpenResponses API (POST /v1/responses).

Why this client and not the OpenAI Chat Completions one:

The OpenAI-compatible /v1/chat/completions endpoint of OpenClaw forces
routing to the gateway's default agent whenever any session reference
(session_id payload field, X-Session-Id header, or x-openclaw-session-key
header) is present — regardless of the requested `model`. Empirically
verified via 3+ controlled curl tests on 2026-05-10. The /v1/responses
endpoint does NOT have that bug: a stable `user` plus chained
`previous_response_id` give a real, persistent, agent-bound session
managed by the gateway, including its built-in memory layer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .const import RESPONSES_PATH

_LOGGER = logging.getLogger(__name__)

# Long timeout because Qwen on local vLLM can take a while.
API_TIMEOUT = aiohttp.ClientTimeout(total=300, sock_read=180)


class OpenClawAgentError(Exception):
    """Any failure talking to the OpenClaw OpenResponses endpoint."""


class OpenClawAgentClient:
    """Minimal async client for POST /v1/responses."""

    def __init__(
        self,
        gateway_url: str,
        token: str,
        session: aiohttp.ClientSession,
    ) -> None:
        self._url = gateway_url.rstrip("/") + RESPONSES_PATH
        self._token = token
        self._session = session

    @property
    def url(self) -> str:
        return self._url

    async def async_send(
        self,
        *,
        model: str,
        input_text: str,
        user: str,
        previous_response_id: str | None = None,
        instructions: str | None = None,
    ) -> dict[str, Any]:
        """POST a single OpenResponses request, return parsed JSON."""
        payload: dict[str, Any] = {
            "model": model,
            "input": input_text,
            "user": user,
            "stream": False,
        }
        if previous_response_id:
            payload["previous_response_id"] = previous_response_id
        if instructions:
            payload["instructions"] = instructions

        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

        try:
            async with self._session.post(
                self._url,
                headers=headers,
                json=payload,
                timeout=API_TIMEOUT,
            ) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise OpenClawAgentError(
                        f"OpenResponses HTTP {resp.status}: {body[:500]}"
                    )
                content_type = resp.content_type or ""
                if "json" not in content_type:
                    raise OpenClawAgentError(
                        f"Unexpected content-type {content_type!r} from "
                        f"{self._url} (likely /v1/responses is disabled): {body[:200]}"
                    )
                import json

                return json.loads(body)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise OpenClawAgentError(
                f"Cannot reach OpenClaw gateway at {self._url}: {err}"
            ) from err

    @staticmethod
    def extract_text(response: dict[str, Any]) -> str:
        """Pull the assistant text out of an OpenResponses payload.

        OpenResponses shape:
            { "output": [
                { "type": "message",
                  "content": [ { "type": "output_text", "text": "..." } ] }
              ] }
        """
        for item in response.get("output", []) or []:
            if item.get("type") != "message":
                continue
            for piece in item.get("content", []) or []:
                if piece.get("type") == "output_text":
                    text = piece.get("text")
                    if text:
                        return text
        return ""
