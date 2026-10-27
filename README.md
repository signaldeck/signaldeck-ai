# signaldeck-plugin-ai

SignalDeck integration for **MCP** (data access via ValueProvider) and a
generic **AG-UI 1.0 chat interface**.

The Python distribution is named `signaldeck-plugin-ai` (`pip install .`);
the import package and SignalDeck processor paths remain `signaldeck_ai`.
If an older `signaldeck-ai` distribution was installed, uninstall that
legacy name before reinstalling to avoid two distributions owning the
same import files: `python -m pip uninstall signaldeck-ai`.

## Docker

This uses the same container layout as `signaldeck-plugin-socketio`:
`Dockerfile`, `docker-compose.yml`, `config/`, `plugins/` and logging.
The demo Docker image tag `signaldeck-ai` is independent of the Python
package name.
The image is derived from locally available `signaldeck:dev` (including `signaldeck-plugin-main`).

```sh
docker compose up --build
```

The Compose service uses `network_mode: host` on Linux. This permits
SignalDeck to reach `cli-agent-api` at `127.0.0.1:8765` despite that
API's deliberate loopback-only binding. The SignalDeck website still
runs on its standard port 5000. The interactive demo uses `signaldeck-plugin-main` DummyData to publish three mocked temperatures (Garten, Wohnzimmer, Keller), half-hourly synthetic temperature time series and an opt-in action changing **only** the dummy garden temperature. The MCP example listens at
`127.0.0.1:8766/mcp`. Host networking does not provide network
isolation, and MCP must not be exposed to untrusted networks.

## Local demo / integration setup

The repository root `config/ai.json` defines an operational three-sensor DummyData setup and `config/group_ai.json` provides the DummyData controls plus MCP status and AG-UI chat. `docker-compose.yml` mounts `./config` at `/config`. The Dockerfile installs the AI plugin, the merged Socket.IO plugin from `main`, and their runtime dependencies; the main plugin is already present in the `signaldeck:dev` base image. Neither `config/` nor `tests/` is packaged in `signaldeck-plugin-ai` wheels. The CI builds a wheel and inspects its members to enforce this.

- **Garten:** 18.5 °C, synthetic trend of +0.35 °C per day, with 2.0 °C daily variation.
- **Wohnzimmer:** 21.3 °C, synthetic trend of −0.08 °C per day, with 0.5 °C daily variation.
- **Keller:** 17.8 °C, synthetic trend of +0.03 °C per day, with 0.2 °C daily variation.
- **Test action:** `set_temp_garten` changes only the simulated garden temperature, through the DummyData processor. No real hardware connection is configured.

Use `get_values({"fields": ["temp_garten"]})`, `get_history({"name": "hist_temp_garten", "date": "2026-10-25", "days": 7, "include_points": true})` or `execute_action({"name": "set_temp_garten", "arguments": {"temp_garten": 23.5}})` from a connected MCP client. Action calls can also exercise cli-agent approvals without affecting real devices.

For `get_history({\"name\": \"hist_temp_garten\", \"date\": \"2026-10-25\", \"days\": 7, \"include_points\": true})`, there are 48 actual datetime-indexed samples per completed day and the partial current day, so the MCP results have meaningful ISO-8601 timestamps (without `time_inferred`). The MCP result contains **every** measurement, without sampling. With cli-agent and OS read access, the entire result is saved in a temporary session file; the LLM receives summary metadata including `workspace_path` and `records_path: "/points"` to process the data using Data MCP.

