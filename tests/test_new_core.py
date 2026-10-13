import json
from pathlib import Path

from prd2tc.chunking import split_document, locate
from prd2tc.diff import diff_requirements
from prd2tc.diff import merge_incremental_cases
from prd2tc.execution import make_execution_result, validate_execution_result
from prd2tc.traceability import build_traceability, validate_traceability
from prd2tc import stages
from prd2tc import cli
from prd2tc.config import Config
from prd2tc.llm import LLMResult
from prd2tc.io import save_cases
from prd2tc.service import TaskStore
from prd2tc.pipeline import GenerateRequest, PipelineRunner, RunResult


def test_chunking_preserves_tail_and_locates_heading():
    text = "# A\n\n" + ("a " * 40) + "\n\n# B\n\nREQ-B-001 订单支付"
    chunks = split_document(text, chunk_size=60, overlap=10)
    assert chunks and "REQ-B-001" in "".join(c.text for c in chunks)
    assert locate(chunks, "订单支付")


def test_traceability_unknown_and_p0_gap():
    matrix = build_traceability([{"id": "REQ-A-001", "module": "A", "priority": "P0"}], [
        {"id": "TC-A-001", "requirement": ["REQ-X-001"]}
    ])
    errors = validate_traceability(matrix)
    assert any("不存在" in e for e in errors)
    assert any("P0" in e for e in errors)


def test_diff_and_execution_contract():
    old = [{"id": "REQ-A-001", "module": "A", "title": "登录", "description": "成功登录"}]
    new = [{"id": "REQ-A-001", "module": "A", "title": "登录", "description": "登录失败提示"},
           {"id": "REQ-A-002", "module": "A", "title": "退出", "description": "退出"}]
    result = diff_requirements(old, new)
    assert result["summary"]["modified"] == 1 and result["summary"]["added"] == 1
    payload = make_execution_result("run-1", [{"caseId": "TC-A-001", "status": "passed", "durationMs": 1}])
    assert validate_execution_result(payload) == []


def test_diff_preserves_id_when_only_requirement_id_changes():
    old = [{"id": "REQ-A-001", "module": "A", "title": "登录", "acceptanceCriteria": ["成功"]}]
    new = [{"id": "REQ-A-099", "module": "A", "title": "登录", "acceptanceCriteria": ["成功"]}]
    result = diff_requirements(old, new)
    assert result["summary"] == {"added": 0, "modified": 0, "removed": 0, "unchanged": 1}
    assert result["unchanged"][0]["id"] == "REQ-A-001"
    assert result["unchanged"][0]["incomingId"] == "REQ-A-099"


def test_execution_contract_rejects_invalid_envelope_and_unclassified_failure():
    errors = validate_execution_result({"schemaVersion": "2.0", "runId": "", "results": [
        {"caseId": "TC-A-001", "status": "failed", "durationMs": 2, "evidence": []},
        {"caseId": "TC-A-001", "status": "passed", "durationMs": 1},
    ]})
    assert any("schemaVersion" in error for error in errors)
    assert any("runId" in error for error in errors)
    assert any("failureCategory" in error for error in errors)
    assert any("重复" in error for error in errors)


def test_generated_requirement_ids_are_stable_and_markdown_aligned():
    analysis = "| REQ-CRM_CORE-001 | P1 | 客户查询 | 返回客户 | 无 |"
    first = stages.requirements_from_analysis(analysis, "# 客户管理\n支持客户查询")
    second = stages.requirements_from_analysis(analysis, "# 客户管理\n支持客户查询")
    assert first[0]["id"] == second[0]["id"]
    assert first[0]["fingerprint"] == second[0]["fingerprint"]
    assert first[0]["idSource"] == "generated"
    assert first[0]["id"] in stages.align_analysis_ids(analysis, first)


def test_requirement_parser_prefers_detail_row_and_extracts_module_name():
    analysis = """## 1. 功能模块清单
| 模块代号 | 模块名 | 页面/入口 | 需求点编号 |
| AUTH | 账号认证 | /login | REQ-AUTH-001 |
## 3. 需求点明细
| REQ 编号 | 优先级 | 需求描述 | 验收标准 | 异常分支/边界 |
| REQ-AUTH-001 | P0 | 用户登录 | 登录后进入首页 | 密码错误时提示 |
"""
    requirements = stages.requirements_from_analysis(analysis, "REQ-AUTH-001 用户登录")
    assert requirements[0]["title"] == "用户登录"
    assert requirements[0]["priority"] == "P0"
    assert requirements[0]["acceptanceCriteria"] == ["登录后进入首页"]
    assert requirements[0]["exceptionFlows"] == ["密码错误时提示"]
    assert stages.modules_from_analysis(analysis, requirements) == [{
        "id": "AUTH", "name": "账号认证", "entry": "/login", "requirementIds": ["REQ-AUTH-001"]}]


