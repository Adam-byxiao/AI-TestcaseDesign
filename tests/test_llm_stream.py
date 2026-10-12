"""流式解析测试：三家 Provider 的 SSE / NDJSON 协议，全部离线（录制片段作 fixture）。

重点覆盖真实踩坑点：
- usage 缺失时回退估算并标记，不谎报精确值；
- 首 token 之前可重试，产出内容后绝不重试（避免重复计费/拼接）；
- 取消立即生效，不再继续消费流。
"""
import json
from dataclasses import replace

import pytest

from prd2tc.config import Config
from prd2tc.llm import (LLMCancelled, LLMError, AnthropicClient, OllamaClient,
                        OpenAIClient, effective_stream)


def _cfg(**kw) -> Config:
    base = Config(api_key="sk-test", api_base="https://api.example.com/v1", model="m",
                  temperature=0.0, timeout=10, max_retries=2, stream=True)
    return replace(base, **kw)


class _FakeStream:
    """模拟 urllib 的流式响应：readline 逐行吐出预录片段。"""

    def __init__(self, lines: list[str], *, status: int = 200):
        self._lines = [((line + "\n").encode("utf-8")) for line in lines]
        self.status = status

    def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    def read(self) -> bytes:
        return b"".join(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _openai_sse(chunks: list[str], *, usage: dict | None = None, finish: str = "stop") -> list[str]:
    lines = []
    for piece in chunks:
        lines.append("data: " + json.dumps(
            {"model": "m-1", "choices": [{"delta": {"content": piece}, "finish_reason": None}]},
            ensure_ascii=False))
        lines.append("")
    if usage is not None:
        lines.append("data: " + json.dumps(
            {"model": "m-1", "choices": [], "usage": usage}, ensure_ascii=False))
        lines.append("")
    lines.append("data: " + json.dumps(
        {"model": "m-1", "choices": [{"delta": {}, "finish_reason": finish}]}, ensure_ascii=False))
    lines.append("")
    lines.append("data: [DONE]")
    return lines


def test_openai_stream_basic(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: _FakeStream(_openai_sse(
                            ["你好", "，世界"], usage={"prompt_tokens": 11, "completion_tokens": 7})))
    result = OpenAIClient(_cfg()).chat("hi", on_delta=seen.append)
    assert result.content == "你好，世界"
    assert result.streamed is True
    assert seen == ["你好", "，世界"], "on_delta 必须逐块回调"
    assert (result.prompt_tokens, result.completion_tokens) == (11, 7)
    assert result.usage_estimated is False
    assert result.first_token_ms >= 0
    assert result.finish_reason == "stop"


def test_openai_stream_without_usage_falls_back_to_estimate(monkeypatch):
    """兼容端点忽略 stream_options 时不得谎报精确 token。"""
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: _FakeStream(_openai_sse(["中文内容" * 5])))
    result = OpenAIClient(_cfg()).chat("hi")
    assert result.content == "中文内容" * 5
    assert result.usage_estimated is True
    assert result.completion_tokens > 0
    assert result.prompt_tokens == 0


def test_openai_stream_marks_truncation(monkeypatch):
    """流式下截断诊断必须保留（门禁反馈依赖它）。"""
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: _FakeStream(_openai_sse(["部分"], finish="length")))
    result = OpenAIClient(_cfg()).chat("hi")
    assert result.finish_reason == "length"
    assert result.truncated is True


def test_anthropic_stream_protocol(monkeypatch):
    """usage 分散在 message_start / message_delta；缓存命中应尽早可见。"""
    lines = [
        "event: message_start",
        "data: " + json.dumps({"type": "message_start", "message": {
            "model": "claude-x", "usage": {"input_tokens": 30, "cache_read_input_tokens": 20,
                                           "cache_creation_input_tokens": 5}}}),
        "",
        "event: content_block_delta",
        "data: " + json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "甲"}}),
        "",
        "event: content_block_delta",
        "data: " + json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "乙"}}),
        "",
        "event: message_delta",
        "data: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                               "usage": {"output_tokens": 9}}),
        "",
        "event: message_stop",
        "data: " + json.dumps({"type": "message_stop"}),
    ]
    seen: list[str] = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeStream(lines))
    result = AnthropicClient(_cfg(api_base="https://api.anthropic.com")).chat("hi", on_delta=seen.append)
    assert result.content == "甲乙" and seen == ["甲", "乙"]
    assert (result.prompt_tokens, result.completion_tokens) == (30, 9)
    assert result.cache_read_input_tokens == 20
    assert result.cache_creation_input_tokens == 5
    assert result.finish_reason == "end_turn" and result.usage_estimated is False


