"""零第三方依赖的统一 LLM Provider 接口。"""
from __future__ import annotations
from dataclasses import dataclass
import json, time, urllib.error, urllib.request
from typing import Protocol
from prd2tc.config import Config

SYSTEM_PROMPT = "你是一位资深的测试架构师与测试设计专家。请严格按用户提供的指令与输出格式作答，不要输出多余的解释。"

class LLMError(RuntimeError): pass

@dataclass
class LLMResult:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    gate_retries: int = 0
    finish_reason: str = ""  # 截断诊断：openai=length / anthropic=max_tokens / ollama=length
    cache_read_input_tokens: int = 0  # 命中缓存的输入 token（usage.cache_read_input_tokens / cached_tokens）
    cache_creation_input_tokens: int = 0  # 写入缓存的输入 token（usage.cache_creation_input_tokens）

    @property
    def total_tokens(self) -> int: return self.prompt_tokens + self.completion_tokens

    @property
    def truncated(self) -> bool:
        """输出是否因长度限制被截断。"""
        return self.finish_reason in {"length", "max_tokens"}

class LLMClient(Protocol):
    def chat(self, user_text: str, *, system: str = SYSTEM_PROMPT,
             temperature: float | None = None) -> LLMResult: ...

class _HTTPClient:
    retry_codes = {429, 500, 502, 503, 504}
    def __init__(self, config: Config): self.config = config
    def _post(self, url: str, body: dict, headers: dict) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8"); last_error = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                request = urllib.request.Request(url, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                message = exc.read().decode("utf-8", errors="replace")[:300]
                if exc.code not in self.retry_codes or attempt >= self.config.max_retries:
                    raise LLMError(f"HTTP {exc.code}: {message}") from exc
                last_error = exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt >= self.config.max_retries: break
            time.sleep(2 ** attempt)
        raise LLMError(f"LLM 调用失败（已尝试 {self.config.max_retries} 次）: {last_error}")

def _endpoint(base: str, suffix: str) -> str:
    base = base.rstrip("/")
    if base.endswith(suffix): return base
    if suffix.startswith("/v1/") and base.endswith("/v1"):
        return base + suffix[len("/v1"):]
    return base + suffix

class OpenAIClient(_HTTPClient):
    """OpenAI Chat Completions；也兼容 DeepSeek 等同协议服务。"""
    def chat(self, user_text: str, *, system: str = SYSTEM_PROMPT, temperature: float | None = None) -> LLMResult:
        url = _endpoint(self.config.api_base, "/chat/completions")
        body = {"model": self.config.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_text}], "temperature": self.config.temperature if temperature is None else temperature, "max_tokens": self.config.max_tokens, "stream": False}
        if getattr(self.config, "seed", None) is not None:
            body["seed"] = self.config.seed  # OpenAI 兼容端点采样种子（Anthropic/MiniMax 端点无此参数）
        payload = self._post(url, body, {"Content-Type": "application/json", "Authorization": f"Bearer {self.config.api_key}"})
        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
            finish_reason = str(choice.get("finish_reason") or "")
        except (KeyError, IndexError, TypeError) as exc: raise LLMError("OpenAI 响应缺少 choices[0].message.content") from exc
        usage = payload.get("usage") or {}
        details = (usage.get("prompt_tokens_details") or {}) if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        return LLMResult(str(content), payload.get("model", self.config.model), int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)), finish_reason=finish_reason, cache_read_input_tokens=int(details.get("cached_tokens", 0) or 0))

class AnthropicClient(_HTTPClient):
    """Anthropic Messages API；兼容端点（如 MiniMax /anthropic）。

    prompt_cache=True 时把 system 转为带 cache_control 的块：对支持显式断点的端点开启缓存；
    对仅被动缓存的端点（如 MiniMax M3 兼容端点）不会报错，但是否命中须看 usage 回显
    cache_read_input_tokens / cache_creation_input_tokens，不能假定生效。
    """
    def chat(self, user_text: str, *, system: str = SYSTEM_PROMPT, temperature: float | None = None) -> LLMResult:
        url = _endpoint(self.config.api_base, "/v1/messages")
        system_payload = system
        if self.config.prompt_cache:
            system_payload = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        payload = self._post(url, {"model": self.config.model, "system": system_payload, "messages": [{"role": "user", "content": user_text}], "temperature": self.config.temperature if temperature is None else temperature, "max_tokens": self.config.max_tokens}, {"Content-Type": "application/json", "x-api-key": self.config.api_key, "anthropic-version": "2023-06-01"})
        blocks = payload.get("content") or []
        content = "".join(str(block.get("text", "")) for block in blocks if isinstance(block, dict) and block.get("type") == "text")
        if not content: raise LLMError("Anthropic 响应缺少文本 content block")
        usage = payload.get("usage") or {}
        return LLMResult(content, payload.get("model", self.config.model),
                         int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)),
                         finish_reason=str(payload.get("stop_reason") or ""),
                         cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
                         cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0))

class OllamaClient(_HTTPClient):
    def chat(self, user_text: str, *, system: str = SYSTEM_PROMPT, temperature: float | None = None) -> LLMResult:
        url = _endpoint(self.config.api_base, "/api/chat")
        payload = self._post(url, {"model":self.config.model,"messages":[{"role":"system","content":system},{"role":"user","content":user_text}],"options":{"temperature":self.config.temperature if temperature is None else temperature},"stream":False}, {"Content-Type":"application/json"})
        content = (payload.get("message") or {}).get("content")
        if not isinstance(content, str): raise LLMError("Ollama 响应缺少 message.content")
        return LLMResult(content, payload.get("model",self.config.model), int(payload.get("prompt_eval_count",0)), int(payload.get("eval_count",0)), finish_reason=str(payload.get("done_reason") or ""))

def create_client(config: Config) -> LLMClient:
    clients = {"openai": OpenAIClient, "anthropic": AnthropicClient, "ollama": OllamaClient}
    try: return clients[config.provider](config)
    except KeyError as exc: raise ValueError(f"不支持的 LLM Provider: {config.provider}") from exc

def chat_completion(cfg: Config, user_text: str, *, system: str = SYSTEM_PROMPT,
                    temperature: float | None = None) -> LLMResult:
    """向后兼容函数入口，内部按 Config.provider 分派。"""
    return create_client(cfg).chat(user_text, system=system, temperature=temperature)
