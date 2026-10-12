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
