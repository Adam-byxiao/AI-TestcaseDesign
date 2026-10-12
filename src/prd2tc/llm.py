"""零第三方依赖的统一 LLM Provider 接口。"""
from __future__ import annotations
from dataclasses import dataclass
import base64, json, re, time, urllib.error, urllib.request
from typing import Protocol
from prd2tc.config import Config

SYSTEM_PROMPT = "你是一位资深的测试架构师与测试设计专家。请严格按用户提供的指令与输出格式作答，不要输出多余的解释。"

class LLMError(RuntimeError): pass


class LLMCancelled(RuntimeError):
    """用户取消导致的中断，与 LLMError 区分，便于上层落 cancelled 而非 failed。"""


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
    streamed: bool = False  # 是否走流式
    first_token_ms: int = 0  # 首 token 延迟（诊断「卡在连接」还是「卡在生成」）
    usage_estimated: bool = False  # completion_tokens 是否为字符估算值
    reasoning: str = ""  # 独立通道的推理内容（reasoning_content / thinking_delta）
    reasoning_chars: int = 0  # 推理内容字符数（含 content 内 <think> 的部分，由 metrics 统计）

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
        """非流式 POST（--repro 与配置探测走这条路径）。"""
        data = json.dumps(body, ensure_ascii=False).encode("utf-8"); last_error = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                request = urllib.request.Request(url, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                message = _error_detail(exc.code, exc.read().decode("utf-8", errors="replace"),
                                        (exc.headers or {}).get("Content-Type", ""))
                if exc.code not in self.retry_codes or attempt >= self.config.max_retries:
                    raise LLMError(message) from exc
                last_error = exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt >= self.config.max_retries: break
            time.sleep(2 ** attempt)
        raise LLMError(f"LLM 调用失败（已尝试 {self.config.max_retries} 次）: {last_error}")

    def _stream(self, url: str, body: dict, headers: dict, handler, *,
                on_delta=None, should_cancel=None, on_reasoning_delta=None) -> _StreamState:
        """流式 POST：逐行读取并交给 handler 解析。

        handler 返回 (正文片段, 推理片段)。两者分流：推理绝不混入 content。

        重试规则（关键）：**只在产出任何内容之前**允许重试。一旦已经吐出 token，
        重试会导致内容重复拼接与重复计费，因此直接失败。
        取消检查放在每行：命中即关闭响应并抛 LLMCancelled，从而真正止损。
        """
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last_error: object = None
        for attempt in range(1, self.config.max_retries + 1):
            state = _StreamState()
            started = time.perf_counter()
            try:
                request = urllib.request.Request(url, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                    for line in self._iter_lines(response):
                        if should_cancel is not None and should_cancel():
                            raise LLMCancelled("任务已取消，已停止接收流式响应")
                        piece, think = handler(line, state)
                        state.feed(piece, on_delta, started)
                        state.feed_reasoning(think, on_reasoning_delta)
                return state
            except LLMCancelled:
                raise
            except urllib.error.HTTPError as exc:
                detail = _error_detail(exc.code, exc.read().decode("utf-8", errors="replace"),
                                       (exc.headers or {}).get("Content-Type", ""))
                if state.produced or exc.code not in self.retry_codes or attempt >= self.config.max_retries:
                    raise LLMError(detail) from exc
                last_error = detail
            except (urllib.error.URLError, TimeoutError) as exc:
                if state.produced or attempt >= self.config.max_retries:
                    raise LLMError(f"流式响应中断（已产出部分内容，不重试以免重复计费）: {exc}") from exc
                last_error = exc
            except json.JSONDecodeError as exc:
                raise LLMError(f"流式响应解析失败: {exc}") from exc
            time.sleep(2 ** attempt)
        raise LLMError(f"LLM 流式调用失败（已尝试 {self.config.max_retries} 次）: {last_error}")

    @staticmethod
    def _iter_lines(response):
        """逐行读取（readline 而非迭代，避免缓冲导致进度延迟）。"""
        while True:
            raw = response.readline()
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                yield line

    def _stream_result(self, state: _StreamState, *, fallback_model: str) -> LLMResult:
        """把流式累积状态收敛为 LLMResult；无 usage 时按字符估算并标记。"""
        content = state.content
        estimated = False
        completion = state.completion_tokens
        if not state.saw_usage or not completion:
            cjk, ascii_chars = _cjk_ascii_split(content)
            completion = int(cjk + ascii_chars / 4)
            estimated = True
        return LLMResult(content, state.model or fallback_model,
                         state.prompt_tokens, completion,
                         finish_reason=state.finish_reason,
                         cache_read_input_tokens=state.cache_read,
                         cache_creation_input_tokens=state.cache_creation,
                         streamed=True, first_token_ms=state.first_token_ms,
                         usage_estimated=estimated,
                         reasoning=state.reasoning,
                         reasoning_chars=len(state.reasoning))
        data = json.dumps(body, ensure_ascii=False).encode("utf-8"); last_error = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                request = urllib.request.Request(url, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                message = _error_detail(exc.code, exc.read().decode("utf-8", errors="replace"),
                                        (exc.headers or {}).get("Content-Type", ""))
                if exc.code not in self.retry_codes or attempt >= self.config.max_retries:
                    raise LLMError(message) from exc
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


def effective_stream(config: Config) -> bool:
    """是否使用流式：--repro（repro=True）强制关闭，保住结构复现基线。"""
    return bool(getattr(config, "stream", False)) and not getattr(config, "repro", False)


def _cjk_ascii_split(text: str) -> tuple[int, int]:
    """返回 (cjk 字符数, 其余字符数)，用于无 usage 时估算 completion token。"""
    cjk = sum(1 for ch in text if ch >= "\u2e80")
    return cjk, max(0, len(text) - cjk)


class _StreamState:
    """流式解析的累积状态：正文、推理内容、usage、结束原因、首 token 时刻。"""

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.reasoning_chunks: list[str] = []
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cache_read = 0
        self.cache_creation = 0
        self.finish_reason = ""
        self.model = ""
        self.saw_usage = False
        self.produced = False
        self.first_token_ms = 0

    def feed(self, piece: str, on_delta, started: float) -> None:
        if not piece:
            return
        if not self.produced:
            self.first_token_ms = int((time.perf_counter() - started) * 1000)
        self.produced = True
        self.chunks.append(piece)
        if on_delta is not None:
            on_delta(piece)

    def feed_reasoning(self, piece: str, on_reasoning_delta=None) -> None:
        """独立的推理通道（如 DeepSeek/M3 的 reasoning_content 字段）。

        与正文分开累积，绝不混入 content——否则提取与指标都会被污染。
        """
        if not piece:
            return
        self.reasoning_chunks.append(piece)
        if on_reasoning_delta is not None:
            on_reasoning_delta(piece)

    @property
    def content(self) -> str:
        return "".join(self.chunks)

    @property
    def reasoning(self) -> str:
        return "".join(self.reasoning_chunks)

class OpenAIClient(_HTTPClient):
    """OpenAI Chat Completions；也兼容 DeepSeek 等同协议服务。"""
    def chat(self, user_text: str, *, system: str = SYSTEM_PROMPT, temperature: float | None = None,
             on_delta=None, should_cancel=None, on_reasoning_delta=None) -> LLMResult:
        url = _endpoint(self.config.api_base, "/chat/completions")
        body = {"model": self.config.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_text}], "temperature": self.config.temperature if temperature is None else temperature, "max_tokens": self.config.max_tokens, "stream": False}
        if getattr(self.config, "seed", None) is not None:
            body["seed"] = self.config.seed  # OpenAI 兼容端点采样种子（Anthropic/MiniMax 端点无此参数）
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.config.api_key}"}
        if effective_stream(self.config):
            return self._chat_stream(url, body, headers, on_delta=on_delta,
                                     should_cancel=should_cancel,
                                     on_reasoning_delta=on_reasoning_delta)
        payload = self._post(url, body, headers)
        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
            finish_reason = str(choice.get("finish_reason") or "")
        except (KeyError, IndexError, TypeError) as exc: raise LLMError("OpenAI 响应缺少 choices[0].message.content") from exc
        usage = payload.get("usage") or {}
        details = (usage.get("prompt_tokens_details") or {}) if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        return LLMResult(str(content), payload.get("model", self.config.model), int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)), finish_reason=finish_reason, cache_read_input_tokens=int(details.get("cached_tokens", 0) or 0))

    def _chat_stream(self, url, body, headers, *, on_delta, should_cancel,
                     on_reasoning_delta=None) -> LLMResult:
        """SSE 流式：data: {...} 行，[DONE] 结束。

        stream_options.include_usage 让末块带上 usage；部分兼容端点会忽略该参数，
        此时回退为字符估算（usage_estimated=True），不谎报精确值。
        MiniMax-M3 等模型会把思考写在 content 里（`<think>…</think>`），
        故正文中仍可能含思考标签——由 reasoning 模块统一剥离。
        """
        body = {**body, "stream": True, "stream_options": {"include_usage": True}}

        def handler(line: str, state: _StreamState) -> tuple[str, str]:
            if not line.startswith("data:"):
                return "", ""
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                return "", ""
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                return "", ""
            if data.get("model"):
                state.model = str(data["model"])
            usage = data.get("usage")
            if isinstance(usage, dict) and usage:
                state.saw_usage = True
                state.prompt_tokens = int(usage.get("prompt_tokens", state.prompt_tokens) or 0)
                state.completion_tokens = int(usage.get("completion_tokens", state.completion_tokens) or 0)
                details = usage.get("prompt_tokens_details") or {}
                if isinstance(details, dict):
                    state.cache_read = int(details.get("cached_tokens", state.cache_read) or 0)
            choices = data.get("choices") or []
            if not choices:
                return "", ""
            choice = choices[0]
            if choice.get("finish_reason"):
                state.finish_reason = str(choice["finish_reason"])
            delta = choice.get("delta") or {}
            content = delta.get("content")
            # 部分推理型端点把思考放在独立字段（DeepSeek/M3 兼容形态）
            reasoning = delta.get("reasoning_content")
            return (content if isinstance(content, str) else "",
                    reasoning if isinstance(reasoning, str) else "")

        state = self._stream(url, body, headers, handler, on_delta=on_delta,
                             should_cancel=should_cancel, on_reasoning_delta=on_reasoning_delta)
        return self._stream_result(state, fallback_model=self.config.model)

