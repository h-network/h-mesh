import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from services.provider_probe import list_served_models, probe_messages_endpoint


class _FakeProvider(BaseHTTPRequestHandler):
    served_models = [{"id": "served-model"}]
    messages_response = {"type": "message", "content": [{"type": "text", "text": "hi"}]}
    messages_status = 200
    hang = False

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/v1/models":
            self._json(200, {"data": self.served_models})
        elif self.path == "/api/tags":
            self._json(200, {"models": [{"name": m["id"]} for m in self.served_models]})
        else:
            self._json(404, {})

    def do_POST(self):
        if self.path == "/v1/messages":
            if type(self).hang:
                import time
                time.sleep(0.3)
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if body.get("model") not in [m["id"] for m in self.served_models]:
                self._json(404, {"type": "error", "error": {"type": "NotFoundError"}})
                return
            self._json(type(self).messages_status, self.messages_response)
        else:
            self._json(404, {})

    def _json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def fake_provider():
    _FakeProvider.served_models = [{"id": "served-model"}]
    _FakeProvider.messages_response = {"type": "message", "content": [{"type": "text", "text": "hi"}]}
    _FakeProvider.messages_status = 200
    _FakeProvider.hang = False
    server = HTTPServer(("127.0.0.1", 0), _FakeProvider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_list_served_models_from_v1_models(fake_provider):
    assert list_served_models(fake_provider, "vllm") == ["served-model"]


def test_list_served_models_falls_back_to_ollama_tags(fake_provider):
    _FakeProvider.served_models = []

    class _OllamaOnly(_FakeProvider):
        def do_GET(self):
            if self.path == "/v1/models":
                self._json(404, {})
            elif self.path == "/api/tags":
                self._json(200, {"models": [{"name": "llama3:8b"}]})
            else:
                self._json(404, {})

    server = HTTPServer(("127.0.0.1", 0), _OllamaOnly)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        assert list_served_models(url, "ollama") == ["llama3:8b"]
        assert list_served_models(url, "vllm") == []
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_probe_succeeds_with_a_real_served_model(fake_provider):
    result = probe_messages_endpoint(fake_provider, "served-model", timeout=5)
    assert result.ok
    assert "claude can use this provider" in result.message


def test_probe_fails_on_404_for_unknown_model_without_condemning_the_endpoint(fake_provider):
    result = probe_messages_endpoint(fake_provider, "made-up-model", timeout=5)
    assert not result.ok
    assert "NotFoundError" in result.message or "404" not in result.message


def test_probe_fails_when_response_is_not_a_message(fake_provider):
    _FakeProvider.messages_response = {"type": "error", "error": {"message": "boom"}}
    result = probe_messages_endpoint(fake_provider, "served-model", timeout=5)
    assert not result.ok
    assert "answered, but not with a message" in result.message


def test_probe_reports_no_answer_on_timeout(fake_provider):
    _FakeProvider.hang = True
    result = probe_messages_endpoint(fake_provider, "served-model", timeout=0.05)
    assert not result.ok
    assert "did not answer" in result.message
