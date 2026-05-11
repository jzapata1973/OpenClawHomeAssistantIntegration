# Changelog local · fork casajaz

> **Una sola integración**: `custom_components/openclaw/`. La integración paralela `openclaw_agent` que existió brevemente (tag `openclaw-agent-v0.1.0`) fue **consolidada en `openclaw` v2.0.0** — su código y diseño viven hoy adentro de `openclaw/`. El folder fue eliminado para mantener un solo namespace.

---

## [2.1.2] · 2026-05-11 — Acumulación de texto + filtro de respuestas triviales

### Síntoma

Después del fix de timeout, el usuario probó "prende las luces de mi pieza":

- **Físicamente**: las luces SÍ se prendieron ✅ (la tool `HassTurnOn` se ejecutó OK).
- **Texto que recibió HA Assist**: `"No response from OpenClaw"` 😶

En el chat de OpenClaw web se veían 2 turnos del agente:

1. `function_call(HassTurnOn, [...])` + texto **"Luces de tu pieza encendidas. 💡"**
2. Después del `function_call_output`: texto **"NO"** (solo eso).

### Causa

El loop de v2.1.0/2.1.1 sobrescribía `final_text` en cada iteración con el `output_text` del response actual. Resultado: la iteración 2 con `"NO"` pisaba el `"Luces de tu pieza encendidas. 💡"` útil de la iteración 1. Y HA Assist parece tratar respuestas tan cortas / no-respuestas como "no response" en la UI.

Patrón de Qwen3.6 observado: en el mismo turno que emite un `function_call` también emite la "anunciación" del resultado (asume éxito). En el turno post-tool, a veces solo emite una palabra ("NO", "OK") en vez del resumen — comportamiento del modelo local, no es bug del gateway ni nuestro.

### Fix

`conversation.py`:

1. **Acumular** `output_text` de TODAS las iteraciones en `all_text_pieces` (en vez de overwrite).
2. **Dedupe adyacente** — si dos iteraciones emiten exactamente el mismo texto, mantener uno solo.
3. **Drop trailing trivial** — si la última pieza es `"NO"` / `"YES"` / `"OK"` / `"sí"` (case-insensitive, sin puntuación) y hay alguna pieza anterior con contenido real, descartar la trivial.
4. **Fallback** — si después de todo no queda nada, devolver `"Listo."` (no string vacío) para que HA Assist no muestre "No response from OpenClaw".

Y refuerzo de instructions:

```
Después de ejecutar una tool, SIEMPRE cerrá con un mensaje breve en el
idioma del usuario describiendo qué pasó. Nunca respondas solamente con
'NO', 'OK' o 'YES' sueltos.
```

### Resultado esperado en HA Assist

Para "prende las luces de mi pieza":

- Antes: `"No response from OpenClaw"` 😶
- Después (con suerte y modelo cooperativo): `"Listo, encendí 5 luces de tu pieza."` ✅
- Después (si el modelo igual emite "NO" al final): `"Luces de tu pieza encendidas. 💡"` (porque dropeamos el "NO" trailing)

### Mirando más adelante

v2.2.0 con streaming SSE va a hacer este accumulation natural — cada delta se appendea al chat_log de HA automáticamente. Las heurísticas de filtro de "NO" trivial pueden quedar como protección defensiva.

---

## [2.1.1] · 2026-05-11 — Hotfix: timeout para `/v1/responses`

### Síntoma

Después del primer chat (que andaba bien), el segundo "apagalas ahora" cortaba con:

```
Cannot reach OpenClaw gateway at http://192.168.10.40:18789/v1/responses:
Timeout on reading data from socket
```

### Causa

`async_send_responses()` reusaba `STREAM_TIMEOUT = ClientTimeout(total=300, sock_read=120)`. Ese timeout fue diseñado para SSE streaming de chat-completions donde llegan deltas cada pocos segundos. **`/v1/responses` non-streaming NO manda nada hasta que el modelo tiene la respuesta completa**. Para Qwen 27B local procesando una request con chain creciente (123k tokens / 48% del context window) en el segundo turn, el agente puede tardar >2 minutos sin emitir un byte → `sock_read=120s` se agota.

### Fix

Nuevo `RESPONSES_TIMEOUT = ClientTimeout(total=600, sock_read=300)` dedicado al endpoint nativo. Generoso pero realista hasta que v2.2.0 traiga streaming SSE real (que evita el problema completamente porque cada token reset-ea el sock_read).

### Mientras tanto, si el chain crece mucho

Si los turns se ponen muy lentos (gateway procesando más de 5min), podés resetear manualmente el chain borrando `config/.storage/openclaw_chain.json`. La próxima conversación arranca limpia. Service `openclaw.reset_chain` viene en v2.2.0.

