"""DisplayData-backed generic AG-UI chat configuration."""
from __future__ import annotations

from urllib.parse import urlsplit
from signaldeck_sdk import DisplayProcessor, DisplayData


class ChatDisplayData(DisplayData):
    def __init__(self, ctx, action_hash, processor):
        super().__init__(ctx, action_hash)
        self.name = processor.name
        self.api_base = f"/plugin/ai/api/chat/{processor.name}"


class AGUIChatProcessor(DisplayProcessor):
    """UI is always ready; no connection is opened during SignalDeck startup."""

    def __init__(self, name, config, ctx, valueProvider, collect_data):
        super().__init__(name, config, ctx, valueProvider, collect_data)
        endpoint = str(config.get("endpoint", "")).rstrip("/")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/ag-ui"}
        ):
            raise ValueError("AG-UI endpoint must be http://localhost:<port>/ag-ui")
        if parsed.port is None or parsed.port < 1 or parsed.port > 65535:
            raise ValueError("AG-UI endpoint requires a TCP port")
        self.endpoint = endpoint if parsed.path == "/ag-ui" else endpoint + "/ag-ui"
        self.api_token_env = str(config.get("api_token_env", "CLI_AGENT_API_TOKEN"))
        if not self.api_token_env.isidentifier():
            raise ValueError("api_token_env must be an environment variable name")

    def getTemplate(self, value):
        return "ai/chat.html"

    def getDisplayData(self, value, actionHash, **kwargs):
        return ChatDisplayData(self.ctx, actionHash, self)

    def getAdditionalJsFiles(self, value):
        return [("signaldeck_ai", "js/chat.js")]

    def getAdditionalCssFiles(self, value):
        return [("signaldeck_ai", "css/chat.css")]

    def getJS_functions_to_call_on_client(self, data):
        return {f"signaldeck-ai-chat-{data.hash}": "initSignalDeckAIChat"}