While `signaldeck-plugin-main` [PR #7](https://github.com/signaldeck/signaldeck-plugin-main/pull/7) is under review, the demo Dockerfile and CI install its `feature/dummy-history-series` branch. **After that PR is merged**, switch the dependency back to the main release branch. These demo dependencies are not bundled in the AI plugin wheel.

The integration tests under `tests/test_demo_config.py` use the **real** DummyData and SDK ValueProvider, rather than a test-only replica. They verify current values, historical reads, the mock write action, and MCP tool discovery. An actual live Docker/cli-agent/AG-UI test still needs to be carried out on the target environment.

## Startup

1. Start SignalDeck; its MCP servers start immediately, while the Chat
   DisplayProcessor remains unconnected.
2. Configure cli-agent's Streamable HTTP MCP server to
   `http://127.0.0.1:8766/mcp` and start `cli-agent-api`: `cli-agent-api --approve-tool signaldeck_climate__get_values --approve-tool signaldeck_climate__get_history --port 8765 --config config_openrouter.toml`
3. Open SignalDeck's Chat DisplayProcessor, enter the bearer token printed
   by `cli-agent-api` into the password field, then click **Verbinden**.
   SignalDeck requires no restart or token configuration file.
4. Use the chat; **Verlauf zurücksetzen** discards the conversation and\n   outstanding approvals, starts a fresh agent thread and keeps the token\n   connection active. A confirmation is required. The former thread is\n   deleted from cli-agent on a best-effort basis. **Trennen** additionally\n   clears the server-side token and closes the associated agent threads.

Example cli-agent config:

```toml
[[mcp_servers]]
name = "signaldeck-climate"
transport = "streamable_http"
url = "http://127.0.0.1:8766/mcp"
```

## Demo: LLM via Script View

The developer config includes a `ScriptProcessor` (from
`signaldeck-plugin-main`) and an **LLM-Skript testen** View preselecting
`demo_llm`. Its single editable `prompt` string variable is interpolated
into the command:

```text
LLM $prompt$
```

Start `cli-agent-api`, open either **Agenten-Chat Energie** or
**Agenten-Chat Haus** and enter its bearer token once using **Verbinden**.
Then open **LLM-Skript testen**, change the prompt as desired and start
the script. The answer appears in the Script View's command output
(refresh the view if necessary). Before the Chat UI has authenticated
the AI processor, the LLM command is expected to fail. The script is
only a developer test; it does not modify real devices.

## Optional Socket.IO @haus one-shot bridge (v2)

The AI processor can optionally listen for **pre-filtered inbound** Socket.IO
events on the SignalDeck MessageBus. The Socket.IO plugin's
`incoming_message_routes` publishes only configured text commands after
normal client validation; the AI plugin has no direct Socket.IO dependency.

```json
{
  "socketio_bridge": {
    "routes": [
      {"room": "haus", "source": "socketio.ai",
       "response_source": "ai.socketio.haus"}
    ],
    "max_queue": 5,
    "timeout_seconds": 120
  }
}
```

This configuration belongs in the `AGUIChatProcessor.config`. The Socket.IO
plugin must configure its corresponding inbound route:

```json
{"incoming_message_routes": [
  {"rooms": ["haus"], "type": "chat",
   "prefix": "@haus", "source": "socketio.ai"}
 ],
 "message_routes": [
  {"source": "ai.socketio.haus", "rooms": ["haus"]}
 ]}
```

An incoming cleartext message `@haus Wie warm ist es?` is transformed by
the Socket.IO plugin into a MessageBus message containing only
`Wie warm ist es?`, its originating room, sender information and event ID.

The AI bridge queues the request on a dedicated worker (never blocking
Socket.IO), borrows the API token from an authenticated Chat view of
**this** AI processor, and runs a fresh one-shot AG-UI thread via the same
transport as the existing `LLM` Cmd. Each thread is deleted in
`finally` after the response or error; no room conversation history or
additional permanent cli-agent session is created. Only one request
executes at once; subsequent requests are bounded by `max_queue`. Duplicate
room/event IDs are ignored. Replies and safe error messages are published
with the configured `response_source` and routed by Socket.IO into the
original room. The bridge unsubscribes on shutdown.

Socket.IO encryption remains end-to-end and opaque: encrypted payloads
without a matching cleartext `@haus` prefix never reach this bridge.
Do not provide room keys to SignalDeck or cli-agent.

The development Docker image now installs the merged Socket.IO plugin
alongside the AI plugin. `config/ai.json` configures a non-persistent
Socket.IO room `haus` and both directions of message routing; the
`config/group_ai.json` group shows **Socket.IO – Übersicht** and
**Socket.IO – Haus**. The latter is a *server administration view*, not
a client chat interface. Messages sent using its server-side Send button
do not trigger `incoming_message_routes`: those only run for
authenticated Socket.IO **client** `send_event` calls.

To exercise the complete integration:

1. Start `cli-agent-api` (port 8765) with access to the SignalDeck demo
   MCP server at `http://127.0.0.1:8766/mcp`.
2. Run `docker compose up --build` in this repository.
3. Open **Agenten-Chat Energie** or **Agenten-Chat Haus** in SignalDeck,
   enter the cli-agent bearer token and press **Verbinden**.
4. From your host terminal, install the demo test client's dependency
   (`python -m pip install "python-socketio[client]>=5.12,<6"`)
   and run:

   ```sh
   python scripts/test_socketio_ai.py "@haus Wie warm ist es im Garten?"
   ```

The test client connects to `http://127.0.0.1:5000`, joins the
un-encrypted `haus` room, sends a genuine client-originated
`send_event`, and prints the assistant reply. Use `--url`, `--room`
or `--timeout` to adjust target and wait time. Without a prior Chat UI
login, the bridge returns an authentication-related message. Normal
messages without `@haus` are not forwarded to the AI bridge and will
not generate a response.

Interactive tool approvals are not granted automatically. Limit the
agent's configured tools to suitably trusted room participants; the
Socket.IO client's claimed `id` is not strongly authenticated. The
demo's permissive Socket.IO CORS setting and `network_mode: host`
are for trusted development networks, not public deployment.

## Optional one-shot LLM Cmd

Each `AGUIChatProcessor` can register a separate SignalDeck command.
The `Cmd` registry is global, so names **must be unique** and a collision
is rejected instead of silently replacing another command. Example processor
configuration:

```json
{
  "name": "agui",
  "class": "signaldeck_ai.chat_processor.AGUIChatProcessor",
  "config": {
    "endpoint": "http://127.0.0.1:8765/ag-ui",
    "cmd": {"name": "LLM", "timeout_seconds": 120}
  }
}
```

Invoke from a SignalDeck Cmd script:

```text
LLM Wie hoch war gestern die PV-Produktion?
```

All trailing Cmd arguments become one prompt. The command awaits the
response without blocking SignalDeck's asyncio event loop and stores the
final answer in `CmdResult.state` under the command's `msg` field.

**No token environment variable or persisted API key.** The command works
*only after* a user successfully connects at least one of **this same
processor instance's** Chat UI views to cli-agent. It borrows the API token
from the existing server-side `ChatRuntime` at the time of the invocation.
Different processor instances are not interchangeable. If nobody logged in,
the invocation fails before sending any request. Disconnecting the final
Chat UI view revokes availability until the next manual login. If several
views of the same instance are connected with **different API tokens**, the
command fails rather than arbitrarily picking one. A history reset does
not revoke the token.

Each invocation creates a **fresh cli-agent thread**, with one user prompt
and no previous Chat UI history or approvals. The AG-UI SSE response is
consumed to completion; interactive approval interrupts and frontend tool
handoffs fail rather than being auto-approved. An attempted
`DELETE /threads/{threadId}` in a `finally` block closes the temporary
MCP-owning cli-agent session on success, failure or timeout. Deletion
errors are logged. The command times out after 120 seconds by default,
configurable from 1 to 600 seconds. Commands have no shared conversation
history across invocations.

The demo registers `LLM` on its existing `agui` processor. This is a
server-side feature: an open browser is **not** required once a chat view
has connected. The connection is held in memory until explicitly
disconnected or SignalDeck is restarted.

**Current SDK logging caveat:** the central SignalDeck `Cmd._run_single`
logs whole command lines before and after execution. Prompts included in
`LLM` Cmd invocations are therefore visible in SignalDeck command logs.
Do not pass secrets as prompt arguments. The command never logs the
authentication token.

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
  "mcp": {
    "name": "house-climate",
    "host": "127.0.0.1",
    "port": 8766,
    "instructions": "Dieser MCP-Server stellt aktuelle und historische Klimadaten bereit."
  },
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

### MCP instructions and selective current values

Set `config.mcp.instructions` to a string to supply MCP server-level
instructions. These are provided through the standard MCP initialization
metadata (not embedded in individual tool descriptions).

`get_values(fields)` accepts an optional list of **local MCP-published names**.
For example, `{"fields": ["temp_garten"]}` reads only the garden temperature.
Omitting `fields` reads all published values; `[]` reads none. Unknown
or unpublished names are rejected before reading any values. The tool
description enumerates the permitted fields and their descriptions. Tool
names remain stable, which preserves cli-agent approval matching.

### Historical data

The shared `PersistData.hist(date=target_day, days=0, fullDay=True)` fetches one
individual day offset N, **not** an N-day window. For `mode=per_day`
the new MCP processor calls the same ValueProvider method for each
offset from 0 through N-1. Exact numeric count, min, max and mean are
computed in Python; raw points can be optionally returned in full by the MCP
tool, using cli-agent's metadata/file handoff to avoid a large LLM context.
Individual history mappings can restrict
the permitted `max_days`.

Use `mode=range` for exported methods that already support
`date=YYYY-MM-DD` as the inclusive start date and `days=N` as the number of days from that date onward.

History entries always use ISO 8601 date/time strings (for example,
`2026-10-23T09:10:00+00:00`) rather than synthetic `day-0` labels.
Actual timestamps from a Pandas Series or timestamp-indexed mapping are
preserved, including timezone information. A date-only index becomes a
midnight datetime with `time_inferred: true`. DummyData with `interval_minutes` configured now returns a **real Pandas
DatetimeIndex with multiple synthetic values per day**, so those mock samples
also carry proper timestamps and do not need `time_inferred`. Only legacy
scalar mock histories lacking timestamps receive an approximate UTC timestamp
and `time_inferred: true`. Those inferred timestamps are not measurement instants. Untimestamped multi-value sequences and scalar results in
`mode=range` now fail clearly instead of manufacturing index labels.


### Actions

Actions are optional and configured exclusively through
`config.mcp_data.actions`, with `func` pointing to a local
`config.methods[].name`. Each action needs a `description` and a JSON
Schema `parameters` object with `additionalProperties: false`.
Arguments are validated before calling the underlying ValueProvider
method. No method is published as an action implicitly. LLM-side user
approval remains important for real device changes.

### Named Cmd / script runs

`mcp_data.runs` explicitly publishes **named actions**, not arbitrary
commands, exported functions, or editable script content. Each alias maps
either to a registered SignalDeck Cmd command or to an existing script.
The LLM sees a single stable MCP tool `run(name, arguments?)` regardless
of how many actions are configured.

Example (inside the MCP processor's `config.mcp_data`):

```json
{
  "runs": [
    {
      "name": "Abend",
      "description": "Aktiviere den vorbereiteten Abendmodus.",
      "script": "abendmodus"
    },
    {
      "name": "Licht",
      "description": "Schalte eine ausdrücklich freigegebene Lichtzone.",
      "command": "licht",
      "args": [{"param": "zone"}, {"param": "state"}],
      "parameters": {
        "type": "object",
        "properties": {
          "zone": {"type": "string", "enum": ["wohnzimmer", "kueche"]},
          "state": {"type": "string", "enum": ["ein", "aus"]}
        },
        "required": ["zone", "state"],
        "additionalProperties": false
      }
    }
  ]
}
```

A Socket.IO message action can fix its destination room while allowing the
LLM to supply the message text. With the actual
`signaldeck-plugin-socketio` command **`socketio_send`**, configure:

```json
{
  "name": "send_haus",
  "description": "Sende eine Textnachricht an den Raum haus.",
  "command": "socketio_send",
  "args": ["haus", {"param": "msg"}],
  "parameters": {
    "type": "object",
    "properties": {
      "msg": {
        "type": "string",
        "description": "Eine kurze Nachricht an den Haus-Chat",
        "minLength": 1,
        "maxLength": 500
      }
    },
    "required": ["msg"],
    "additionalProperties": false
  }
}
```

Then the public call
`run({"name": "send_haus", "arguments": {"msg": "Hallo aus SignalDeck!"}})`
starts `socketio_send haus Hallo aus SignalDeck!`. The room name cannot be
changed by the LLM, and the Cmd name is never exposed as an argument.
This requires that the Socket.IO plugin is installed and its command
registered before the MCP processor is initialized. The AI demo does not
install Socket.IO, so its `Nachricht` action uses safe `echo` instead.

The example `licht` command and `abendmodus` script must be defined in
SignalDeck separately; neither is installed by this plugin. The actual
repository demo instead uses harmless built-in `echo` commands and scripts.

`run({"name": "Abend"})` starts the fixed script without any client-visible
script name. `run({"name":"Licht", "arguments":{"zone":"kueche","state":"aus"}})`
starts only the fixed command with these validated tokens.

Configuration rules:
- Public aliases are unique and must have a description.
- Exactly one of `command` or `script` is required per alias.
- A command uses a fixed registered Cmd command and positional `args`.
  Each argument is either a fixed literal or `{"param":"..." }`.
- A script uses a fixed existing script and optional `variables` mapping.
  Every passed variable must exist in its ScriptDefinition.
- Every exposed parameter must be required. By default, string
  parameters require explicitly enumerated **single safe tokens**.
  For actual natural-language text, opt in with `minLength` (optional,
  default 1) and `maxLength` (mandatory, 1–500). The text must be
  nonempty, printable, without control/format characters, newlines or
  leading/trailing whitespace. Such free text is accepted only as the
  **last argument of a Cmd command** that supports joining trailing words;
  free text is not allowed in script variables. Numeric arguments need
  finite bounds or an enum. Unexpected properties are forbidden.
  SignalDeck Cmd uses space-separated arguments rather than shell execution,
  so only commands designed for multiword text are appropriate.
- Cmd/script targets and their source strings never appear in the tool's
  public descriptions. The LLM cannot upload script text or submit arbitrary
  command lines.
- `run` returns `{"name":"Abend","status":"scheduled","run_id":"..."}`
  for named scripts (no `run_id` for single Cmd commands) when scheduling
  succeeds. Multiple instances of the same script may run concurrently.
  The ScriptProcessor displays their separate status, variables and output,
  and lets users stop an individual run. Scheduling does **not** imply that
  asynchronous execution succeeded; inspect the run's status for failures.
- The diagnostic UI marks `run` as a state-changing operation and requires
  explicit confirmation. cli-agent permission for the generic `run` tool
  covers **every** action in the server's configured run allowlist, so
  only group appropriately trusted actions on a given MCP server.

`execute_action` continues to provide an optional low-level
ValueProvider-method integration. For house automation, prefer `run`.

### Multiple MCP servers

Add another `MCPServerProcessor` with a distinct port and its own
value/method allowlist. Each instance publishes its own
`/mcp` endpoint and DisplayData status/tool list.

### MCP-Server-Oberfläche und Tool-Testkonsole

Der MCP-DisplayProcessor zeigt die vom **tatsächlichen FastMCP-Server**
registrierten Tools, ihre ausführlichen Beschreibungen und ihre
generierten JSON-Eingabe-/Ausgabeschemas. Die Ansicht ist kein separat
gepflegtes Toolverzeichnis: sie liest die FastMCP-Registry.

Jedes Tool lässt sich aus der SignalDeck-Oberfläche ausprobieren:

1. MCP-Server-Panel öffnen und ein Tool aufklappen.
2. Das JSON-Eingabefeld bearbeiten (für lesende Tools werden Beispiele
   aus der `mcp_data`-Konfiguration vorgeschlagen).
3. `Tool ausführen` anklicken und die serialisierte MCP-Rückgabe ansehen.

Alle Testaufrufe laufen über SignalDeck `POST /run` und den bestehenden
ActionDispatcher/DisplayProcessor, nicht über einen ungeschützten zweiten
HTTP-Endpunkt. Die Ausführung nutzt `FastMCP.call_tool` und damit die
**gleichen registrierten Handler und Eingabeschemas** wie ein externer
MCP-Client. Es wird kein cli-agent für das Testen benötigt.

`execute_action` ist als Schreibaktion markiert und verlangt ein
zusätzliches Bestätigungs-Kontrollkästchen. Die Bestätigung wird auch
serverseitig geprüft. **Wichtig:** Diese Bestätigung ersetzt keine
Authentifizierung oder Autorisierung. In produktiven Installationen
muss der SignalDeck-Webzugriff geschützt werden; die Testkonsole kann
alle konfigurierten MCP-Schreibaktionen ausführen. Der Demo-Prozessor
verändert ausschließlich Dummy-Werte.

## AG-UI

`AGUIChatProcessor` integrates the generic AG-UI 1.0 HTTP/SSE transport with
SignalDeck's existing action system. The browser sends `connect`, `send`,
`approve`, `reset_history` (`new_chat` remains a compatibility alias) and `disconnect` as normal SignalDeck `/run` actions,
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

### Gemeinsame Chats pro View-`value`

Der `AGUIChatProcessor` verwaltet **eine Session pro konfiguriertem
`value`**. Die Zuordnung ist ausschließlich serverseitig und gilt für die
jeweilige Processor-Instanz. Verschiedene Werte haben getrennte Agenten-Threads,
Nachrichten und ausstehende Tool-Freigaben. Identische Werte teilen sich
dieselbe Session, **auch in verschiedenen Browsern**. SignalDeck benötigt
hierfür keine Browser-/Benutzersessions.

Beim Öffnen fragt die Oberfläche über die native `POST /run`-Aktion
`ai_action=state` den Chatstatus ab und stellt den Verlauf wieder her.
Während eine andere View desselben Werts antwortet, wird der Verlauf
regelmäßig aktualisiert; parallele neue Anfragen werden gesperrt. Der
API-Token bleibt im SignalDeck-Serverprozess, wird nicht in der Browser-UI
übertragen oder dauerhaft gespeichert. Der zurückgegebene
`connection_id`-Handle ermöglicht allerdings jedem Browser mit Zugriff
auf dieselbe View, die gemeinsame Agentenverbindung zu benutzen: **SignalDeck
muss daher für vertrauenswürdige Nutzer geschützt erreichbar sein.**

Mit `Verlauf zurücksetzen` wird nur der Thread des jeweiligen Werts
zurückgesetzt. `Trennen` beendet die gemeinsame Verbindung für alle
gleichzeitig geöffneten Ansichten dieses Werts. Die gespeicherten Chats
überleben einen Seitenwechsel und das Schließen des Browsers, **nicht** den
Neustart von SignalDeck. Sie bleiben bis `Trennen` oder Server-Neustart
im Speicher; die 30-Minuten-Leerlaufgrenze gilt weiterhin nur für
nicht benannte Legacy-Verbindungen.

Die Demo unter `config/group_ai.json` enthält zwei Chat-Views des selben
Prozessors mit `value=["energie"]` und `value=["haussteuerung"]`.
Sie lassen sich unabhängig voneinander testen.

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
connection handle held only in its in-page JavaScript state. Connections scoped to a named view remain in server memory until disconnect or shutdown. Only standalone legacy connections expire after 30 minutes of inactivity.
Frontend-provided AG-UI tools, multi-interrupt and multimodal handling are
not implemented yet.

## Tests and status

```sh
python -m pytest
```

This is an **initial implementation**, not an end-to-end-validated
release. It still needs integration testing with a real SignalDeck
instance, MCP server and cli-agent-api.

### Date-based history calls

`get_history` requires `date` in ISO calendar format (`YYYY-MM-DD`).
`days` is now the number of days **starting on that explicit date**
(default 1), not a relative offset from now. Example:

```json
{"name":"hist_temp_garten","date":"2026-10-20","days":2,"include_points":true}
```

This selects October 20 and October 21. Each selected day is dispatched
through the existing SignalDeck `hist(date=<that day>, days=0,
fullDay=True)` API: the underlying `days` argument is an offset, hence
**must remain zero** to avoid accidentally selecting an earlier day.
Optional `start_time` and `end_time` (`HH:MM` or `HH:MM:SS`, local clock time)
select the **same time window on every day** in the requested range:

```json
{"name":"hist_temp_garten","date":"2026-10-20","days":2,"start_time":"06:30","end_time":"10:30","include_points":true}
```

The start is inclusive and the end is exclusive (`06:30 <= time < 10:30`).
Either bound may be omitted to include the rest of that calendar day.
Overnight windows, where the start is not earlier than the end, are rejected;
use separate calls for windows crossing midnight. Filtering occurs after
combining stored measurements with any eligible current-day cache, but
**before** calculating statistics or returning points. The response
echoes both optional time bounds; aggregate counts, extrema and mean apply
only to the filtered samples. Time filtering requires real measurement
timestamps, not inferred midnight timestamps for scalar daily values.

Request validation rejects future or invalid calendar dates and respects
the individual `max_days` limits. Scalar historical results are marked
`time_inferred` with the requested calendar day rather than the current
time. Previously saved requests containing only `days` need a `date`.

### Point-in-time historical lookup

Use `get_historical_value` when you need **one** historical value instead of
all samples for a calendar day. The `date` parameter MUST contain both date
and local time, including seconds: `YYYY-MM-DDTHH:MM:SS`. A date without a
time or a timezone-offset string is rejected.

```json
{"name": "pv_power_history", "date": "2026-10-20T14:30:00"}
```

This uses SignalDeck's existing `hist(date=..., days=0, fullDay=False)`.
It returns the **best matching value chosen by the configured datastore**;
it does not promise that the measurement was recorded at exactly 14:30:00.
The response contains `requested_time`, `value`, the configured
description and optional unit, not a full series or daily statistics.

`get_history` still supports full calendar-day time series, including
its explicit start date, optional multi-day window, points/statistics and
today's optional cache merge. Both tools share the same explicit
`mcp_data.data_rows` allowlist. Custom `mode="range"` history providers
are not eligible for a single-value lookup.

The MCP diagnostic console shows only FastMCP's structured tool result,
instead of also showing a second escaped JSON copy from its text blocks.
The external MCP response consumed by cli-agent is not changed.

### In-memory measurements on the current day

For a requested date window that includes **today**, `get_history`
queries both the normal persisted day (`hist(date=..., days=0,
fullDay=True)`) and the in-memory measurement buffer using
`hist(currentValues=True)`. Only actual timestamped samples for today's
calendar date are merged; any matching timestamp is counted once, with
the cache value taking precedence. All aggregate statistics and optional
`points` are calculated **after** merging, without output sampling.
For earlier days the cache is never requested.

A processor without `cacheSize` (or with an empty cache) simply returns
the persisted readings. Some synthetic/demo processors do not implement
timestamped cache data; scalar responses from those processors are ignored
for the optional merge. Neither `date` nor `days` of the normal daily
history query is changed.


## Einklappbare Tool-Aktivitäten im AG-UI-Chat

Die Chatansicht sammelt alle vom cli-agent bereits gesendeten
`TOOL_CALL_START`- und `TOOL_CALL_RESULT`-Ereignisse in einer
**einklappbaren Übersicht pro Agentenlauf**. Das gilt ausdrücklich
auch für automatisch freigegebene MCP-Tools; neue AG-UI-Events oder
Änderungen an cli-agent sind nicht erforderlich.

Die Anzeige verwendet nur **Toolname und Ausführungsstatus**.
Fehlende Argumente werden weder angefordert noch angezeigt.
Die MCP-Rückgabewerte erscheinen ebenfalls nicht in dieser Übersicht.
Eine unterbrochene Freigabe und deren Fortsetzung aktualisieren
denselben Tool-Eintrag statt ihn zu verdoppeln.

Die Übersicht wird nur in der **serverseitigen Darstellungs-Historie**
gespeichert, getrennt von den Nachrichten für cli-agent. Deshalb
bleiben die Aufrufe nach Seitenwechseln und automatischer Synchronisierung
sichtbar, ohne den LLM-Kontext zu vergrößern. Sie werden mit
**Verlauf zurücksetzen** gelöscht. Die Anzahl der im Speicher
verfolgten Tool-Aufrufe pro Chat ist auf 1024 begrenzt; bei einem
SignalDeck-Neustart geht die In-Memory-Darstellungs-Historie verloren.

## Markdown in Chat-Antworten

Assistant-Nachrichten werden beim Streaming und beim Wiederherstellen des
Chatverlaufs als Markdown gerendert. Unterstützt werden insbesondere
Überschriften, Fett-/Kursivschrift, Listen, Links, Tabellen und Codeblöcke.
Nutzernachrichten sowie Toolnamen/-status bleiben reiner Text.

Die Darstellung verwendet eine lokal ausgelieferte, fest eingebundene Kopie
von `markdown-it` 14.1.0 (`static/js/markdown-it.min.js`, Lizenz daneben).
HTML in Modellantworten wird escaped, gefährliche Link-Protokolle werden
abgewiesen und externe Markdown-Bilder nicht automatisch geladen. Bei
fehlender Renderer-Bibliothek erscheint die Antwort weiterhin als Klartext.