def test_anthropic_stream_empty_content_raises(monkeypatch):
    lines = ["data: " + json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 1}}})]
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeStream(lines))
    with pytest.raises(LLMError, match="缺少文本内容"):
        AnthropicClient(_cfg(api_base="https://api.anthropic.com")).chat("hi")


def test_ollama_ndjson_stream(monkeypatch):
    lines = [
        json.dumps({"model": "qwen", "message": {"content": "答"}, "done": False}),
        json.dumps({"model": "qwen", "message": {"content": "案"}, "done": False}),
        json.dumps({"model": "qwen", "message": {"content": ""}, "done": True,
                    "done_reason": "stop", "prompt_eval_count": 12, "eval_count": 3}),
    ]
    seen: list[str] = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeStream(lines))
    result = OllamaClient(_cfg(provider="ollama", api_base="http://localhost:11434")).chat("hi", on_delta=seen.append)
    assert result.content == "答案" and seen == ["答", "案"]
    assert (result.prompt_tokens, result.completion_tokens) == (12, 3)
    assert result.usage_estimated is False


def test_cancel_stops_consuming_stream(monkeypatch):
    """取消必须立即生效 —— 这是流式最大的省钱点。

    should_cancel 每行检查一次；预录 6 块内容，在第 2 次检查后返回 True，
    因此最多只应消费 2 块，剩余内容不得再被读取。
    """
    seen: list[str] = []
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: _FakeStream(_openai_sse(["a", "b", "c", "d", "e", "f"])))
    calls = {"n": 0}

    def should_cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    with pytest.raises(LLMCancelled):
        OpenAIClient(_cfg()).chat("hi", on_delta=seen.append, should_cancel=should_cancel)
    assert len(seen) <= 2, f"取消后仍消费了 {len(seen)} 块：{seen}"


def test_retry_before_first_token_is_allowed(monkeypatch):
    """首 token 之前遇到 5xx 允许重试。"""
    import urllib.error
    import io
    attempts = {"n": 0}

    def fake(*a, **k):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise urllib.error.HTTPError("u", 503, "boom", {"Content-Type": "application/json"},
                                         io.BytesIO(b'{"error":"overloaded"}'))
        return _FakeStream(_openai_sse(["ok"], usage={"prompt_tokens": 1, "completion_tokens": 1}))

    monkeypatch.setattr("urllib.request.urlopen", fake)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    result = OpenAIClient(_cfg()).chat("hi")
    assert result.content == "ok" and attempts["n"] == 2


def test_no_retry_after_content_produced(monkeypatch):
    """已产出内容后失败绝不重试，否则会重复拼接与重复计费。"""
    import urllib.error
    import io

    class _Boom:
        def __init__(self):
            self._lines = [b'data: {"choices":[{"delta":{"content":"half"}}]}\n']
            self.status = 200

        def readline(self):
            if self._lines:
                return self._lines.pop(0)
            raise urllib.error.URLError("connection reset")

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

    attempts = {"n": 0}

    def fake(*a, **k):
        attempts["n"] += 1
        return _Boom()

    monkeypatch.setattr("urllib.request.urlopen", fake)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    with pytest.raises(LLMError, match="不重试"):
        OpenAIClient(_cfg()).chat("hi")
    assert attempts["n"] == 1, "不得重试已产出内容的流"


def test_effective_stream_respects_repro_and_flag():
    assert effective_stream(_cfg()) is True
    assert effective_stream(_cfg(stream=False)) is False
    assert effective_stream(_cfg(repro=True)) is False, "--repro 必须强制非流式"
    assert effective_stream(_cfg(repro=True, stream=True)) is False


