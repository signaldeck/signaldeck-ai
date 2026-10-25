# signaldeck-ai

SignalDeck integration for **MCP** (data access via ValueProvider) and a
generic **AG-UI 1.0 chat interface**.

## Docker

This uses the same container layout as `signaldeck-plugin-socketio`:
`Dockerfile`, `docker-compose.yml`, `config/`, `plugins/` and logging.
The image is derived from locally available `signaldeck:dev`.

```sh
docker compose up --build
```

The Compose service uses `network_mode: host` on Linux. This permits
SignalDeck to reach `cli-agent-api` at `127.0.0.1:8765` despite that
API's deliberate loopback-only binding. The SignalDeck website still
runs on its standard port 5000. The MCP example listens at
`127.0.0.1:8766/mcp`. Host networking does not provide network
isolation, and MCP must not be exposed to untrusted networks.

## Startup

1. Start SignalDeck; its MCP servers start immediately, while the Chat
   DisplayProcessor remains unconnected.
2. Configure cli-agent's Streamable HTTP MCP server to
   `http://127.0.0.1:8766/mcp` and start `cli-agent-api`.
3. Open SignalDeck's Chat DisplayProcessor, enter the bearer token printed
   by `cli-agent-api` into the password field, then click **Verbinden**.
   SignalDeck requires no restart or token configuration file.
4. Use the chat; clicking **Trennen** clears the server-side token and closes
   the associated agent threads.

Example cli-agent config:

```toml
[[mcp_servers]]
name = "signaldeck-climate"
transport = "streamable_http"
url = "http://127.0.0.1:8766/mcp"
```

## ValueProvider mappings and MCP publication

The processor deliberately separates SignalDeck data resolution from MCP
publication:

- `config.values` and `config.methods` use **unmodified** SignalDeck SDK
  configuration. Their `name` keys establish local names and their `from`
  keys reference exports from other processors.
- `config.mcp_data.values` chooses permitted local values by `field`.
- `config.mcp_data.data_rows` chooses permitted local methods by `func`.
- `description` is defined **only** in `mcp_data`. A configured value or
  method is **not** automatically exposed through MCP.

Example of an MCP processor's `config`:

```json
{
  "mcp": {\n    "name": "house-climate",\n    "host": "127.0.0.1",\n    "port": 8766,\n    "instructions": "Dieser MCP-Server stellt aktuelle und historische Klimadaten bereit."\n  },
  "values": [
    {"name": "temp_garten", "type": "field", "from": "temperature_garten"},
    {"name": "temp_intern", "type": "field", "from": "another_export"}
  ],
  "methods": [
    {"name": "hist_temp_garten", "from": "hist_temperature_garten"}
  ],
  "mcp_data": {
    "values": [
      {
        "field": "temp_garten",
        "description": "Aktuelle Temperatur im Garten",
        "unit": "°C"
      }
    ],
    "data_rows": [
      {
        "func": "hist_temp_garten",
        "description": "Temperaturverlauf im Garten",
        "max_days": 30
      }
    ]
  }
}
```

Here `temperature_garten` illustrates a ValueProvider export name; your
producing processor must actually publish that exact alias. The exported
historical method `hist_temperature_garten` occurs in the Ecowitt example.
`temp_intern` deliberately remains unavailable through MCP.

At startup, every `mcp_data.values[].field` must refer to a local
`config.values[].name`, and every `mcp_data.data_rows[].func` must refer
to a local `config.methods[].name`. Invalid entries fail configuration
validation rather than exposing other fields by accident.

Read calls resolve **only** allowlisted ValueProvider references and never
invoke unrelated entries while refreshing a processor.

### MCP instructions and selective current values\n\nSet `config.mcp.instructions` to a string to supply MCP server-level\ninstructions. These are provided through the standard MCP initialization\nmetadata (not embedded in individual tool descriptions).\n\n`get_values(fields)` accepts an optional list of **local MCP-published names**.\nFor example, `{"fields": ["temp_garten"]}` reads only the garden temperature.\nOmitting `fields` reads all published values; `[]` reads none. Unknown\nor unpublished names are rejected before reading any values. The tool\ndescription enumerates the permitted fields and their descriptions. Tool\nnames remain stable, which preserves cli-agent approval matching.\n\n### Historical data