---

## [2.1.0] · 2026-05-11 — Tools nativas de HA inyectadas en cada request

### Problema que resuelve

v2.0.0 routeaba bien al agente y mantenía la sesión, pero **NO inyectaba tools** en el payload de `/v1/responses` — solo mandaba `input` + un texto vago en `instructions` diciendo "usá las tools del MCP del agente". El agente OpenClaw entonces tenía dos caminos:

1. Usar el server MCP de HA configurado en `openclaw-mcp-adapter` (lo correcto).
2. Improvisar `curl http://192.168.10.21:8123/api/...` o similar (lo incorrecto).

A veces tomaba el camino #2 → HA respondía 401 → notificación **"Login attempt failed from 192.168.10.40"** que vimos el 2026-05-10.

Adicionalmente, el system prompt cargaba archivos pesados del workspace del agente (`AGENTS.md`, `SOUL.md`, `MEMORY.md`) y/o el dump de entidades expuestas (~13k chars).

### Solución (v2.1.0)

Patrón estándar de HA, igual que `homeassistant.components.openai_conversation`:

1. **`conversation.py` reescrito como `ConversationEntity`** (entity moderna) en vez del viejo `AbstractConversationAgent` registrado vía `async_set_agent`. Hereda de `conversation.ConversationEntity` + `conversation.AbstractConversationAgent` para retrocompat.

2. **HA expone sus tools al agente**:
   ```python
   await chat_log.async_provide_llm_data(
       user_input.as_llm_context(DOMAIN), "assist",
       user_llm_prompt=instructions,
   )
   tools = [_format_tool_for_responses(t) for t in chat_log.llm_api.tools]
   ```
   Eso da `HassTurnOn`, `HassClimateSetTemperature`, `HassMediaPause`, etc., **automáticamente filtradas por las entidades expuestas a Assist**. Cero configuración manual de qué tools mostrar.

3. **Tool-call loop manual** (no streaming todavía):
   - Mandamos `/v1/responses` con `tools=[...]` + `input=[user_message]`.
   - Si la response trae items `function_call`, los ejecutamos vía `chat_log.llm_api.async_call_tool(...)`.
   - Mandamos siguiente turno con `input=[function_call_output items]` y `previous_response_id` actualizado.
   - Repetir hasta que la response venga sin `function_call` (solo texto final).
   - Tope: `MAX_TOOL_ITERATIONS = 8` para evitar loops infinitos.

4. **`api.py async_send_responses` extendido** para aceptar `tools: list[dict]` y `input_items: list[dict]` (además del `input_text: str` original).

5. **`_format_tool_for_responses(llm.Tool) -> dict`** — convierte una tool de HA al schema OpenAI Responses, removiendo keywords no soportadas (`oneOf`, `anyOf`, `allOf`, `enum`, `not`).

### Pre-requisito de servidor

OpenClaw debe forwardear el campo `tools` del payload al modelo subyacente. Para vLLM Qwen3.6 se hace habilitando el parser correcto en el wrapper de vLLM:

```
--tool-call-parser qwen3_coder
```

(Verificable: si después del upgrade el agente responde "no sé hacer eso" en vez de ejecutar acciones, el problema está acá.)

### Cambios concretos

- `conversation.py` — reescrito por completo. ~280 líneas. Mantiene la lectura de las opciones legacy (`Agent ID`, `Voice agent ID`, `Assist session ID override`).
- `api.py` — `async_send_responses` ahora acepta `tools` + `input_items`.
- `manifest.json` — bump a 2.1.0.
- `__init__.py` — sin cambios (el `async_forward_entry_setups` ya cubría `conversation` platform; ahora se carga la entity en lugar de registrar un agent).

### Lo que NO cambia (sigue funcionando igual)

- Sensores, binary sensors, button, select, event, coordinator → idénticos.
- Service handlers `openclaw.send_message`/`clear_history`/`invoke_tool` → idénticos (aún en `/v1/chat/completions`, heredan los bugs de v1.x). Migración a v2.2.0.
- Chat card Lovelace → idéntica.
- ChainStore (`config/.storage/openclaw_chain.json`) → mismo formato; el chain de v2.0.0 se sigue respetando.

### Token saving esperado

- **Sin v2.1.0:** instructions ~13.000 chars + agente cargando AGENTS.md/SOUL.md/MEMORY.md por su cuenta.
- **Con v2.1.0:** instructions ~300 chars (un párrafo corto) + tools schema (~unos KB, depende de cuántas entidades estén expuestas). HA solo manda lo que el agente realmente necesita ver.

