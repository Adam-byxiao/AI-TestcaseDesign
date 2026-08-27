"""约束模块测试：学习模式提取 / 模板填充 / 三阶段注入。"""
from pathlib import Path

from prd2tc import constraints, examples, stages
from prd2tc.config import Config

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "examples"
SAMPLE_PRD = REPO_ROOT.parent / "PRD_File" / "youxuan-mall" / "PRD-优选商城-v0.3.md"


def test_seed_examples_valid_json():
    import json

    data = json.loads((EXAMPLES_DIR / "youxuan-mall-examples.json").read_text(encoding="utf-8"))
    assert len(data) == 3
    cases = [c for e in data for c in e["cases"]]
    assert len(cases) == 6
    assert all("design_method" in c for c in cases)


def test_infer_methods_from_design_logic():
    methods = constraints.infer_methods("金额断言要等异步加载完成；优惠券门槛临界值")
    assert "边界值分析" in methods


def test_build_learned_patterns():
    exs = examples.load_examples(EXAMPLES_DIR)
    text = constraints.build_learned_patterns(exs)
    assert "示例库覆盖模块" in text
    assert "优先级口径" in text
    assert "设计逻辑" in text


def test_build_case_constraints_template():
    exs = examples.load_examples(EXAMPLES_DIR)
    block = constraints.build_case_constraints(exs)
    assert "静态约束" in block
    assert "设计方法" in block
    assert "{{learned_patterns}}" not in block  # 占位符已被填充
    assert "反模式禁止" in block


def test_build_case_constraints_without_examples():
    block = constraints.build_case_constraints([])
    assert "无参考示例" in block


def test_principles_loadable():
    text = constraints.load_principles()
    assert "等价类划分" in text and "边界值分析" in text


def test_principles_enforce_constraints_language():
    """原理层必须是「可执行的约束语句」而非纯叙述：须含约束分级、条件分支、自检门。"""
    text = constraints.load_principles()
    assert "[必须]" in text, "必须包含强制级约束标记"
    assert "[禁止]" in text, "必须包含禁止级约束标记"
    assert "条件分支" in text, "必须包含条件分支规则"
    assert "输出自检门" in text, "必须包含输出自检门"
    assert "检查" in text, "每条规则必须带检查方式"
    # 叙述性弱语句（无约束效力）不应作为唯一表达
    assert text.count("必须") >= 10


def test_stage3_injects_constraints_and_examples():
    """阶段 3 的 user 消息应同时包含输出要求区、约束块与参考示例。"""
    cfg = Config(api_key="k", api_base="https://x", model="m", temperature=0.4, timeout=10, max_retries=1)
    exs = examples.load_examples(EXAMPLES_DIR)
    user = stages.build_stage_3_user("…", examples=exs)
    assert "# 输出要求" in user, "输出要求必须置末（近因偏差）"
    assert "反模式禁止" in user
    assert "设计逻辑（必须学习）" in user
    # 输出要求应位于输入数据之后
    assert user.index("# 输出要求") > user.index("# 输入数据")
