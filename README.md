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
3. Put the bearer token printed by cli-agent-api into the environment
   variable `CLI_AGENT_API_TOKEN` used by Docker Compose (restart
   SignalDeck to refresh its process environment).
4. Click **Verbinden** in the SignalDeck chat panel.

Example cli-agent config:

```toml
[[mcp_servers]]
name = "signaldeck-climate"
transport = "streamable_http"
url = "http://127.0.0.1:8766/mcp"
```

## The SignalDeck ValueProvider configuration

MCP reuses the existing SignalDeck `Processor.refresh()` mechanism,
without inventing a new way to reference data:

- `values`: each item has `type: "field"` or `"method"`,
  `name`, and `from`. Additional `description` and optional
  `unit` are passed to the MCP user.
- `methods`: each item has `name` and `from`, just like any
  SignalDeck consumer of a published method. Additional
  `kind: "history"` or `"action"` limits its MCP exposure.
- Producers must explicitly export these names in their own
  `config.export.values` and `config.export.methods`.

Example subset of an MCP processor's `config`:

```json
{
  "mcp": {"name": "house-climate", "host": "127.0.0.1", "port": 8766},
  "values": [
    {
      "type": "field",
      "name": "basement_temperature",
      "from": "temperature_basement",
      "description": "Temperature in the basement",
      "unit": "°C"
    }
  ],
  "methods": [
    {
      "name": "basement_history",
      "from": "hist_temperature_vorratsraum",
      "kind": "history",
      "description": "Recorded temperature in the basement",
      "mode": "per_day",
      "max_days": 30,
      "unit": "°C"
    }
  ]
}
```

`temperature_basement` is just an example producer export name.
`hist_temperature_vorratsraum` is exported by the Ecowitt
example config. The exporting producer must be included in the same
SignalDeck configuration; the minimal `config/ai.json` contains no
producer, so its initial MCP server publishes an empty current-values
tool until you configure bindings.

### Historical data

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

Action methods must be explicitly listed with `kind: "action"`,
a nonempty `description`, and a JSON Schema `parameters` object
with `additionalProperties: false`. Arguments are validated before
calling `ValueProvider.getMethodValue()`. LLM-side user approvals
remain strongly recommended for actual device changes.

### Multiple MCP servers

Add another `MCPServerProcessor` with a distinct port and its own
value/method allowlist. Each instance publishes its own
`/mcp` endpoint and DisplayData status/tool list.

## AG-UI

`AGUIChatProcessor` is a general HTTP/SSE AG-UI 1.0 client, not a
cli-agent-specific adapter. The Flask proxy forwards standard
`RunAgentInput`, returns SSE events and handles approval
interrupt/resume. The client connects manually after startup.
The cli-agent-specific `GET /ag-ui/capabilities` health/discovery URL
and `DELETE /threads/{threadId}` lifecycle URL are optional
conventions; a broader AG-UI interoperability layer may need
different discovery and cleanup behavior.

### Security

The cli-agent bearer token is read from a server-side environment
variable and never exposed to browser JS. The SignalDeck proxy permits
configured loopback endpoints only. Browser state-changing requests
check same-origin `Origin` headers (CSRF defense, **not user login**).
You must separately secure the SignalDeck web application: otherwise
any web user could request AI actions under the configured agent's
permissions. Do not expose an unauthenticated MCP endpoint outside
loopback. Docker host mode shares the host network namespace.

The first browser UI supports textual messages, tool-status events,
and a single approval interrupt. Frontend-provided AG-UI tools,
multi-interrupt and multimodal handling are not implemented yet.

## Tests and status

```sh
python -m pytest
```

This is an **initial implementation**, not an end-to-end-validated
release. It still needs integration testing with a real SignalDeck
instance, MCP server and cli-agent-api.
