"""配置：OpenAI、Anthropic、Ollama 统一 Provider 参数。

环境变量：
  PRD2TC_LLM_PROVIDER                  openai / anthropic / ollama
  PRD2TC_API_KEY                       Provider API Key（Ollama 不需要）
  PRD2TC_API_BASE                      Provider API 地址
  PRD2TC_MODEL                         模型名
  PRD2TC_TEMPERATURE                   温度，默认 0.4
  PRD2TC_TIMEOUT                       请求超时秒，默认 120
  PRD2TC_MAX_TOKENS                    单次输出上限，默认 16384
                                       （实测用例输出约 1700 tok/需求，个别模块 3 条需求即超过 8192；
                                         该值只是上限、按实际输出计费，调高不增加固定成本。
                                         注意：不得超过 Provider 的模型输出上限，否则会被拒（Anthropic 尤甚））
  PRD2TC_TOKENS_PER_REQ                批次容量估算：每条需求预期输出 token（默认 1800，实测校准）
  PRD2TC_CASE_BATCH_CAP                单批需求数上限（默认 8）
  PRD2TC_CASE_OUTPUT_BUDGET            单批允许占用 max_tokens 的比例（默认 0.60，留 40% 余量防截断）
  PRD2TC_PROMPT_CACHE                  1=开启（Anthropic 请求带 cache_control，兼容端点是否生效以 usage 回显为准）
  PRD2TC_REPRO                         1=可复现模式（三阶段温度归零，回归验收用）
  PRD2TC_SEED                          Provider 采样种子（仅 OpenAI 兼容端点生效，Anthropic/MiniMax 忽略）
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    api_key: str
    api_base: str
    model: str
    temperature: float
    timeout: int
    max_retries: int
    chunk_size: int = 6000
    chunk_overlap: int = 1200
    provider: str = "openai"
    max_tokens: int = 16384
    prompt_cache: bool = False
    repro: bool = False  # 可复现模式：三阶段温度归零，作为回归验收基线
    seed: int | None = None  # Provider 采样种子（OpenAI 兼容端点支持；Anthropic/MiniMax 忽略）
    stream: bool = True  # 流式输出：默认开启，用于实时进度/token；repro 模式下强制关闭
    stream_push_ms: int = 500  # 实时指标推送间隔（毫秒），限流用
    jobs: int = 1  # 默认并发批次数；CLI/服务未显式指定时生效（PRD2TC_JOBS）
    # Markdown 中转：让模型输出 Markdown 表格再由程序转 JSON，彻底规避嵌套转义崩坏
    markdown_transit: bool = False
    stage_temperatures: tuple[float, float, float] | None = None

    def requires_api_key(self) -> bool:
        return self.provider in {"openai", "anthropic"}

    @classmethod
    def from_env(cls, provider_override: str | None = None) -> "Config":
        provider = (provider_override or os.environ.get("PRD2TC_LLM_PROVIDER", "openai")).strip().lower()
        if provider not in {"openai", "anthropic", "ollama"}:
            raise ValueError("PRD2TC_LLM_PROVIDER 必须是 openai、anthropic 或 ollama")
        if provider == "anthropic":
            default_base, default_model = "https://api.anthropic.com", "claude-sonnet-4-5"
            api_key = os.environ.get("PRD2TC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or ""
        elif provider == "ollama":
            default_base, default_model, api_key = "http://localhost:11434", "llama3.2", ""
        else:
            deepseek = os.environ.get("DEEPSEEK_API_KEY")
            default_base = "https://api.deepseek.com" if deepseek else "https://api.openai.com/v1"
            default_model = "deepseek-chat" if deepseek else "gpt-4.1-mini"
            api_key = os.environ.get("PRD2TC_API_KEY") or os.environ.get("OPENAI_API_KEY") or deepseek or ""
        configured_base = os.environ.get("PRD2TC_API_BASE")
        if provider == "openai":
            configured_base = configured_base or os.environ.get("OPENAI_BASE_URL")
        scalar = float(os.environ.get("PRD2TC_TEMPERATURE", "0.4"))
        stage_values = tuple(float(os.environ.get(f"PRD2TC_TEMP_S{i}", str(
            scalar if "PRD2TC_TEMPERATURE" in os.environ else (0.2, 0.3, 0.3)[i - 1])))
            for i in (1, 2, 3))
        return cls(
            api_key=api_key,
            api_base=(configured_base or default_base).rstrip("/"),
            model=os.environ.get("PRD2TC_MODEL", default_model),
            temperature=scalar,
            timeout=int(os.environ.get("PRD2TC_TIMEOUT", "120")),
            max_retries=int(os.environ.get("PRD2TC_MAX_RETRIES", "3")),
            chunk_size=int(os.environ.get("PRD2TC_CHUNK_SIZE", "6000")),
            chunk_overlap=int(os.environ.get("PRD2TC_CHUNK_OVERLAP", "1200")),
            provider=provider,
            max_tokens=int(os.environ.get("PRD2TC_MAX_TOKENS", "16384")),
            prompt_cache=os.environ.get("PRD2TC_PROMPT_CACHE", "0").strip().lower()
            in {"1", "true", "yes", "on"},
            repro=os.environ.get("PRD2TC_REPRO", "0").strip().lower() in {"1", "true", "yes", "on"},
            seed=int(os.environ["PRD2TC_SEED"]) if os.environ.get("PRD2TC_SEED") and os.environ["PRD2TC_SEED"].strip().lstrip("-").isdigit() else None,
            stream=os.environ.get("PRD2TC_STREAM", "1").strip().lower() not in {"0", "false", "no", "off"},
            stream_push_ms=int(os.environ.get("PRD2TC_STREAM_PUSH_MS", "500")),
            jobs=max(1, int(os.environ.get("PRD2TC_JOBS", "1") or 1)),
            markdown_transit=os.environ.get("PRD2TC_MARKDOWN_TRANSIT", "0").strip().lower()
            in {"1", "true", "yes", "on"},
            stage_temperatures=stage_values,
        )
