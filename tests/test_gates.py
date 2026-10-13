"""机器门禁测试：阶段校验 + 反馈修正重出机制。"""
from prd2tc import gates, stages
from prd2tc.config import Config
from prd2tc.llm import LLMResult

GOOD_ANALYSIS = """# 需求分析报告

## 1. 功能模块清单
| 模块 | 说明 |
## 2. 角色清单
| 角色 | 说明 |
## 3. 需求点明细
REQ-AUTH-001 登录
## 4. 业务规则
| 编号 | 规则 |
## 5. 非功能需求
| 编号 | 类别 |
## 6. 风险与疑点
| 风险 | 应对 |
"""

GOOD_PLAN = """# 测试方案

## 1. 测试范围
In Scope / Out Scope
## 2. 测试策略
功能/边界/权限
## 3. 优先级口径
P0-P3
## 4. 风险与应对
| 风险 | 应对 |
## 5. 测试环境与数据
BASE_URL、账号
## 6. 准入 / 准出标准
准入：…
准出：…
## 7. 用例量目标
| 模块 | 数量 |
"""

VALID_CASES = (
    "```json\n"
    '[{"id":"TC-A-001","module":"AUTH","title":"t","design_method": "场景法", "priority":"P1",'
    '"tags":["@regression"],"requirement":["REQ-AUTH-001"],'
    '"steps":[{"action":"goto","target":"/","data":""},{"action":"expectVisible","target":"页面主体","data":""}],"expected":["页面成功渲染并显示预期内容"]}]\n'
    "```"
)


def test_check_analysis_sections():
    assert gates.check_analysis(GOOD_ANALYSIS) == []
    errors = gates.check_analysis("只有需求点明细")
    assert any("缺少必备章节" in e for e in errors)


def test_check_analysis_req_coverage():
    prd = "REQ-AUTH-001 REQ-AUTH-002"
    # 报告遗漏 PRD 中的 REQ-AUTH-002
    errors = gates.check_analysis(GOOD_ANALYSIS, prd)
    assert any("REQ-AUTH-002" in e for e in errors)
    # 报告包含全部 REQ 时通过
    ok = gates.check_analysis(GOOD_ANALYSIS + "REQ-AUTH-002", prd)
    assert ok == []
    # PRD 无 REQ 编号时跳过覆盖检查
    assert gates.check_analysis(GOOD_ANALYSIS, "无编号 PRD") == []


def test_check_plan_sections():
    assert gates.check_plan(GOOD_PLAN) == []
    errors = gates.check_plan("缺少范围")
    assert any("缺少必备章节" in e for e in errors)


def test_check_cases_text():
    assert gates.check_cases_text(VALID_CASES) == []
    assert any("解析失败" in e for e in gates.check_cases_text("没有 JSON"))
    bad = '```json\n[{"id":"BAD","module":"XXX","priority":"PX"}]\n```'
    assert gates.check_cases_text(bad)  # 字段校验错误非空


class _FakeLLM:
    """按调用次数返回不同内容：验证重试与反馈注入。"""

    def __init__(self, responses: list[str]):
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, cfg, user_text: str, **kwargs):
        self.calls.append(user_text)
        content = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        return LLMResult(content=content, model="fake")


def test_generate_with_feedback_retries():
    """首次输出缺章节 → 反馈重试；第二次通过。"""
    fake = _FakeLLM(["只有需求点明细", GOOD_ANALYSIS])
    cfg = Config(api_key="k", api_base="https://x", model="m", temperature=0.4, timeout=10, max_retries=1)
    content, result, errors = stages.generate_with_feedback(
        cfg,
        lambda fb: stages.build_stage_1_user("PRD 内容", feedback=fb),
        lambda t: gates.check_analysis(t),
        llm=fake,
        max_retries=2,
        label="测试",
    )
    assert errors == []
    assert len(fake.calls) == 2
    assert result.gate_retries == 1
    # 第二次调用的 user 消息包含门禁反馈
    assert "门禁反馈" in fake.calls[1]
    assert "缺少必备章节" in fake.calls[1]


def test_generate_with_feedback_gives_up():
    """始终不过门禁 → 返回最后一次错误，不无限重试。"""
    fake = _FakeLLM(["bad", "bad", "bad"])
    cfg = Config(api_key="k", api_base="https://x", model="m", temperature=0.4, timeout=10, max_retries=1)
    content, result, errors = stages.generate_with_feedback(
        cfg,
        lambda fb: stages.build_stage_2_user("分析", feedback=fb),
        gates.check_plan,
        llm=fake,
        max_retries=2,
        label="测试",
    )
    assert errors  # 最终仍失败
    assert len(fake.calls) == 3  # 1 次原始 + 2 次重试
    assert result.gate_retries == 2


def test_analysis_gate_supports_dynamic_module_with_underscore():
    text = GOOD_ANALYSIS + "\nREQ-CRM_CORE-001"
    assert not any("REQ-CRM_CORE-001" in error for error in gates.check_analysis(
        text, "REQ-CRM_CORE-001 客户管理"))


def test_generate_with_feedback_truncation_diagnosis():
    """输出被长度截断（finish_reason=length）时，错误清单必须带诊断提示。"""

    class _TruncatedLLM:
        def __init__(self):
            self.calls = 0

        def __call__(self, cfg, user_text: str, **kwargs):
            self.calls += 1
            return LLMResult(content="只有需求点明细", model="fake", finish_reason="length")

    fake = _TruncatedLLM()
    cfg = Config(api_key="k", api_base="https://x", model="m", temperature=0.4, timeout=10, max_retries=1)
    content, result, errors = stages.generate_with_feedback(
        cfg,
        lambda fb: stages.build_stage_2_user("分析", feedback=fb),
        gates.check_plan,
        llm=fake,
        max_retries=0,
        label="测试",
    )
    assert any("截断" in e for e in errors), "截断诊断必须出现在错误清单"
    assert any("缺少必备章节" in e for e in errors), "原始门禁错误仍需保留"


def test_generate_with_feedback_accumulates_retry_tokens():
    """token 统计应累积所有调用（含失败重试），而不是只记最后一次。"""

    class _RetryLLM:
        def __init__(self):
            self.calls = 0

        def __call__(self, cfg, user_text: str, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResult(content="只有需求点明细", model="fake",
                                 prompt_tokens=100, completion_tokens=50)
            return LLMResult(content=GOOD_PLAN, model="fake",
                             prompt_tokens=80, completion_tokens=40)

    fake = _RetryLLM()
    cfg = Config(api_key="k", api_base="https://x", model="m", temperature=0.4, timeout=10, max_retries=1)
    content, result, errors = stages.generate_with_feedback(
        cfg,
        lambda fb: stages.build_stage_2_user("分析", feedback=fb),
        gates.check_plan,
        llm=fake,
        max_retries=1,
        label="测试",
    )
    assert errors == []
    assert result.total_tokens == 100 + 50 + 80 + 40, "应包含第一次失败调用的 token"
