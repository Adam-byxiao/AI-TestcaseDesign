"""Prompt 工程十原则回归测试（锁定：分隔符 / 近因 / 角色 / 温度 / 少样本 / 负向约束）。"""
from pathlib import Path

import pytest

from prd2tc import stages
from prd2tc.config import Config
from prd2tc.llm import LLMResult

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "examples"


class _CaptureLLM:
    """记录每次调用的 (user, system, temperature)。"""

    def __init__(self):
        self.calls = []

    def __call__(self, cfg, user_text, **kwargs):
        self.calls.append((user_text, kwargs.get("system"), kwargs.get("temperature")))
        return LLMResult(content="# 需求分析报告\n\n## 1. 功能模块清单\n\n## 2. 角色清单\n\n## 3. 需求点明细\n\n## 4. 业务规则\n\n## 5. 非功能需求\n\n## 6. 风险与疑点", model="fake")


def _cfg() -> Config:
    return Config(api_key="k", api_base="https://x", model="m", temperature=0.4, timeout=10, max_retries=1, stage_temperatures=(0.2, 0.3, 0.3))


def test_inputs_wrapped_in_delimiters():
    """原则 4：输入数据用 <key>…</key> 分隔符包裹，与指令隔离。"""
    user = stages.build_stage_1_user("PRD 内容")
    assert "<PRD 全文>" in user and "</PRD 全文>" in user
    user3 = stages.build_stage_3_user("方案", requirements=[{"id": "REQ-AUTH-001", "module": "AUTH", "title": "t"}])
    assert "<结构化需求" in user3


def test_output_requirements_at_end():
    """原则 10：输出要求置末（近因偏差）。"""
    users = [
        ("stage1", stages.build_stage_1_user("PRD")),
        ("stage2", stages.build_stage_2_user("分析")),
        ("stage3", stages.build_stage_3_user("方案", requirements=[{"id": "REQ-AUTH-001", "module": "AUTH", "title": "t"}])),
    ]
    for name, user in users:
        assert user.index("# 输出要求") > user.index("# 输入数据"), name


def test_feedback_placed_last():
    """门禁反馈必须出现在输出要求之后（最强近因）。"""
    user = stages.build_stage_3_user("方案", feedback="错误清单：缺少章节 X")
    assert user.rindex("门禁反馈") > user.index("# 输出要求")


def test_stage_roles_and_temperature():
    """原则 5/8：分阶段角色与温度（事实抽取低温度）。

    阶段 3 自 v0.6.0 起由 0.5 降为 0.3：它是全链路最需要确定性的环节
    （结构化 JSON + 字段强校验 + 程序化重编号），不应是温度最高的一环。
    """
    for stage, role_kw, expected_temp in [
        (1, "测试架构师", 0.2),
        (2, "测试经理", 0.3),
        (3, "测试设计专家", 0.3),
    ]:
        fake = _CaptureLLM()
        if stage == 1:
            stages.run_stage_1(_cfg(), "PRD", llm=fake)
        elif stage == 2:
            stages.run_stage_2(_cfg(), "分析", llm=fake)
        else:
            with pytest.raises(ValueError):  # stub 内容无 JSON，仅验证参数传递
                stages.run_stage_3(_cfg(), "方案", llm=fake)
        _, system, temperature = fake.calls[0]
        assert role_kw in (system or ""), f"阶段{stage} 角色应含「{role_kw}」"
        assert temperature == expected_temp, f"阶段{stage} 温度应为 {expected_temp}"


def test_stage3_prompt_has_few_shot_and_negative():
    """原则 2/6：阶段 3 含少样本示例与负向约束。"""
    user = stages.build_stage_3_user("方案")
    assert "数据示例" in user and "TC-AUTH-LOGIN-001" in user
    assert "不要输出 JSON 之外的任何字符" in user
    assert "不要重复用例" in user


def test_prompt_versioned():
    """原则 9：prompt 带版本号（实证迭代可追溯）。"""
    for asset in ("prompts/00-testing-principles.md", "prompts/01-prd-analysis.md",
                  "prompts/02-test-plan.md", "prompts/03-test-cases.md", "prompts/04-case-constraints.md"):
        text = stages.loader.load_asset(asset)
        assert "prompt v" in text, asset