### Limitaciones / próximas

- Aún sin streaming (response llega completa). Streaming SSE con parsing del flujo de events de OpenResponses → v2.2.0.
- Service handlers siguen rotos. Migración → v2.2.0.
- `voluptuous_openapi.convert` se importa con fallback defensivo por si en alguna versión vieja de HA no está presente.

---

## [2.0.0] · 2026-05-10 — Migración interna a `/v1/responses` nativo

**Breaking change interno**, **upgrade transparente para el usuario**: si HACS te ofrece v1.0.3 → v2.0.0, aceptás, reiniciás HA, y todo sigue funcionando. La config entry vieja se sigue leyendo (mismos campos: `gateway_host`, `gateway_port`, `agent_id`, `assist_session_id`, etc.). No se borran sensores, ni botones, ni el select, ni el servicio `openclaw.send_message`, ni la chat card.

### Qué cambia (sólo internamente)

El **conversation agent** (la pieza que usa Assist) deja de hablar con `/v1/chat/completions` y pasa a hablar con `/v1/responses` directamente. El resto del código (coordinator, sensors, binary_sensor, button, select, event, services) **no se toca**.

### Por qué

`/v1/chat/completions` tiene un combo de bugs irreparables desde el cliente:

- Cualquier referencia a `session_id` (en payload o en cualquier header de session) **fuerza routing al agente default del gateway**, ignorando `model`. Verificado vía 3+ curl tests controlados el 2026-05-10.
- El gateway encima miente y devuelve `"model": "<el pedido>"` aunque internamente usó otro agente.

`/v1/responses` no tiene ninguno de los dos bugs. Con `user` estable + `previous_response_id` chain → routing real al agente + continuidad real de sesión + reuso de la **memoria built-in del gateway** (que recuerda hechos del usuario incluso a través de reinicios de HA).

Los workarounds de v1.0.0–v1.0.3 (derivar `model = openclaw/<agent>`, sacar `session_id` del payload, mantener historial client-side) **dejan de ser necesarios**. La nueva implementación es estructuralmente más simple.

### Pre-requisito de servidor

`/v1/responses` está deshabilitado por default en OpenClaw. En `~/.openclaw/openclaw.json`:

```json
"gateway": {
  "http": {
    "endpoints": {
      "responses": { "enabled": true }
    }
  }
}
```

Reiniciar OpenClaw y verificar:

```sh
curl -s -X POST http://<gateway>:18789/v1/responses \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"model":"openclaw/<agent>","input":"hola","user":"casajaz-nabuhome"}'
```

(Habilitado en producción del usuario el 2026-05-10.)

### Cambios concretos

- **Nuevo:** `custom_components/openclaw/chain_store.py` — persiste el `last_response_id` por `session_key` a `config/.storage/openclaw_chain.json`. Sobrevive restarts de HA.
- **Nuevo en `api.py`:** `OpenClawApiClient.async_send_responses(model, input_text, user, previous_response_id, instructions)` y `OpenClawApiClient.extract_responses_text(response)`. Métodos viejos (chat completions, models, tools invoke) se mantienen para uso del coordinator y del service handler.
- **Nuevo en `const.py`:** `API_RESPONSES = "/v1/responses"`.
- **Reescrito:** `conversation.py` — usa `async_send_responses()` con chain. Reusa la opción legacy `CONF_ASSIST_SESSION_ID` como `user`/session_key (cuando vacío, genera default estable `openclaw-<entry_id[:8]>-<agent>`). Instructions solo en primer turn de la chain.
- **Wiring:** `__init__.py async_setup_entry` instancia `ChainStore`, lo carga, y lo guarda en `hass.data[DOMAIN][entry_id]["chain_store"]`.

### Sin cambios (backwards compat)

- `coordinator.py`, `sensor.py`, `binary_sensor.py`, `button.py`, `select.py`, `event.py`, `helpers.py`, `exposure.py` → idénticos.
- `__init__.py` service handlers (`openclaw.send_message`, `openclaw.clear_history`, `openclaw.invoke_tool`) → idénticos. **Siguen usando chat-completions y por lo tanto tienen los mismos bugs de routing/sesión que documentamos para v1.x.** Migración a `/v1/responses` planificada para v2.1.0.
- `config_flow.py` → idéntico. Mismos campos. La opción "Assist Session ID override" que el usuario ya tenía seteada (`homeassistant-jaz`) se reusa como session_key del chain.
- `www/openclaw-chat-card.js` → idéntico.

### Limitaciones conocidas v2.0.0

