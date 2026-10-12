import json
from prd2tc.config import Config
from prd2tc.llm import AnthropicClient, OllamaClient, OpenAIClient, create_client

class Response:
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return json.dumps(self.payload).encode()

def config(provider, base, key="key"):
    # 本文件校验「非流式」请求/响应形状（stream=False 即 --repro 走的路径）；
    # 流式协议见 tests/test_llm_stream.py。
    return Config(key, base, "model", .2, 10, 1, provider=provider, stream=False)

def test_openai_adapter_request_and_response(monkeypatch):
    captured = {}
    def open_url(request, timeout):
        captured.update(url=request.full_url, body=json.loads(request.data), headers=dict(request.header_items()))
        return Response({"model":"served","choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":2,"completion_tokens":3}})
    monkeypatch.setattr("urllib.request.urlopen", open_url)
    result = OpenAIClient(config("openai", "https://api.openai.com/v1")).chat("hello", system="system")
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["body"]["messages"][1]["content"] == "hello"
    assert any(k.lower() == "authorization" and v == "Bearer key" for k, v in captured["headers"].items())
    assert (result.content, result.model, result.total_tokens) == ("ok", "served", 5)

def test_anthropic_adapter_request_and_response(monkeypatch):
    captured = {}
    def open_url(request, timeout):
        captured.update(url=request.full_url, body=json.loads(request.data), headers=dict(request.header_items()))
        return Response({"model":"claude","content":[{"type":"text","text":"part1"},{"type":"text","text":"part2"}],"usage":{"input_tokens":4,"output_tokens":6}})
    monkeypatch.setattr("urllib.request.urlopen", open_url)
    result = AnthropicClient(config("anthropic", "https://api.anthropic.com")).chat("hello", system="system")
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["body"]["system"] == "system" and captured["body"]["max_tokens"] == 16384
    assert any(k.lower() == "x-api-key" and v == "key" for k, v in captured["headers"].items())
    assert (result.content, result.total_tokens) == ("part1part2", 10)

def test_anthropic_v1_base_is_not_duplicated(monkeypatch):
    captured = {}
    def open_url(request, timeout):
        captured["url"] = request.full_url
        return Response({"content":[{"type":"text","text":"ok"}],"usage":{}})
    monkeypatch.setattr("urllib.request.urlopen", open_url)
    AnthropicClient(config("anthropic", "https://api.anthropic.com/v1")).chat("hello")
    assert captured["url"] == "https://api.anthropic.com/v1/messages"

def test_ollama_adapter_and_factory(monkeypatch):
    captured = {}
    def open_url(request, timeout):
        captured.update(url=request.full_url, body=json.loads(request.data), headers=dict(request.header_items()))
        return Response({"model":"qwen","message":{"content":"local"},"prompt_eval_count":7,"eval_count":8})
    monkeypatch.setattr("urllib.request.urlopen", open_url)
    client = create_client(config("ollama", "http://localhost:11434", ""))
    assert isinstance(client, OllamaClient)
    result = client.chat("hello")
    assert captured["url"] == "http://localhost:11434/api/chat"
    assert "Authorization" not in captured["headers"]
    assert (result.content, result.total_tokens) == ("local", 15)

def test_config_selects_provider_credentials(monkeypatch):
    for name in ("PRD2TC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PRD2TC_LLM_PROVIDER", "anthropic"); monkeypatch.setenv("ANTHROPIC_API_KEY", "ant")
    assert Config.from_env().api_key == "ant"
    monkeypatch.setenv("PRD2TC_LLM_PROVIDER", "ollama")
    cfg = Config.from_env(); assert cfg.api_base == "http://localhost:11434" and not cfg.requires_api_key()
