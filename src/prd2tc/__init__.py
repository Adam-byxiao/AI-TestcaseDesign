"""prd2tc —— PRD → 测试方案 → 测试用例 的独立大模型驱动生成工具。

CLI 入口：prd2tc generate/init/validate/stats（见 cli.py）。
零第三方依赖：HTTP 用标准库 urllib 调用 OpenAI、Anthropic 和 Ollama Provider。
"""

__version__ = "0.5.0"
