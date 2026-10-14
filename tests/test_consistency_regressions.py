"""输入、续跑与产物一致性的离线回归；不访问真实模型。"""
import io
import json
import zipfile
from dataclasses import replace

import pytest

from prd2tc import artifacts, ingest, stages
from prd2tc.config import Config
from prd2tc.diff import diff_requirements
from prd2tc.io import load_cases, save_cases
from prd2tc.llm import LLMResult
from prd2tc.pipeline import GenerateRequest, PipelineRunner
from prd2tc.stub import stub_chat


@pytest.fixture
def run_input(tmp_path):
    prd = tmp_path / "PRD.md"
    prd.write_text("# 登录需求\n\nREQ-AUTH-001 用户登录\n", encoding="utf-8")
    return Config("", "https://offline.invalid", "audit", .4, 1, 1), GenerateRequest(
        prd=prd, out=tmp_path / "out", product="audit", version="v1", stub=True,
        analyze_images=False)


def test_rejected_stale_run_never_rewrites_provenance(run_input):
    cfg, req = run_input
    root = PipelineRunner(cfg).generate(req).root
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    req.prd.write_text("# 注册需求\n\nREQ-AUTH-001 用户注册\n", encoding="utf-8")
    for _ in range(2):
        with pytest.raises(ValueError, match="stale|另一份 PRD"):
            PipelineRunner(cfg).generate(replace(req, stage="3"))
        assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_resume_regenerates_empty_plan_and_downstream(run_input):
    cfg, req = run_input
    root = PipelineRunner(cfg).generate(req).root
    (root / "plan/测试方案.md").write_text("", encoding="utf-8")
    calls = []
    def counting(cfg, prompt, **kwargs):
        calls.append(prompt)
        return stub_chat(cfg, prompt, **kwargs)
    result = PipelineRunner(cfg, llm=counting).generate(replace(req, resume=True))
    assert result.status == "succeeded"
    assert artifacts.check_one(root, artifacts.PLAN_MD) == []
    assert len(calls) == 2  # analysis 保留；plan 与 cases 必须重跑


def test_generation_mode_change_invalidates_checkpoints(run_input):
    cfg, req = run_input
    first = PipelineRunner(cfg).generate(req)
    old = json.loads((first.root / "run-state.json").read_text(encoding="utf-8"))
    PipelineRunner(replace(cfg, markdown_transit=True)).generate(replace(req, resume=True))
    new = json.loads((first.root / "run-state.json").read_text(encoding="utf-8"))
    assert old["configHash"] != new["configHash"]
    assert new["modules"]["AUTH"] == "succeeded"


def test_edited_upstream_invalidates_case_checkpoint(run_input):
    cfg, req = run_input
    root = PipelineRunner(cfg).generate(req).root
    with (root / "plan/测试方案.md").open("a", encoding="utf-8") as f:
        f.write("\n人工补充：重点覆盖注册边界。\n")
    calls = []
    def counting(cfg, prompt, **kwargs):
        calls.append(prompt)
        return stub_chat(cfg, prompt, **kwargs)
    PipelineRunner(cfg, llm=counting).generate(replace(req, stage="3", resume=True))
    assert len(calls) == 1


def test_save_cases_replaces_module_snapshot(tmp_path):
    save_cases(tmp_path, [{"id": "TC-AUTH-001", "module": "AUTH"}])
    save_cases(tmp_path, [{"id": "TC-CART-001", "module": "CART"}])
    assert [c["module"] for c in load_cases(tmp_path)] == ["CART"]
    save_cases(tmp_path, [])
    assert load_cases(tmp_path) == []


@pytest.mark.parametrize("field,value", [("priority", "P0"), ("excluded", True)])
def test_diff_detects_metadata_change(field, value):
    old = {"id": "REQ-AUTH-001", "module": "AUTH", "title": "登录", "priority": "P1"}
    report = diff_requirements([old], [{**old, field: value}])
    assert report["summary"]["modified"] == 1


@pytest.mark.parametrize("temperature", [0., .9])
def test_explicit_config_temperature_reaches_every_stage(temperature):
    cfg = Config("", "offline", "audit", temperature, 1, 1)
    assert [stages.stage_temperature(s, cfg) for s in (1, 2, 3)] == [temperature] * 3


def test_uploaded_and_zipped_prototypes_keep_ui_contract(tmp_path):
    html = b'<html><body><button id="login-submit">Login</button></body></html>'
    inputs = [("PRD.md", b"# Login requirement"), ("prototype.html", html)]
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        for name, data in inputs:
            z.writestr(name, data)
    runner = PipelineRunner(Config("", "offline", "audit", .4, 1, 1))
    for entries in (inputs, [("prd.zip", archive.getvalue())]):
        spec, _ = runner._auto_prototypes(ingest.ingest(entries), tmp_path)
        assert spec is not None
        assert "login-submit" in spec.anchor_values()


def test_pipeline_uses_default_vision_client_not_text_client(tmp_path, monkeypatch):
    calls = []
    def vision(cfg, prompt, image_data, **kwargs):
        calls.append(image_data)
        return LLMResult('{"summary":"登录截图","facts":["有登录按钮"]}', "vision")
    monkeypatch.setattr("prd2tc.llm.vision_completion", vision)
    bundle = ingest.ingest([("PRD.md", b"# Login"), ("screen.png", b"\x89PNG\r\n\x1a\nimage")])
    runner = PipelineRunner(Config("", "offline", "audit", .4, 1, 1))
    runner._load_images(bundle, tmp_path, request=GenerateRequest(), llm=runner.llm)
    assert len(calls) == 1


def test_large_whitespace_plan_is_incomplete(tmp_path):
    path = tmp_path / artifacts.PLAN_MD.rel
    path.parent.mkdir()
    path.write_text(" " * 100, encoding="utf-8")
    assert artifacts.check_one(tmp_path, artifacts.PLAN_MD)[0].code == "empty"