def test_generated_requirements_from_overlapping_chunks_are_deduplicated():
    analysis = """| REQ-CRM-001 | P1 | 客户搜索 | 返回匹配客户 | 无 |
| REQ-CRM-002 | P1 | 客户搜索 | 返回匹配客户 | 无 |
"""
    requirements = stages.requirements_from_analysis(analysis, "# CRM\n支持客户搜索")
    aligned = stages.align_analysis_ids(analysis, requirements)
    assert len(requirements) == 1
    assert requirements[0]["sourceIds"] == ["REQ-CRM-001", "REQ-CRM-002"]
    assert set(__import__("re").findall(r"REQ-[A-Z0-9_]+-\d{3}", aligned)) == {requirements[0]["id"]}


def test_module_generation_and_checkpoint_resume():
    calls = []
    cache = {}

    def fake_llm(cfg, user_text, **kwargs):
        module = "B" if '"module": "B"' in user_text else "A"
        calls.append(module)
        content = json.dumps([{"id": f"TC-{module}-001", "module": module, "title": "场景",
                               "design_method": "场景法", "priority": "P1", "tags": ["@regression"],
                               "requirement": [f"REQ-{module}-001"],
                               "steps": [{"action": "goto", "target": "/"},
                                          {"action": "expectVisible", "target": "首页主体"}],
                                "expected": ["操作成功并跳转首页"]}])
        return LLMResult(content, "fake", 2, 3)

    reqs = [{"id": "REQ-A-001", "module": "A", "title": "A"},
            {"id": "REQ-B-001", "module": "B", "title": "B"}]
    cfg = Config("k", "https://x", "m", .4, 10, 1)
    cases, usage = stages.run_stage_3_modules(
        cfg, "方案", reqs, "# A\nA\n# B\nB", llm=fake_llm, jobs=2, use_gate=False,
        load_checkpoint=lambda m: cache.get(m), save_checkpoint=lambda m, c: cache.__setitem__(m, c))
    assert {c["module"] for c in cases} == {"A", "B"}
    assert usage.total_tokens == 10
    cases2, _ = stages.run_stage_3_modules(
        cfg, "方案", reqs, "# A\nA\n# B\nB", llm=fake_llm, jobs=2, use_gate=False,
        load_checkpoint=lambda m: cache.get(m))
    assert len(calls) == 2 and len(cases2) == 2


def test_dynamic_module_validation():
    case = {"id": "TC-CRM_2-001", "module": "CRM_2", "title": "客户查询", "design_method": "场景法", "priority": "P1",
            "tags": ["@regression"], "requirement": ["REQ-CRM_2-001"],
            "steps": [{"action": "goto", "target": "/customers"},
                     {"action": "expectVisible", "target": "客户列表"}],
            "expected": ["客户列表页成功显示数据"]}
    from prd2tc.validator import validate_case
    assert validate_case(case, 0, set(), {"CRM_2"}) == []


def test_incremental_case_state_machine():
    old = [{"id": "TC-A-001", "requirement": ["REQ-A-001"]},
           {"id": "TC-A-002", "requirement": ["REQ-A-002"]}]
    new = [{"id": "TC-A-001", "requirement": ["REQ-A-001"]}]
    diff = {"modified": [{"id": "REQ-A-001"}], "removed": [{"id": "REQ-A-002"}]}
    merged = merge_incremental_cases(old, new, diff)
    assert merged["cases"][0]["status"] == "active"
    assert {c["status"] for c in merged["history"]} == {"needs_review", "deprecated"}


def test_long_stage1_processes_all_chunks():
    calls = []
    cfg = Config("k", "https://x", "m", .4, 10, 1, chunk_size=80, chunk_overlap=10)
    good = """# 需求分析报告
## 1. 功能模块清单
## 2. 角色清单
## 3. 需求点明细
REQ-A-001 A
REQ-B-001 B
## 4. 业务规则
## 5. 非功能需求
## 6. 风险与疑点
"""

    def fake(cfg, user_text, **kwargs):
        calls.append(user_text)
        return LLMResult(good, "fake", 1, 1)

    prd = "# A\n\nREQ-A-001 " + "甲" * 100 + "\n\n# B\n\nREQ-B-001 " + "乙" * 100
    content, usage, errors = stages.generate_stage_1(cfg, prd, llm=fake, jobs=3, use_gate=True, reporter=lambda *_: None)
    assert errors == [] and "REQ-B-001" in content
    assert len(calls) >= 3  # 至少两个分段 + 一次合并
    assert usage.total_tokens == len(calls) * 2


