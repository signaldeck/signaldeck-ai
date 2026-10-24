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
    assert not hasattr(processor, "api_token_env")
    assert processor.connection("missing") is None


@pytest.mark.parametrize("url", [
    "https://evil.example/ag-ui", "http://evil.example:8765/ag-ui",
    "http://127.0.0.1:8765/path", "http://127.0.0.1:8765/ag-ui?token=x",
])
def test_chat_config_rejects_untrusted_endpoints(url):
    with pytest.raises(ValueError):
        AGUIChatProcessor("chat", {"endpoint": url}, SimpleNamespace(), Provider(), False)


def test_connection_tokens_are_ephemeral_and_thread_scoped():
    p = AGUIChatProcessor(
        "chat", {"endpoint": "http://127.0.0.1:8765/ag-ui"},
        SimpleNamespace(), Provider(), False,
    )
    first = p.connect("first-token")
    second = p.connect("second-token")
    assert p.connection(first).token == "first-token"
    assert p.claim_thread(first, "thread-1") is not None
    assert p.claim_thread(second, "thread-1") is None
    assert p.owns_thread(first, "thread-1")
    assert not p.owns_thread(second, "thread-1")
    assert p.disconnect(first).token == "first-token"
    assert p.connection(first) is None
    assert p.claim_thread(second, "thread-1") is not None
    p.shutdown()
    assert p.connection(second) is None