class AnthropicClient(_HTTPClient):
    """Anthropic Messages API；兼容端点（如 MiniMax /anthropic）。

    prompt_cache=True 时把 system 转为带 cache_control 的块：对支持显式断点的端点开启缓存；
    对仅被动缓存的端点（如 MiniMax M3 兼容端点）不会报错，但是否命中须看 usage 回显
    cache_read_input_tokens / cache_creation_input_tokens，不能假定生效。
    """
    def chat(self, user_text: str, *, system: str = SYSTEM_PROMPT, temperature: float | None = None,
             on_delta=None, should_cancel=None, on_reasoning_delta=None) -> LLMResult:
        url = _endpoint(self.config.api_base, "/v1/messages")
        system_payload = system
        if self.config.prompt_cache:
            system_payload = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        body = {"model": self.config.model, "system": system_payload, "messages": [{"role": "user", "content": user_text}], "temperature": self.config.temperature if temperature is None else temperature, "max_tokens": self.config.max_tokens}
        headers = {"Content-Type": "application/json", "x-api-key": self.config.api_key, "anthropic-version": "2023-06-01"}
        if effective_stream(self.config):
            return self._chat_stream(url, body, headers, on_delta=on_delta,
                                     should_cancel=should_cancel,
                                     on_reasoning_delta=on_reasoning_delta)
        payload = self._post(url, body, headers)
        blocks = payload.get("content") or []
        content = "".join(str(block.get("text", "")) for block in blocks if isinstance(block, dict) and block.get("type") == "text")
        if not content: raise LLMError("Anthropic 响应缺少文本 content block")
        usage = payload.get("usage") or {}
        return LLMResult(content, payload.get("model", self.config.model),
                         int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)),
                         finish_reason=str(payload.get("stop_reason") or ""),
                         cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
                         cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0))

    def _chat_stream(self, url, body, headers, *, on_delta, should_cancel,
                     on_reasoning_delta=None) -> LLMResult:
        """Anthropic SSE 事件流：usage 分散在 message_start 与 message_delta。

        message_start 就带回 input_tokens / 缓存命中数，因此缓存命中能**尽早**显示，
        不必等生成结束 —— 这对判断「是不是提示词缓存没生效」很关键。
        Extended thinking 走 thinking_delta 事件，单独分流为推理内容。
        """
        body = {**body, "stream": True}

        def handler(line: str, state: _StreamState) -> tuple[str, str]:
            if not line.startswith("data:"):
                return "", ""  # event: 行与空行直接跳过
            payload = line[5:].strip()
            if not payload:
                return "", ""
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                return "", ""
            kind = str(data.get("type") or "")
            if kind == "message_start":
                message = data.get("message") or {}
                state.model = str(message.get("model") or state.model)
                usage = message.get("usage") or {}
                if usage:
                    state.saw_usage = True
                    state.prompt_tokens = int(usage.get("input_tokens", 0) or 0)
                    state.cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
                    state.cache_creation = int(usage.get("cache_creation_input_tokens", 0) or 0)
                return "", ""
            if kind == "content_block_delta":
                delta = data.get("delta") or {}
                if str(delta.get("type")) == "thinking_delta":
                    think = delta.get("thinking")
                    return "", (think if isinstance(think, str) else "")
                piece = delta.get("text")
                return (piece if isinstance(piece, str) else ""), ""
            if kind == "message_delta":
                delta = data.get("delta") or {}
                if delta.get("stop_reason"):
                    state.finish_reason = str(delta["stop_reason"])
                usage = data.get("usage") or {}
                if usage and usage.get("output_tokens") is not None:
                    state.saw_usage = True
                    state.completion_tokens = int(usage.get("output_tokens") or 0)
                return "", ""
            return "", ""

        state = self._stream(url, body, headers, handler, on_delta=on_delta,
                             should_cancel=should_cancel, on_reasoning_delta=on_reasoning_delta)
        content = state.content
        if not content.strip() and not state.reasoning.strip():
            raise LLMError("Anthropic 流式响应缺少文本内容")
        return self._stream_result(state, fallback_model=self.config.model)

