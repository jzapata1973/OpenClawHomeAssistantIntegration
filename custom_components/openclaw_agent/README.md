# OpenClaw Agent (native)

Conversation agent for Home Assistant that talks to OpenClaw via its **native `/v1/responses` endpoint** instead of the OpenAI-compatible `/v1/chat/completions` one.

## Why this exists

The OpenAI-compatible endpoint of OpenClaw has known bugs that make stable agent + session routing impossible from the client side:

- Any `session_id` in the payload, or `X-Session-Id` / `x-openclaw-session-key` in the headers, **forces routing to the gateway's default agent** regardless of the requested `model`. Verified empirically via 3+ controlled curl tests on 2026-05-10.
- The gateway then returns `"model": "<the requested one>"` in the response body even though the actual answer came from the default agent. The response lies.

The `/v1/responses` endpoint has neither problem. Combined with a stable `user` field and `previous_response_id` chaining, you get:

- Real routing to the requested OpenClaw agent.
- Real per-session continuity managed server-side.
- Free reuse of OpenClaw's built-in memory layer (so the agent remembers facts about the user across HA restarts).

## Pre-requisites in OpenClaw

The native endpoint is **disabled by default**. Enable it by editing `~/.openclaw/openclaw.json` and adding inside `gateway.http.endpoints`:

```json
"responses": { "enabled": true }
```

Restart OpenClaw and verify with:

```sh
curl -s -X POST http://<gateway>:18789/v1/responses \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"model":"openclaw/<agent>","input":"hola","user":"casajaz-nabuhome"}'
```

If you get a JSON response with `"object": "response"`, you are ready.

## Install

1. Copy `custom_components/openclaw_agent/` into your HA `config/custom_components/`.
2. Restart HA.
3. **Settings → Devices & Services → Add Integration → "OpenClaw Agent (native)"**.
4. Fill in:
   - **Gateway URL** — e.g. `http://192.168.10.40:18789`
   - **Gateway token** — from `gateway.auth.token` in `openclaw.json`
   - **Agent name** — e.g. `nabu-home`
   - **Session key** — pin it once, e.g. `casajaz-nabuhome`. This is what gives you a single persistent conversation thread.
5. **Settings → Voice assistants → Add assistant** → pick this integration as the conversation agent.

## Behaviour

- Every Assist turn becomes a `POST /v1/responses` call with `model = openclaw/<agent>`, `user = <session_key>`, and `previous_response_id = <last seen response.id>`.
- The chain head is persisted to `config/.storage/openclaw_agent.chain` so it survives HA restarts.
- System instructions are sent only on the very first turn of a chain. The gateway carries them server-side after that, saving tokens.

## Limitations

- v0.1.0 is **non-streaming** — Qwen-on-vLLM responses can take a few seconds. Streaming will come in v0.2.0.
- Tool/function calling is whatever the gateway agent already has configured (skills, MCP servers); HA does not inject HA-specific tools at the protocol level — the agent should reach back to HA via the configured Home Assistant MCP server.
