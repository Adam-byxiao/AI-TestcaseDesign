"""离线端到端测试：stub LLM 跑通三阶段生成 + 校验（无需 API key）。"""
import json
import re
from pathlib import Path

import pytest

from prd2tc import cli, loader, stages
from prd2tc.config import Config
from prd2tc.validator import validate_dir

REPO_ROOT = Path(__file__).resolve().parents[1]  # 01-AI-TestCaseDesign/
SAMPLE_PRD = REPO_ROOT.parent / "PRD_File" / "youxuan-mall" / "PRD-优选商城-v0.3.md"


def test_extract_cases_fenced_and_plain():
    fenced = '```json\n[{"id": "TC-A-001"}]\n```'
    assert stages.extract_cases(fenced) == [{"id": "TC-A-001"}]
    plain = '前缀 [{"id": "TC-A-001"}, {"id": "TC-A-002"}] 后缀'
    assert stages.extract_cases(plain) == [{"id": "TC-A-001"}, {"id": "TC-A-002"}]


def test_extract_cases_missing():
    with pytest.raises(ValueError):
        stages.extract_cases("没有 JSON")


def test_stage_prompt_building():
    cfg = Config(api_key="k", api_base="https://x", model="m", temperature=0.4, timeout=10, max_retries=1)
    content, result = stages.run_stage_1(cfg, "PRD 内容", llm=cli._stub_chat)
    assert "需求分析报告" in content
    assert result.model == "stub"
    # prompt 构造：任务指令 + 阶段输入
    user = stages.build_user_message(loader.load_asset(stages.PROMPT_1), {"PRD 全文": "x"})
    assert "PRD 全文" in user and "任务指令" in user


def test_generate_stub_end_to_end(tmp_path):
    """generate --stub 全流程：三阶段产物 + 校验通过"""
    code = cli.main(["generate", "--prd", str(SAMPLE_PRD), "--stub", "--out", str(tmp_path)])
    assert code == 0

    out = tmp_path / "youxuan-mall" / "v0.3"
    assert (out / "manifest.json").is_file()
    assert (out / "analysis" / "需求分析报告.md").is_file()
    assert (out / "plan" / "测试方案.md").is_file()
    assert (out / "cases" / "用例集.md").is_file()
    cases_file = out / "cases" / "cases" / "AUTH.json"
    assert cases_file.is_file()

    total, errors = validate_dir(out / "cases" / "cases")
    assert errors == []
    assert total == 1

    md = (out / "cases" / "用例集.md").read_text(encoding="utf-8")
    assert "Traceability" in md or "需求回溯表" in md
    assert "TC-STUB-001" in md


def test_validate_and_stats_commands(tmp_path):
    """validate/stats 命令对 stub 产物工作正常"""
    cli.main(["generate", "--prd", str(SAMPLE_PRD), "--stub", "--out", str(tmp_path)])
    cases_dir = tmp_path / "youxuan-mall" / "v0.3" / "cases" / "cases"
    assert cli.main(["validate", "--dir", str(cases_dir)]) == 0
    assert cli.main(["stats", "--dir", str(cases_dir)]) == 0


def test_generate_requires_key_without_stub(capsys):
    """未配置 API key 且非 stub/dry-run 时应报错退出"""
    code = cli.main(["generate", "--prd", str(SAMPLE_PRD), "--out", str(Path("."))])
    assert code == 2
    assert "API Key" in capsys.readouterr().out


def test_dry_run_no_call(tmp_path):
    """dry-run 应打印 prompt 且不产出阶段文件"""
    code = cli.main(["generate", "--prd", str(SAMPLE_PRD), "--dry-run", "--out", str(tmp_path)])
    assert code == 0
    out = tmp_path / "youxuan-mall" / "v0.3"
    assert not (out / "analysis" / "需求分析报告.md").exists()