class OllamaClient(_HTTPClient):
    def chat(self, user_text: str, *, system: str = SYSTEM_PROMPT, temperature: float | None = None,
             on_delta=None, should_cancel=None, on_reasoning_delta=None) -> LLMResult:
        url = _endpoint(self.config.api_base, "/api/chat")
        body = {"model":self.config.model,"messages":[{"role":"system","content":system},{"role":"user","content":user_text}],"options":{"temperature":self.config.temperature if temperature is None else temperature},"stream":False}
        headers = {"Content-Type":"application/json"}
        if effective_stream(self.config):
            return self._chat_stream(url, body, headers, on_delta=on_delta,
                                     should_cancel=should_cancel,
                                     on_reasoning_delta=on_reasoning_delta)
        payload = self._post(url, body, headers)
        content = (payload.get("message") or {}).get("content")
        if not isinstance(content, str): raise LLMError("Ollama 响应缺少 message.content")
        return LLMResult(content, payload.get("model",self.config.model), int(payload.get("prompt_eval_count",0)), int(payload.get("eval_count",0)), finish_reason=str(payload.get("done_reason") or ""))

    def _chat_stream(self, url, body, headers, *, on_delta, should_cancel,
                     on_reasoning_delta=None) -> LLMResult:
        """NDJSON 流式：每行一个 JSON 对象，末行 done=true 并带回精确计数。

        新版 Ollama 会把思考放在 message.thinking，单独分流为推理内容。
        """
        body = {**body, "stream": True}

        def handler(line: str, state: _StreamState) -> tuple[str, str]:
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                return "", ""
            if data.get("model"):
                state.model = str(data["model"])
            if data.get("done"):
                state.saw_usage = True
                state.prompt_tokens = int(data.get("prompt_eval_count", state.prompt_tokens) or 0)
                state.completion_tokens = int(data.get("eval_count", state.completion_tokens) or 0)
                if data.get("done_reason"):
                    state.finish_reason = str(data["done_reason"])
            message = data.get("message") or {}
            piece = message.get("content")
            think = message.get("thinking")
            return (piece if isinstance(piece, str) else "",
                    think if isinstance(think, str) else "")

        state = self._stream(url, body, headers, handler, on_delta=on_delta,
                             should_cancel=should_cancel, on_reasoning_delta=on_reasoning_delta)
        return self._stream_result(state, fallback_model=self.config.model)

