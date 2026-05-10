# Changelog local · fork casajaz

Cambios aplicados sobre el fork `jzapata1973/OpenClawHomeAssistantIntegration` que NO están (todavía) en el upstream `techartdev`. Este archivo es independiente del `CHANGELOG.md` del upstream para evitar conflictos de merge.

**Versionado:** semver propio del fork, partiendo en `1.0.0`. Patch = bug fix, minor = feature, major = cambio incompatible.

---

## [1.0.3] · 2026-05-10 — Continuidad de conversación client-side + token saving

### Contexto

v1.0.2 dejó las requests de HA ruteando al agente correcto (nabu-home), pero como se sacó `session_id` del payload (porque cualquier referencia rompía el routing), **cada mensaje de Assist creaba una sesión nueva en el gateway**. Y el system prompt con todas las entidades (~13.000 chars) se mandaba en CADA request, inflando el costo.

Probamos varias formas de coaxar al gateway desde el cliente para tener una sesión persistente bajo nabu-home (`openai:` prefix, `agent:` prefix, headers solos, etc.) — todas terminaron creando sesión bajo el agente default. **El gateway no se puede convencer desde el cliente.**

### Solución

**Continuidad client-side, estilo OpenAI estándar:** HA mantiene el historial de cada conversación en memoria de proceso y lo manda en `messages[]` en cada request. El gateway sigue siendo stateless desde la perspectiva del routing → routing por `model` sigue funcionando.

**Skip del system prompt en follow-ups** — el contexto pesado (entidades expuestas) solo se inyecta:
- En la primera request de cada conversación (cuando el cache de historial está vacío)
- Cada `SYSTEM_REFRESH_EVERY = 10` turnos (para refrescar estados de entidades)

### Defaults (hardcoded en `conversation.py`)

```python
HISTORY_MAX_TURNS         = 20    # ventana de últimos 20 pares user+assistant
SYSTEM_REFRESH_EVERY      = 10    # re-inyectar system prompt cada 10 turnos
MAX_CACHED_CONVERSATIONS  = 50    # LRU eviction después de 50 conversaciones simultáneas
```

### Archivos tocados

- `conversation.py` — agrega `_ConversationState` dataclass, cache `self._conversations`, helpers `_get_or_create_state` y `_record_turn`. Reescribe la lógica de `async_process` para decidir cuándo mandar system prompt y construir el historial.
- `api.py` — `async_send_message` y `async_stream_message` aceptan param opcional `history: list[dict] | None` que se inyecta entre system prompt y user message.

### Tradeoffs

- ✅ Continuidad de conversación entre invocaciones de Assist (el `Assist Session ID` override hace que todas compartan el mismo conversation_id internamente → mismo cache)
- ✅ Token saving fuerte: turnos 2-10 son ~13.000 chars más livianos cada uno
- ✅ Routing por modelo intacto (no se rompió v1.0.2)
- ❌ Historial se pierde con cada reinicio de HA (es in-memory)
- ❌ Cada request sigue creando una sesión `openai:UUID` en el gateway, pero ahora bajo el agente correcto y con poca data
- ❌ Los estados de entidades solo se refrescan cada 10 turnos (si cambia algo importante en HA durante una conversación larga, el agente puede no verlo hasta el refresh)

### Para futuras versiones

- Persistir historial a disco para sobrevivir reinicios de HA (v1.1.0 candidato)
- Hacer los 3 defaults configurables via Options Flow
- Refrescar system prompt cuando detecte cambio en entidades expuestas (event-driven)

---

## [1.0.2] · 2026-05-10 — Fix definitivo de routing al agente

### Síntoma residual de v1.0.0/v1.0.1

A pesar de mandar `model=openclaw/<agente>` correcto en el payload, el gateway igual ruteaba al agente default (`main`). Confirmado vía curl directo:

| `session_id` mandado en | Routing efectivo |
|---|---|
| Nada | Agente correcto ✅ |
| Payload (`session_id`+`user`) | `main` ❌ |
| Headers (`X-Session-Id`, `x-openclaw-session-key`) | `main` ❌ |
| Ambos | `main` ❌ |