def test_import_results_cli_writes_schema_v2(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    save_cases(source, [{"id": "TC-A-001", "module": "A", "title": "x", "design_method": "场景法", "priority": "P1",
                         "tags": ["@regression"], "requirement": ["REQ-A-001"],
                         "steps": [{"action": "goto", "target": "/"},
                                 {"action": "expectVisible", "target": "页面主体"}],
                         "expected": ["页面成功渲染并显示预期内容"]}])
    results = tmp_path / "results.json"
    results.write_text(json.dumps(make_execution_result("run-1", [
        {"caseId": "TC-A-001", "status": "passed", "durationMs": 2}
    ])), encoding="utf-8")
    assert cli.main(["import-results", "--cases", str(source), "--results", str(results), "--out", str(output)]) == 0
    payload = json.loads((output / "A.json").read_text(encoding="utf-8"))
    assert payload["schemaVersion"] == "2.0"
    assert payload["actionSchemaVersion"] == "1.1"
    assert payload["cases"][0]["lastExecution"]["runId"] == "run-1"
    examples = tmp_path / "examples.json"
    assert cli.main(["promote-examples", "--cases", str(output), "--out", str(examples),
                     "--design-logic", "已评审且执行通过", "--reviewed"]) == 0
    promoted = json.loads(examples.read_text(encoding="utf-8"))
    assert promoted[0]["reviewed"] is True and promoted[0]["sourceRunIds"] == ["run-1"]


def test_service_task_store_is_persistent(tmp_path):
    store = TaskStore(tmp_path)
    store.save("t1", status="pending", percent=0)
    store.save("t1", status="running", percent=50)
    restored = TaskStore(tmp_path).get("t1")
    assert restored["status"] == "failed" and restored["stage"] == "recovered"


def test_pipeline_runner_persists_success_state(tmp_path):
    prd = tmp_path / "product" / "sample.md"
    prd.parent.mkdir()
    prd.write_text("REQ-AUTH-001 登录", encoding="utf-8")
    events = []
    runner = PipelineRunner(Config("", "https://x", "m", .4, 10, 1))
    result = runner.generate(GenerateRequest(prd=prd, out=tmp_path / "out", product="product",
                                             version="v1", stub=True),
                             progress_callback=lambda stage, percent: events.append((stage, percent)))
    state = json.loads((result.root / "run-state.json").read_text(encoding="utf-8"))
    assert result.status == "succeeded" and state["status"] == "succeeded"
    assert events == [("analysis", 33), ("plan", 66), ("cases", 100)]


def test_cli_generate_is_a_thin_pipeline_runner_adapter(tmp_path, monkeypatch):
    prd = tmp_path / "sample.md"
    prd.write_text("REQ-A-001 查询", encoding="utf-8")
    captured = {}
    def fake_generate(self, request, progress_callback=None):
        captured["request"] = request
        captured["provider"] = self.config.provider
        return RunResult("run", tmp_path / "out", status="succeeded")
    monkeypatch.setattr(PipelineRunner, "generate", fake_generate)
    assert cli.main(["generate", "--prd", str(prd), "--stub", "--provider", "ollama"]) == 0
    assert captured["request"].stub is True
    assert captured["provider"] == "ollama"


def test_diff_cli_same_prd_is_fully_unchanged(tmp_path):
    prd = tmp_path / "product" / "PRD-product-v1.0.md"
    prd.parent.mkdir()
    prd.write_text("# 产品\n\nREQ-AUTH-001 登录功能", encoding="utf-8")
    assert cli.main(["generate", "--prd", str(prd), "--stub", "--out", str(tmp_path / "out")]) == 0
    previous = tmp_path / "out" / "product" / "v1.0"
    assert cli.main(["diff", "--prd", str(prd), "--from", str(previous),
                     "--out", str(tmp_path / "out")]) == 0
    result = json.loads((previous / "diff" / "requirement-diff.json").read_text(encoding="utf-8"))
    assert result["summary"] == {"added": 0, "modified": 0, "removed": 0, "unchanged": 1}
    manifest = json.loads((previous / "run-manifest.json").read_text(encoding="utf-8"))
    assert result["parentRunId"] == manifest["runId"]
    assert result["parentVersion"] == "v1.0"


def test_merge_cli_preserves_changed_and_removed_history(tmp_path):
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    output = tmp_path / "merged"
    base = {"module": "A", "title": "x", "design_method": "场景法", "priority": "P1", "tags": ["@regression"],
            "steps": [{"action": "goto", "target": "/"},
                    {"action": "expectVisible", "target": "页面主体"}],
            "expected": ["页面成功渲染并显示预期内容"]}
    save_cases(old_root / "cases" / "cases", [
        {**base, "id": "TC-A-001", "requirement": ["REQ-A-001"]},
        {**base, "id": "TC-A-002", "requirement": ["REQ-A-002"]},
    ])
    save_cases(new_root / "cases" / "cases", [
        {**base, "id": "TC-A-101", "requirement": ["REQ-A-001"]},
    ])
    diff_path = tmp_path / "diff.json"
    diff_path.write_text(json.dumps({"modified": [{"id": "REQ-A-001"}],
                                     "removed": [{"id": "REQ-A-002"}]}), encoding="utf-8")
    assert cli.main(["merge", "--from", str(old_root), "--new", str(new_root),
                     "--diff", str(diff_path), "--out", str(output)]) == 0
    active = json.loads((output / "cases" / "A.json").read_text(encoding="utf-8"))["cases"]
    history = json.loads((output / "case-history.json").read_text(encoding="utf-8"))
    summary = json.loads((output / "merge-summary.json").read_text(encoding="utf-8"))
    assert [(case["id"], case["status"]) for case in active] == [("TC-A-101", "active")]
    assert {(case["id"], case["status"]) for case in history} == {
        ("TC-A-001", "needs_review"), ("TC-A-002", "deprecated")}
    assert summary["from"] == str(old_root.resolve()) and summary["new"] == str(new_root.resolve())


# ---- 覆盖率门禁失败时的产物保全（运行 9d97060e 的教训）----

_ANALYSIS_2_REQS = """# 需求分析报告
## 1. 功能模块清单
| 模块代号 | 模块名 | 页面/入口 | 需求点编号 |
| A | 模块A | /a | REQ-A-001 ~ REQ-A-002 |
## 2. 角色清单
| 角色 | 说明 | 关键权限/状态 |
| 用户 | 普通 | - |
## 3. 需求点明细
| REQ 编号 | 优先级 | 需求描述 | 验收标准 | 异常分支/边界 |
| REQ-A-001 | P0 | 需求一 | 成功渲染页面 | - |
| REQ-A-002 | P0 | 需求二 | 成功渲染列表 | - |
## 4. 业务规则
| 编号 | 规则 | 影响面 | 测试要点 |
| BR-1 | 无 | - | - |
## 5. 非功能需求
| 编号 | 类别 | 可验证指标 |
| NFR-1 | 性能 | 2s |
## 6. 风险与疑点
| 风险 | 影响 | 应对 |
| 无 | - | - |
"""

_PLAN_7 = """# 测试方案
## 1. 测试范围
In Scope: A
## 2. 测试策略
功能
## 3. 优先级口径
P0-P3
## 4. 风险与应对
无
## 5. 测试环境与数据
stub
## 6. 准入 / 准出标准
通过
## 7. 用例量目标
2
"""


def _coverage_failing_pipeline(tmp_path):
    """构造「用例只覆盖 REQ-A-001、漏掉 P0 的 REQ-A-002」的运行。"""
    def fake(cfg, user_text, **kwargs):
        if "PRD 全文" in user_text:
            return LLMResult(_ANALYSIS_2_REQS, "m")
        if "测试方案排版模板" in user_text:
            return LLMResult(_PLAN_7, "m")
        return LLMResult("```json\n" + json.dumps([{
            "id": "TC-A-001", "module": "A", "title": "需求一验证",
            "design_method": "场景法", "priority": "P0", "tags": ["@smoke"],
            "requirement": ["REQ-A-001"],
            "steps": [{"action": "goto", "target": "/a", "data": ""},
                       {"action": "expectVisible", "target": "页面主体", "data": ""}],
            "expected": ["页面成功渲染并显示数据"]}], ensure_ascii=False) + "\n```", "m")

    cfg = Config(api_key="", api_base="local", model="m", temperature=0.0,
                 timeout=10, max_retries=0, provider="ollama")
    prd = tmp_path / "prd.md"
    prd.write_text("# PRD\nREQ-A-001 REQ-A-002\n", encoding="utf-8")
    runner = PipelineRunner(cfg, llm=fake)
    return runner.generate(GenerateRequest(prd=prd, out=tmp_path / "out", product="p",
                                           version="v1", stub=True, no_gate=True))


def test_partial_cases_preserved_when_coverage_gate_fails(tmp_path):
    """覆盖率门禁失败时不得丢弃已生成用例（实测曾丢弃 43 条）。

    改为写入 cases/partial/ 并附「勿直接采信」标记；正式 cases/cases/ 保持为空，
    避免失败产物被当成正式结果。
    """
    result = _coverage_failing_pipeline(tmp_path)
    assert result.status == "failed"
    assert any("P0" in e for e in result.errors), result.errors

    partial = result.root / "cases" / "partial"
    assert partial.is_dir(), "已生成用例应落到 cases/partial/"
    kept = json.loads((partial / "A.json").read_text(encoding="utf-8"))["cases"]
    assert len(kept) == 1 and kept[0]["id"] == "TC-A-001"

    notes = [p.name for p in partial.glob("*.md")]
    assert notes and any("未通过门禁" in n or "READ-ME" in n for n in notes), notes

    official = result.root / "cases" / "cases"
    assert not list(official.glob("*.json")), (
        "正式产物目录不得写入未通过门禁的用例"
    )


def test_partial_manifest_records_case_count(tmp_path):
    """manifest 记录部分用例数量，便于 UI/用户判断损失范围。"""
    result = _coverage_failing_pipeline(tmp_path)
    manifest = json.loads((result.root / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest["stages"]["cases"].get("partialCaseCount") == 1


def test_no_partial_dir_when_run_succeeds(tmp_path):
    """对照：正常成功时不应产生 partial 目录。"""
    sample = Path(__file__).parents[1] / "examples" / "sample-prd.md"
    out = tmp_path / "ok"
    result = PipelineRunner(
        Config(api_key="", api_base="local", model="m", temperature=0.0, timeout=10,
               max_retries=1, provider="ollama")
    ).generate(GenerateRequest(prd=sample, out=out, product="p", version="v1", stub=True))
    assert result.status == "succeeded"
    assert not (result.root / "cases" / "partial").exists()
    assert list((result.root / "cases" / "cases").glob("*.json"))


def test_generate_module_failure_persists_independent_statuses(tmp_path, monkeypatch):
    prd = tmp_path / "product" / "PRD-product-v1.0.md"
    prd.parent.mkdir()
    prd.write_text("# 功能\n\nREQ-A-001 查询\n\nREQ-B-001 删除", encoding="utf-8")
    analysis = """# 需求分析报告
## 1. 功能模块清单
## 2. 角色清单
## 3. 需求点明细
| REQ-A-001 | P1 | 查询 | 返回结果 | 无 |
| REQ-B-001 | P1 | 删除 | 删除成功 | 无 |
## 4. 业务规则
## 5. 非功能需求
## 6. 风险与疑点
"""
    plan = "# 测试方案\n\n模块化验证"

    def fake_llm(cfg, user_text, **kwargs):
        if "PRD 全文" in user_text:
            return LLMResult(analysis, "fake")
        if "测试方案排版模板" in user_text:
            return LLMResult(plan, "fake")
        module = "B" if '"module": "B"' in user_text else "A"
        if module == "B":
            raise RuntimeError("simulated module failure")
        case = {"id": "TC-A-001", "module": "A", "title": "查询", "design_method": "场景法", "priority": "P1",
                "tags": ["@regression"], "requirement": ["REQ-A-001"],
                "steps": [{"action": "goto", "target": "/"},
                    {"action": "expectVisible", "target": "页面主体"}],
            "expected": ["页面成功渲染并显示预期内容"]}
        return LLMResult(json.dumps([case]), "fake")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    monkeypatch.setattr(cli.S, "chat_completion", fake_llm)
    code = cli.main(["generate", "--prd", str(prd), "--out", str(tmp_path / "out"),
                     "--no-gate", "--jobs", "2"])
    assert code == 1
    root = tmp_path / "out" / "product" / "v1.0"
    state = json.loads((root / "run-state.json").read_text(encoding="utf-8"))
    manifest = json.loads((root / "run-manifest.json").read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert state["modules"] == {"A": "succeeded", "B": "failed"}
    assert manifest["status"] == "failed"
    assert manifest["stages"]["cases"]["status"] == "failed"
    assert "simulated module failure" in json.dumps(manifest["stages"]["cases"])
    assert (root / "cases" / ".checkpoints" / "A.json").is_file()