def create_client(config: Config) -> LLMClient:
    clients = {"openai": OpenAIClient, "anthropic": AnthropicClient, "ollama": OllamaClient}
    try: return clients[config.provider](config)
    except KeyError as exc: raise ValueError(f"不支持的 LLM Provider: {config.provider}") from exc


# ---- 配置自检：保存前探测端点，把「运行 1 秒后一句无信息量的失败」变成「配置时明确的原因」 ----

# Provider → (请求路径, 最小请求体工厂, 认证头工厂)
PROBE_TIMEOUT_SECONDS = 25
_HTML_MARKERS = ("<!doctype", "<html", "<head", "<title")
_KINDS = ("ok", "config", "auth", "endpoint", "model", "rate_limit", "server", "network", "malformed")


@dataclass
class ProbeResult:
    """一次端点探测的结构化结论。kind 取值见 _KINDS。"""
    ok: bool
    kind: str
    message: str
    hint: str = ""
    endpoint: str = ""
    provider: str = ""
    model: str = ""
    http_status: int | None = None
    content_type: str = ""
    suggested_base: str = ""
    latency_ms: int = 0

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in
                ("ok", "kind", "message", "hint", "endpoint", "provider", "model",
                 "http_status", "content_type", "suggested_base", "latency_ms")}

    def summary(self) -> str:
        prefix = f"HTTP {self.http_status}" if self.http_status else ""
        if prefix and self.message.lstrip().startswith(prefix):
            prefix = ""  # 消息本身已带状态码，避免重复
        head = f"[{self.kind}] " + (f"{prefix} " if prefix else "") + self.message
        return head + (f"\n  → {self.hint}" if self.hint else "")