def test_non_stream_path_unchanged(monkeypatch):
    """stream=False 时仍走原非流式路径，并保持 streamed=False。"""
    class _Resp:
        def read(self):
            return json.dumps({"model": "m", "choices": [
                {"message": {"content": "普通响应"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 6}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    result = OpenAIClient(_cfg(stream=False)).chat("hi")
    assert result.content == "普通响应" and result.streamed is False
    assert (result.prompt_tokens, result.completion_tokens) == (5, 6)
    assert result.usage_estimated is False


def test_openai_reasoning_content_stays_out_of_content(monkeypatch):
    """DeepSeek/M3 兼容形态：推理走独立字段，绝不能混入 content。

    混入会导致 extract_cases 从思考里抓 JSON、以及指标把思考当产出。
    """
    lines = []
    for piece in ("先分析需求…", "再检查字段…"):
        lines.append("data: " + json.dumps(
            {"model": "m", "choices": [{"delta": {"reasoning_content": piece}, "finish_reason": None}]},
            ensure_ascii=False))
        lines.append("")
    for piece in ('[{"id":"TC-A-001"}]',):
        lines.append("data: " + json.dumps(
            {"model": "m", "choices": [{"delta": {"content": piece}, "finish_reason": None}]},
            ensure_ascii=False))
        lines.append("")
    lines.append("data: " + json.dumps(
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 9, "completion_tokens": 20}}))
    lines.append("")
    lines.append("data: [DONE]")

    content_seen: list[str] = []
    reasoning_seen: list[str] = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeStream(lines))
    result = OpenAIClient(_cfg()).chat("hi", on_delta=content_seen.append,
                                       on_reasoning_delta=reasoning_seen.append)
    assert result.content == '[{"id":"TC-A-001"}]'
    assert result.reasoning == "先分析需求…再检查字段…"
    assert result.reasoning_chars == len(result.reasoning)
    assert content_seen == ['[{"id":"TC-A-001"}]'], content_seen
    assert len(reasoning_seen) == 2, reasoning_seen
    assert "先分析" not in result.content


def test_anthropic_thinking_delta_is_separated(monkeypatch):
    lines = [
        "data: " + json.dumps({"type": "message_start", "message": {
            "model": "claude", "usage": {"input_tokens": 5}}}),
        "",
        "data: " + json.dumps({"type": "content_block_delta",
                               "delta": {"type": "thinking_delta", "thinking": "推理中"}}),
        "",
        "data: " + json.dumps({"type": "content_block_delta",
                               "delta": {"type": "text_delta", "text": "答案"}}),
        "",
        "data: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                               "usage": {"output_tokens": 4}}),
    ]
    content_seen: list[str] = []
    reasoning_seen: list[str] = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeStream(lines))
    result = AnthropicClient(_cfg(api_base="https://api.anthropic.com")).chat(
        "hi", on_delta=content_seen.append, on_reasoning_delta=reasoning_seen.append)
    assert result.content == "答案"
    assert result.reasoning == "推理中"
    assert content_seen == ["答案"] and reasoning_seen == ["推理中"]


def test_ollama_thinking_field_is_separated(monkeypatch):
    lines = [
        json.dumps({"model": "qwen", "message": {"content": "", "thinking": "思考中"}, "done": False}),
        json.dumps({"model": "qwen", "message": {"content": "结果"}, "done": False}),
        json.dumps({"model": "qwen", "message": {"content": ""}, "done": True,
                    "done_reason": "stop", "prompt_eval_count": 3, "eval_count": 2}),
    ]
    content_seen: list[str] = []
    reasoning_seen: list[str] = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeStream(lines))
    result = OllamaClient(_cfg(provider="ollama", api_base="http://localhost:11434")).chat(
        "hi", on_delta=content_seen.append, on_reasoning_delta=reasoning_seen.append)
    assert result.content == "结果"
    assert result.reasoning == "思考中"
    assert content_seen == ["结果"] and reasoning_seen == ["思考中"]


def test_reasoning_only_truncated_response_is_not_content(monkeypatch):
    """真实故障形态：未闭合 <think> 被截断，content 里没有任何可用 JSON。"""
    lines = _openai_sse(["<think>The JSON parsing failed at line 11. Let me check testData"],
                        finish="length")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeStream(lines))
    result = OpenAIClient(_cfg()).chat("hi")
    assert result.truncated is True
    from prd2tc.reasoning import looks_like_reasoning_exhausted
    assert looks_like_reasoning_exhausted(result.content, finish_reason=result.finish_reason) is True
