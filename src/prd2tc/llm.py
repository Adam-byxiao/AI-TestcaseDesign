"""OpenAI 兼容 Chat Completions 客户端（标准库 urllib，含重试与用量统计）。

支持的端点默认 DeepSeek（https://api.deepseek.com/chat/completions），
任何 OpenAI 兼容服务均可通过 PRD2TC_API_BASE 接入。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from prd2tc.config import Config

SYSTEM_PROMPT = "你是一位资深的测试架构师与测试设计专家。请严格按用户提供的指令与输出格式作答，不要输出多余的解释。"


class LLMError(RuntimeError):
    """LLM 调用失败（含多次重试后仍失败）"""


@dataclass
class LLMResult:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def chat_completion(
    cfg: Config,
    user_text: str,
    *,
    system: str = SYSTEM_PROMPT,
    temperature: float | None = None,
) -> LLMResult:
    """调用 chat/completions，返回首个 choice 的文本内容与用量统计。"""
    url = f"{cfg.api_base}/chat/completions"
    body = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
        "temperature": temperature if temperature is not None else cfg.temperature,
        "stream": False,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.api_key}",
    }
    data = json.dumps(body).encode("utf-8")

    last_error: Exception | None = None
    for attempt in range(1, cfg.max_retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
            usage = payload.get("usage", {})
            return LLMResult(
                content=content,
                model=payload.get("model", cfg.model),
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
            )
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:300]
            if e.code in (429, 500, 502, 503, 504) and attempt < cfg.max_retries:
                delay = 2 ** attempt  # 指数退避
                print(f"[llm] HTTP {e.code}，{delay}s 后重试（{attempt}/{cfg.max_retries}）: {err_body}")
                time.sleep(delay)
                last_error = e
                continue
            raise LLMError(f"HTTP {e.code}: {err_body}") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as e:
            last_error = e
            if attempt < cfg.max_retries:
                delay = 2 ** attempt
                print(f"[llm] {type(e).__name__}，{delay}s 后重试（{attempt}/{cfg.max_retries}）")
                time.sleep(delay)
                continue
            break
    raise LLMError(f"LLM 调用失败（已重试 {cfg.max_retries} 次）: {last_error}")