- **No streaming** del lado responses: la respuesta llega completa (esto era streaming en v1.x). Streaming SSE nativo en v2.1.0.
- **Service handlers `openclaw.*` siguen en chat-completions** — heredan bugs de v1.x. Migración en v2.1.0.
- **Refresh de `instructions`**: solo en el primer turn de la chain. Para refrescar (ej. nuevas entidades expuestas), hay que vaciar el archivo `config/.storage/openclaw_chain.json`. UI/service para esto en v2.1.0.

### Para volver atrás

`git checkout v1.0.3 -- custom_components/openclaw` y reinstalar. La config entry sigue siendo compatible.

---

## [openclaw_agent v0.1.0] · 2026-05-10 — primera versión nativa (consolidada en v2.0.0)

> **NOTA HISTÓRICA**: esta integración paralela existió por unas horas el 2026-05-10 antes de consolidarse en `openclaw` v2.0.0. Su tag `openclaw-agent-v0.1.0` queda en el repo por trazabilidad. El folder `custom_components/openclaw_agent/` fue **eliminado** en v2.0.0.

Nueva integración HA construida desde cero sobre el endpoint nativo `/v1/responses` de OpenClaw. **Esta es la respuesta arquitectónica al combo de bugs que motivaron v1.0.0–v1.0.3 del fork de openclaw**.

### Por qué existe (resumen ejecutivo)

El endpoint `/v1/chat/completions` (OpenAI compatible) tiene un combo de bugs que hace imposible un routing estable agente+sesión desde el cliente:

- Cualquier `session_id` en payload, o `X-Session-Id`/`x-openclaw-session-key` en headers, **fuerza routing al agente default del gateway** ignorando el `model` pedido. Verificado empíricamente con 3 curl tests controlados el 2026-05-10.
- El gateway responde con `"model": "<el pedido>"` aunque internamente usó otro agente. La response miente.

`/v1/responses` no tiene ninguno de esos bugs. Con `user` estable + `previous_response_id` se obtiene routing real al agente + continuidad real de sesión + memoria persistente del propio gateway.

### Arquitectura

```
HA Assist
  ↓
custom_components/openclaw_agent/conversation.py
  ↓ POST /v1/responses
  {
    model:               "openclaw/nabu-home",
    input:               <user message>,
    user:                "casajaz-nabuhome",        // session_key estable
    previous_response_id: <last seen response.id>,  // chain
    instructions:         <only on first turn>
  }
  ↓
OpenClaw 192.168.10.40:18789
  → routes to nabu-home (vllm/qwen3.6-27b-autoround)
  → chains via previous_response_id
  → built-in memory persists facts about the user
```

### Pre-requisito de servidor

`/v1/responses` está deshabilitado por default. Hay que habilitarlo en `~/.openclaw/openclaw.json`:

```json
"gateway": {
  "http": {
    "endpoints": {
      "responses": { "enabled": true }
    }
  }
}
```

(Ya habilitado en producción del usuario el 2026-05-10.)

### Files

- `__init__.py` — entry point + setup del client + chain store
- `manifest.json` — `domain: openclaw_agent`, version 0.1.0
- `const.py` — keys + defaults (`DEFAULT_SESSION_KEY = "casajaz-nabuhome"`, `DEFAULT_AGENT_NAME = "nabu-home"`)
- `client.py` — POST /v1/responses + extracción de texto del response
- `chain_store.py` — persistencia del último `response.id` por `session_key` a disco (`config/.storage/openclaw_agent.chain`) — sobrevive restart de HA
- `conversation.py` — agent `AbstractConversationAgent` que arma payload, llama client, mantiene chain
- `config_flow.py` — UI inicial (URL, token, agente, session_key) + options flow + probe de conectividad
- `strings.json` + `translations/{en,es}.json` — UI strings
- `README.md` — doc de usuario para esta integración

### Limitaciones conocidas v0.1.0

- **No streaming** — la response llega completa. Para Qwen local puede demorar unos segundos. Streaming SSE nativo en v0.2.0.
- **No tool injection desde HA** — el agente usa las skills/MCP que ya tiene configuradas en OpenClaw. El acceso a HA se hace vía el server MCP de Home Assistant que ya está en el `openclaw-mcp-adapter`.
- **System instructions solo en primer turn de la chain** — para refrescar (ej. nuevas entidades expuestas), borrar el archivo `.storage/openclaw_agent.chain` o borrar la entrada del session_key específico.

### Para futuras versiones

- v0.2.0: streaming SSE
- v0.3.0: refresh periódico de instrucciones (cada N turnos o on-demand vía service)
- v0.4.0: usar entidades expuestas como instructions iniciales (paridad con `openclaw` v1.x include_exposed_entities_context)

---



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