Y peor: el gateway responde con `"model": "<el que pediste>"` aunque internamente usó otro agente. La response miente.

### Fix

`custom_components/openclaw/api.py` — ya **NO se manda `session_id`/`user` en el payload** ni `X-Session-Id`/`x-openclaw-session-key` en headers. Solo va `model`, `messages` y `stream`. Esto restaura el routing por modelo.

### Tradeoff conocido

Se pierde la **continuidad de sesión cross-invocation en OpenClaw** (cada vez que abrís Assist y mandás algo, OpenClaw crea una sesión nueva). HA mantiene su `conversation_id` interno y sigue mandando `messages[]` con el historial dentro de la misma "ronda" de Assist, así que los follow-ups siguen teniendo contexto.

Si en el futuro queremos recuperar continuidad real en OpenClaw, hay que investigar si el gateway acepta session_ids prefijados con el agente (ej. `nabu-home:mi-sesion`) o si hay endpoint para crear sesión bajo agente específico.

### Cleanup

Logs `WARNING` de v1.0.1 bajados a `DEBUG` (ya no spamean en cada chat).

---

## [1.0.1] · 2026-05-10 — Diagnóstico: logs de routing

Logs `WARNING` temporales en `conversation.py` y `api.py` para diagnosticar por qué v1.0.0 no logra rutear las requests al agente correcto en algunos setups, a pesar de que el curl directo al gateway con `model=openclaw/<agent>` funciona perfecto.

Loguea:
- `options.active_model` (lo que el select tiene persistido)
- `voice_agent_id`, `configured_agent_id`, `resolved_agent_id`
- `model` final que llega al payload
- `payload.model` y `payload keys` justo antes del POST al gateway

Una vez identificada la causa, los logs vuelven a `DEBUG` o se eliminan en una versión posterior.

---

## [1.0.0] · 2026-05-10 — Fix: routing real al agente configurado

**Resuelve los upstream issues:** [#8](https://github.com/techartdev/OpenClawHomeAssistantIntegration/issues/8), [#24](https://github.com/techartdev/OpenClawHomeAssistantIntegration/issues/24), [#28](https://github.com/techartdev/OpenClawHomeAssistantIntegration/issues/28).

### Síntoma

Aunque se configuraba `Agent ID = nabu-home` (o cualquier otro), las requests de Assist y del servicio `openclaw.send_message` siempre caían en el agente default `main`. Adicionalmente, el dropdown `select.openclaw_assistant_active_model` se reseteaba solo cada ~30 segundos pisando la elección del usuario.

### Causa raíz

El gateway de OpenClaw rutea por el campo `model` del payload OpenAI-compatible, **no por el header** `x-openclaw-agent-id` que el cliente HA enviaba. Cuando el campo `model` iba vacío, el gateway caía al default (`main`).

Adicionalmente, `select._handle_coordinator_update` sobreescribía `_attr_current_option` con el modelo reportado por el gateway en cada poll del coordinator (cada `DEFAULT_SCAN_INTERVAL = 30s`), pisando la selección persistida en `entry.options['active_model']`.

### Cambios

- **`custom_components/openclaw/conversation.py`** — si `options.active_model` está vacío y hay `agent_id` configurado distinto del default, deriva `model = openclaw/<agent_id>` antes de llamar al cliente.
- **`custom_components/openclaw/__init__.py`** (handler de `openclaw.send_message`) — misma derivación + ahora también considera el `agent_id` configurado en el setup, no solo el de voice o el del call.
- **`custom_components/openclaw/select.py`** — `_handle_coordinator_update` y el `__init__` del entity respetan `entry.options['active_model']` como fuente de verdad. El modelo reportado por el gateway solo se usa como fallback inicial cuando el usuario aún no eligió nada.

### Cómo verificar

1. En HA: Settings → Devices → OpenClaw → Configure → Agent ID = `<tu-agente>`.
2. (Opcional) Cambiar `Active Model` en el device → debería persistir, ya no se reverte.
3. Hacer una pregunta vía Assist con el assistant de OpenClaw.
4. En la web de OpenClaw → Sesiones: la sesión nueva debe aparecer en el agente configurado, no en `main`.