The shared `PersistData.hist(days=N, fullDay=True)` fetches one
individual day offset N, **not** an N-day window. For `mode=per_day`
the new MCP processor calls the same ValueProvider method for each
offset from 0 through N-1. Exact numeric count, min, max and mean are
computed in Python; raw points can be optionally returned (sampled
to at most 1000 points). Individual history mappings can restrict
the permitted `max_days`.

Use `mode=range` for exported methods that already support
`days=N` to return a complete range.

### Actions

Actions are optional and configured exclusively through
`config.mcp_data.actions`, with `func` pointing to a local
`config.methods[].name`. Each action needs a `description` and a JSON
Schema `parameters` object with `additionalProperties: false`.
Arguments are validated before calling the underlying ValueProvider
method. No method is published as an action implicitly. LLM-side user
approval remains important for real device changes.

### Multiple MCP servers

Add another `MCPServerProcessor` with a distinct port and its own
value/method allowlist. Each instance publishes its own
`/mcp` endpoint and DisplayData status/tool list.

## AG-UI

`AGUIChatProcessor` integrates the generic AG-UI 1.0 HTTP/SSE transport with
SignalDeck's existing action system. The browser sends `connect`, `send`,
`approve`, `new_chat` and `disconnect` as normal SignalDeck `/run` actions,
dispatched through `AGUIChatProcessor.process()` and `performActions()`.
A background worker sends standard `RunAgentInput` requests to the agent.
**Only** server-to-browser SSE events have a separate, read-only route at
`GET /plugin/ai/api/chat/<processor>/events/<run_id>`; the browser sends its
opaque connection handle in the `X-SignalDeck-AI-Connection` header.
The client connects manually after startup. Because SignalDeck's inherited
`DisplayProcessor.process()` would rerender the chat element (destroying the
live event stream), this processor overrides `process()` solely to return
JSON action acknowledgments while leaving the DOM intact.

The cli-agent-specific `GET /ag-ui/capabilities` discovery URL and
`DELETE /threads/{threadId}` lifecycle URL are presently used for connection
validation and cleanup. Other AG-UI servers may need different discovery and
cleanup mechanisms.

### Security

The cli-agent bearer token is provided in the SignalDeck Chat UI at connection
time and kept only in server-side process memory until disconnect or idle
expiration (30 minutes). The UI clears the password field after connection
and keeps only an opaque connection handle in JavaScript memory; neither token
nor handle is stored in browser storage or DisplayData. Avoid placing
SignalDeck behind an untrusted HTTP proxy, because the initial token entry
travels through SignalDeck's HTTP endpoint. The SignalDeck proxy permits
configured loopback endpoints only. All browser state-changing requests use SignalDeck's `/run` action dispatcher;
the chat processor requires **POST** with a same-origin `Origin` header.
The existing core `/run` endpoint also accepts GET for other processors,
but AG-UI chat actions explicitly reject GET. This is CSRF mitigation,
**not user authentication**.
You must separately secure the SignalDeck web application: otherwise
any web user could request AI actions under the configured agent's
permissions. Do not expose an unauthenticated MCP endpoint outside
loopback. Docker host mode shares the host network namespace.

The first browser UI supports textual messages, tool-status events,
and a single approval interrupt. Runs execute on worker threads and bounded
SSE replays keep the SignalDeck `/run` handler responsive. The browser
never receives the agent API token, but does receive a temporary opaque
connection handle held only in its in-page JavaScript state. Credentials
expire from server memory after 30 minutes of inactivity or on disconnect.
Frontend-provided AG-UI tools, multi-interrupt and multimodal handling are
not implemented yet.

## Tests and status

```sh
python -m pytest
```

This is an **initial implementation**, not an end-to-end-validated
release. It still needs integration testing with a real SignalDeck
instance, MCP server and cli-agent-api.
