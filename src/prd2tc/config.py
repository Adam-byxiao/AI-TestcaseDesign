"""配置：环境变量 + 参数解析（OpenAI 兼容协议，默认 DeepSeek）。

环境变量：
  DEEPSEEK_API_KEY / PRD2TC_API_KEY    API Key（必填，除非 --stub/--dry-run）
  PRD2TC_API_BASE                      兼容端点，默认 https://api.deepseek.com
  PRD2TC_MODEL                         模型名，默认 deepseek-chat
  PRD2TC_TEMPERATURE                   温度，默认 0.4
  PRD2TC_TIMEOUT                       请求超时秒，默认 120
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

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            api_key=os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("PRD2TC_API_KEY") or "",
            api_base=os.environ.get("PRD2TC_API_BASE", "https://api.deepseek.com").rstrip("/"),
            model=os.environ.get("PRD2TC_MODEL", "deepseek-chat"),
            temperature=float(os.environ.get("PRD2TC_TEMPERATURE", "0.4")),
            timeout=int(os.environ.get("PRD2TC_TIMEOUT", "120")),
            max_retries=int(os.environ.get("PRD2TC_MAX_RETRIES", "3")),
        )