def _probe_request(provider: str, model: str, api_key: str, system: str) -> tuple[str, dict, dict]:
    """构造探测用的最小请求（1 token 输出，尽量避免计费）。"""
    if provider == "anthropic":
        body = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
        headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        if api_key:
            headers["x-api-key"] = api_key
        return "/v1/messages", body, headers
    if provider == "ollama":
        body = {"model": model, "messages": [{"role": "user", "content": "ping"}],
                "stream": False, "options": {"num_predict": 1}}
        return "/api/chat", body, {"Content-Type": "application/json"}
    body = {"model": model, "messages": [{"role": "system", "content": system},
                                         {"role": "user", "content": "ping"}],
            "max_tokens": 1, "stream": False}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return "/chat/completions", body, headers


def _excerpt(text: str, limit: int = 160) -> str:
    flat = " ".join(str(text).split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _html_title(body: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
    return _excerpt(match.group(1).strip(), 60) if match else ""


def _error_detail(code: int, raw: str, content_type: str) -> str:
    """把 HTTP 错误压缩成一句可读的诊断，避免把整页 HTML 灌进日志与 UI。

    地址填错时端点常返回 200/404 的网页，原文是几百字符的 HTML，
    既看不出原因也淹没其他错误；这里直接点明「地址返回的是网页」。
    """
    is_html = "html" in (content_type or "").lower() or any(m in raw[:2000].lower() for m in _HTML_MARKERS)
    if is_html:
        title = _html_title(raw)
        who = f"「{title}」" if title else ""
        return (f"HTTP {code}: 该地址返回的是网页{who}，不是 API 响应；"
                "apiBase 很可能填成了控制台/官网地址（API 地址通常以 /v1 结尾）")
    return f"HTTP {code}: {_excerpt(raw, 200)}"


def _suggest_base(api_base: str, provider: str) -> str:
    """OpenAI 兼容端点常见漏写 /v1；给出可直接替换的建议地址。"""
    base = (api_base or "").rstrip("/")
    if provider != "openai" or not base or base.endswith("/v1"):
        return ""
    return base + "/v1"


def probe_provider(config: Config, *, prompt_system: str = SYSTEM_PROMPT) -> ProbeResult:
    """探测 Provider 端点是否可用：单次请求、不重试，返回结构化诊断。

    专门区分「地址填错」与「密钥填错」——这两者在原始 HTTPError 里都只是
    一句 HTTP 4xx，导致配置错误要到运行阶段才以无信息量的形式暴露。
    """
    provider, model, api_base = config.provider, config.model, (config.api_base or "").rstrip("/")
    result = ProbeResult(ok=False, kind="config", message="", provider=provider,
                         model=model, suggested_base=_suggest_base(api_base, provider))

    if not api_base:
        result.message = "apiBase 为空"
        result.hint = "请填写 Provider 的 API 地址（例如 https://api.deepseek.com 或 https://api.minimax.chat/v1）"
        return result
    if not re.match(r"^https?://", api_base, re.IGNORECASE):
        result.message = f"apiBase 缺少 http(s):// 前缀：{api_base}"
        result.hint = "地址必须以 http:// 或 https:// 开头"
        return result
    if config.requires_api_key() and not config.api_key:
        result.message = f"{provider} 需要 API Key，但当前为空"
        result.hint = "填写 API Key；Ollama 等本地端点无需 Key"
        return result

    suffix, body, headers = _probe_request(provider, model, config.api_key, prompt_system)
    url = _endpoint(api_base, suffix)
    result.endpoint = url
    started = time.perf_counter()

    def finish(kind: str, message: str, hint: str = "", *, status: int | None = None,
               ctype: str = "") -> ProbeResult:
        result.kind, result.message, result.hint = kind, message, hint
        result.http_status, result.content_type = status, ctype
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    try:
        request = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                         headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=min(config.timeout or PROBE_TIMEOUT_SECONDS,
                                                         PROBE_TIMEOUT_SECONDS)) as response:
            raw = response.read().decode("utf-8", errors="replace")
            ctype = response.headers.get("Content-Type", "")
        if any(marker in raw[:2000].lower() for marker in _HTML_MARKERS):
            title = _html_title(raw)
            return finish("endpoint", f"地址返回的是网页而非 API 响应（{title or 'HTML 页面'}）",
                          "该地址很可能是控制台/官网首页，请填写 API 接口地址")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return finish("endpoint", f"响应不是合法 JSON：{_excerpt(raw)}",
                          "请确认 apiBase 指向 OpenAI 兼容的接口地址", ctype=ctype)
        if response.status >= 400:
            return finish("server", f"HTTP {response.status}：{_excerpt(raw)}",
                          "端点返回错误状态", status=response.status, ctype=ctype)
        result.ok, result.kind, result.message = True, "ok", "连接成功，端点可用"
        result.model = str(payload.get("model") or model)
        result.http_status, result.content_type = response.status, ctype
        result.hint = _excerpt(raw, 120)
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        ctype = exc.headers.get("Content-Type", "") if exc.headers else ""
        is_html = "html" in ctype.lower() or any(m in raw[:2000].lower() for m in _HTML_MARKERS)
        if is_html:
            title = _html_title(raw)
            return finish("endpoint",
                          f"HTTP {exc.code} 且返回网页而非 API 响应（{title or 'HTML 页面'}）",
                          "apiBase 很可能填成了控制台/官网地址，而不是 API 接口地址；"
                          "请到 Provider 控制台复制「OpenAI 兼容」的接口地址",
                          status=exc.code, ctype=ctype)
        if exc.code in (401, 403):
            return finish("auth", f"HTTP {exc.code} 鉴权失败：{_excerpt(raw)}",
                          "API Key 不正确、已过期或不属于该地址对应的平台",
                          status=exc.code, ctype=ctype)
        if exc.code == 404:
            return finish("endpoint", f"HTTP 404 接口不存在：{_excerpt(raw)}",
                          (f"接口路径不正确；OpenAI 兼容端点通常在地址末尾需要 /v1"
                           + (f"，可尝试 {result.suggested_base}" if result.suggested_base else ""))
                          if result.suggested_base else "接口路径不正确，请核对 apiBase",
                          status=exc.code, ctype=ctype)
        if exc.code == 429:
            return finish("rate_limit", f"HTTP 429 触发限流：{_excerpt(raw)}",
                          "端点可达、鉴权通过，但当前被限流；稍后重试", status=exc.code, ctype=ctype)
        if exc.code == 400 and "model" in raw.lower():
            return finish("model", f"HTTP 400 模型不可用：{_excerpt(raw)}",
                          f"模型名「{model}」可能不被该端点支持，请核对模型名", status=exc.code, ctype=ctype)
        hint = (f"该地址未返回正常 API 响应；OpenAI 兼容端点通常需要 /v1，可尝试 {result.suggested_base}"
                if exc.code >= 500 and result.suggested_base else
                ("可尝试在地址末尾补齐 /v1" if result.suggested_base else "请核对 apiBase 与模型名"))
        return finish("server", f"HTTP {exc.code}：{_excerpt(raw)}", hint,
                      status=exc.code, ctype=ctype)
    except urllib.error.URLError as exc:
        return finish("network", f"无法连接 {url}：{exc.reason}",
                      "检查网络 / DNS / 代理，确认该主机可访问")
    except TimeoutError:
        return finish("network", f"连接 {url} 超时（>{PROBE_TIMEOUT_SECONDS}s）",
                      "端点无响应或被网络阻断")
    except Exception as exc:  # 兜底：探测本身绝不抛出，避免阻断保存流程
        return finish("malformed", f"探测失败：{type(exc).__name__}: {exc}", "请核对 apiBase 配置")

def vision_completion(cfg: Config, prompt: str, images, *, system: str = "",
                      temperature: float | None = None, client=None) -> LLMResult:
    """视觉补全：把图片与提示词一起发给支持多模态的模型。

    `images` 为 `[(bytes, mime), ...]`。图片以 data URI 内联。
    **不走流式**：图片分析是结构化短输出，流式无收益且会复杂化。
    Provider 不支持图片输入时会抛 LLMError，由调用方降级处理。
    `client` 可注入（测试用），默认按 cfg 构造。

    多模态消息格式按 Provider 区分：
      - OpenAI 兼容：`content` 为数组，图片项 `{"type":"image_url",...}`
      - Anthropic ：`content` 为数组，图片项 `{"type":"image","source":{...}}`
    """
    if not images:
        raise LLMError("vision_completion 需要至少一张图片")
    client = client or create_client(cfg)
    temp = cfg.temperature if temperature is None else temperature
    if isinstance(client, AnthropicClient):
        blocks: list[dict] = [{"type": "text", "text": prompt}]
        for data, mime in images:
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": mime,
                "data": base64.b64encode(data).decode("ascii")}})
        url = _endpoint(cfg.api_base, "/v1/messages")
        body = {"model": cfg.model, "messages": [{"role": "user", "content": blocks}],
                "temperature": temp, "max_tokens": cfg.max_tokens}
        if system:
            body["system"] = system
        headers = {"Content-Type": "application/json", "x-api-key": cfg.api_key,
                   "anthropic-version": "2023-06-01"}
        payload = client._post(url, body, headers)
        parts = payload.get("content") or []
        text = "".join(str(b.get("text", "")) for b in parts
                       if isinstance(b, dict) and b.get("type") == "text")
        if not text:
            raise LLMError("Anthropic 视觉响应缺少文本 content block")
        usage = payload.get("usage") or {}
        return LLMResult(text, payload.get("model", cfg.model),
                         int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)),
                         finish_reason=str(payload.get("stop_reason") or ""))

    # OpenAI 兼容（含 MiniMax / DeepSeek 等多模态端点）
    content: list[dict] = [{"type": "text", "text": prompt}]
    for data, mime in images:
        uri = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        content.append({"type": "image_url", "image_url": {"url": uri}})
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": content})
    url = _endpoint(cfg.api_base, "/chat/completions")
    body = {"model": cfg.model, "messages": messages, "temperature": temp,
            "max_tokens": cfg.max_tokens, "stream": False}
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {cfg.api_key}"}
    payload = client._post(url, body, headers)
    try:
        choice = payload["choices"][0]
        text = choice["message"]["content"]
        finish = str(choice.get("finish_reason") or "")
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("视觉响应缺少 choices[0].message.content"
                       "（该模型可能不支持图片输入）") from exc
    usage = payload.get("usage") or {}
    return LLMResult(str(text), payload.get("model", cfg.model),
                     int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)),
                     finish_reason=finish)


def chat_completion(cfg: Config, user_text: str, *, system: str = SYSTEM_PROMPT,
                    temperature: float | None = None, on_delta=None,
                    should_cancel=None, on_reasoning_delta=None) -> LLMResult:
    """向后兼容函数入口，内部按 Config.provider 分派。

    on_delta/on_reasoning_delta/should_cancel 为可选流式钩子；不支持它们的自定义
    llm 实现（如测试里的假实现）仍可只接受前三个参数——见 stages._llm_hook_params。
    """
    return create_client(cfg).chat(user_text, system=system, temperature=temperature,
                                   on_delta=on_delta, should_cancel=should_cancel,
                                   on_reasoning_delta=on_reasoning_delta)
