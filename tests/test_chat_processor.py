from types import SimpleNamespace

import pytest

from signaldeck_ai.chat_processor import AGUIChatProcessor


class Provider:
    def registerProcessor(self, processor, export):
        pass


def test_chat_config_is_offline_at_startup():
    processor = AGUIChatProcessor(
        "chat", {"endpoint": "http://127.0.0.1:8765/ag-ui"},
        SimpleNamespace(), Provider(), False,
    )
    assert processor.endpoint == "http://127.0.0.1:8765/ag-ui"
    assert processor.api_token_env == "CLI_AGENT_API_TOKEN"


@pytest.mark.parametrize("url", [
    "https://evil.example/ag-ui", "http://evil.example:8765/ag-ui",
    "http://127.0.0.1:8765/path", "http://127.0.0.1:8765/ag-ui?token=x",
])
def test_chat_config_rejects_untrusted_endpoints(url):
    with pytest.raises(ValueError):
        AGUIChatProcessor("chat", {"endpoint": url}, SimpleNamespace(), Provider(), False)
