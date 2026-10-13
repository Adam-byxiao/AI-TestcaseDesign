"""学习能力（示例库）测试：加载 / 检索 / 渲染注入 / 端到端。"""
from pathlib import Path

import pytest

from prd2tc import cli, examples, stages
from prd2tc.config import Config
from prd2tc.validator import validate_dir

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "examples"
# 端到端 fixture 用仓库内自带的 PRD（旧版指向仓库外 PRD_File/，导致新克隆者必失败）
SAMPLE_PRD = REPO_ROOT / "examples" / "sample-prd.md"
PRODUCT = "examples"
VERSION = "v0.1"


def test_load_examples():
    exs = examples.load_examples(EXAMPLES_DIR)
    assert len(exs) >= 3
    assert all(e.module and e.cases for e in exs)
    assert any(e.module == "AUTH" for e in exs)
    assert any(e.design_logic for e in exs)


def test_retrieve_relevance():
    exs = examples.load_examples(EXAMPLES_DIR)
    # 查询包含结算/金额关键词 → 应命中 CHECKOUT
    picked = examples.retrieve(exs, "结算 应付金额 运费 优惠券 满100减20 提交订单", k=2)
    assert picked and picked[0].module == "CHECKOUT"
    # 查询包含登录/停用 → 应命中 AUTH
    picked2 = examples.retrieve(exs, "登录 手机号 密码 停用账号 拦截", k=2)
    assert picked2 and picked2[0].module == "AUTH"


def test_retrieve_empty_for_unrelated():
    exs = examples.load_examples(EXAMPLES_DIR)
    assert examples.retrieve(exs, "zzz 无关内容 qqq", k=3) == []


def test_render_examples():
    exs = examples.load_examples(EXAMPLES_DIR)
    md = examples.render_examples(exs[:2])
    assert "参考示例" in md and "设计逻辑" in md
    assert md.count("```json") >= 2


def test_stage_injection_with_examples():
    cfg = Config(api_key="k", api_base="https://x", model="m", temperature=0.4, timeout=10, max_retries=1)
    exs = examples.retrieve(examples.load_examples(EXAMPLES_DIR), "登录 账号", k=1)
    content, _ = stages.run_stage_1(cfg, "PRD 内容", llm=cli._stub_chat, examples=exs)
    assert "需求分析报告" in content


def test_generate_with_examples_end_to_end(tmp_path):
    """带示例库跑 stub 全流程：示例被加载并注入，产物仍通过校验"""
    code = cli.main([
        "generate", "--prd", str(SAMPLE_PRD), "--stub",
        "--examples", str(EXAMPLES_DIR), "--examples-k", "2",
        "--out", str(tmp_path),
    ])
    assert code == 0
    out = tmp_path / PRODUCT / VERSION
    assert (out / "cases" / "用例集.md").is_file()
    total, errors = validate_dir(out / "cases" / "cases")
    assert errors == []
    assert total == 1


# ---------- 示例库必须自身合规（它是被注入提示词的「示范」） ----------

def _all_example_cases():
    import json
    cases = []
    for path in sorted(EXAMPLES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for group in (data if isinstance(data, list) else [data]):
            cases.extend(group.get("cases") or [])
    return cases


def test_example_library_passes_its_own_smoke_gate():
    """**核心回归**：示例库的 `@smoke` 必须能通过项目自己的冒烟门禁。

    为什么必须锁住：示例库是**注入提示词的学习信号** —— 它一旦与门禁规则矛盾，
    就等于用错误示范教模型。实测该库曾把 5/5 条 P0 全部标为 `@smoke`
    （正是「@smoke 退化为 P0 同义词」这一缺陷的样本），并触发门禁报错
    「冒烟集过大：5 条 @smoke 超过上限 1 条」。
    """
    from prd2tc import validator
    cases = _all_example_cases()
    assert cases, "示例库为空"
    smoke = [c["id"] for c in cases if "@smoke" in (c.get("tags") or [])]
    p0 = [c["id"] for c in cases if c.get("priority") == "P0"]
    assert validator.check_smoke_set(smoke, p0, len(cases)) == []


def test_example_library_smoke_matches_system_choice():
    """示例库的 `@smoke` 应与系统自身的选择结果**完全一致**。

    这样示例才是「当前规则的真实样子」，而不是一份手工维护、终将漂移的副本。
    """
    from prd2tc import priority
    cases = _all_example_cases()
    declared = sorted(c["id"] for c in cases if "@smoke" in (c.get("tags") or []))
    assert declared == sorted(priority.select_smoke(cases))


def test_example_library_design_logic_states_current_smoke_rule():
    """`design_logic` 是会被模型读到的**规则声明**，不得陈述已废弃的规则。

    实测事故：该字段曾写「主链路为 P0 必带 @smoke」—— 正是被废弃的口径
    （现规则是「@smoke 是 P0 的真子集，仅主干、每需求最多 1 条」）。
    规则文本与门禁不一致时，模型会照文本做然后被门禁拦下。
    """
    import json
    texts = []
    for path in sorted(EXAMPLES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for group in (data if isinstance(data, list) else [data]):
            if group.get("design_logic"):
                texts.append(group["design_logic"])
    joined = "\n".join(texts)
    assert "必带 @smoke" not in joined, "design_logic 仍在陈述废弃规则「P0 必带 @smoke」"
    assert "@smoke" in joined, "示例库应说明 @smoke 的口径"

