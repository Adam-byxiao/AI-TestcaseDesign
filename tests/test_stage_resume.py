"""阶段产物完备性与分段执行测试。

背景：原先「能否从中间阶段开始跑」只有散落的 `.exists()` 检查，
会放过三类**静默错配**（产物看似正常，实则无效）：

1. 空/纯空白产物 —— 0 字节的 `测试方案.md` 满足 `exists()`；
2. 残缺产物 —— `requirements.json` 里 `requirements: []`，
   阶段 3 会在「没有需求」的前提下跑完；
3. **陈旧产物** —— 改了 PRD 后直接 `--stage 3`，用旧 plan 生成不匹配的用例。

`--stage N` / `--from-stage N` 的语义区分也是本模块的契约。
"""
import json
from pathlib import Path

import pytest

from prd2tc import artifacts
from prd2tc.artifacts import (ANALYSIS_MD, CASES_JSON, PLAN_MD, REQUIREMENTS_JSON,
                              check_inputs, check_one, format_problems, stage_plan,
                              stages_to_run)


def _write_run(tmp_path: Path, *, analysis="ok", requirements="ok", plan="ok",
               cases="ok", prd_hash="") -> Path:
    """构造一个运行目录；参数控制各产物状态（ok/empty/missing/incomplete/malformed）。"""
    (tmp_path / "analysis").mkdir(parents=True, exist_ok=True)
    (tmp_path / "plan").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cases" / "cases").mkdir(parents=True, exist_ok=True)

    def put(path: Path, state: str, good: str):
        if state == "missing":
            return
        if state == "empty":
            path.write_text("", encoding="utf-8")
            return
        if state == "whitespace":
            path.write_text("   \n\n", encoding="utf-8")
            return
        if state == "malformed":
            path.write_text("{ this is not json", encoding="utf-8")
            return
        if state == "incomplete":
            path.write_text(json.dumps({"requirements": []}), encoding="utf-8")
            return
        path.write_text(good, encoding="utf-8")

    put(tmp_path / "analysis" / "需求分析报告.md", analysis, "# 需求分析报告\n\n" + "REQ-A-001 登录\n" * 20)
    put(tmp_path / "analysis" / "requirements.json", requirements,
        json.dumps({"requirements": [{"id": "REQ-A-001", "module": "A", "title": "登录"}]},
                   ensure_ascii=False))
    put(tmp_path / "plan" / "测试方案.md", plan, "# 测试方案\n\n" + "范围与策略说明。\n" * 20)
    put(tmp_path / "cases" / "cases" / "A.json", cases,
        json.dumps([{"id": "TC-A-001", "module": "A", "title": "t",
                     "design_method": "场景法", "priority": "P1", "tags": ["@regression"],
                     "requirement": ["REQ-A-001"],
                     "steps": [{"action": "goto", "target": "/"},
                               {"action": "expectVisible", "target": "主体"}],
                     "expected": ["页面成功渲染并显示内容"]}], ensure_ascii=False))

    if prd_hash:
        (tmp_path / "manifest.json").write_text(
            json.dumps({"prdHash": prd_hash}, ensure_ascii=False), encoding="utf-8")
    return tmp_path


# ---------- 单产物完备性 ----------

def test_complete_run_has_no_problems(tmp_path):
    root = _write_run(tmp_path)
    for artifact in (ANALYSIS_MD, REQUIREMENTS_JSON, PLAN_MD, CASES_JSON):
        assert check_one(root, artifact) == [], artifact.key


def test_missing_artifact_is_reported_with_fix(tmp_path):
    root = _write_run(tmp_path, plan="missing")
    problems = check_one(root, PLAN_MD)
    assert len(problems) == 1
    p = problems[0]
    assert p.code == "missing"
    assert "--stage 2" in p.fix          # 建议必须可操作
    assert "测试方案" in str(p)


def test_empty_artifact_is_reported(tmp_path):
    """**核心回归**：0 字节产物满足 `exists()`，但不是有效产物。"""
    root = _write_run(tmp_path, plan="empty")
    problems = check_one(root, PLAN_MD)
    assert problems and problems[0].code == "empty", problems


def test_whitespace_only_artifact_is_reported(tmp_path):
    root = _write_run(tmp_path, plan="whitespace")
    assert check_one(root, PLAN_MD)[0].code == "empty"


def test_malformed_json_is_reported(tmp_path):
    root = _write_run(tmp_path, requirements="malformed")
    problems = check_one(root, REQUIREMENTS_JSON)
    assert problems and problems[0].code == "malformed", problems


def test_empty_requirements_list_is_reported(tmp_path):
    """**核心回归**：`requirements: []` 会让阶段 3 在「没有需求」下跑完。"""
    root = _write_run(tmp_path, requirements="incomplete")
    problems = check_one(root, REQUIREMENTS_JSON)
    assert problems and problems[0].code == "incomplete", problems
    assert "没有需求" in problems[0].detail


def test_missing_cases_glob_is_reported(tmp_path):
    root = _write_run(tmp_path, cases="missing")
    problems = check_one(root, CASES_JSON)
    assert problems and problems[0].code == "missing", problems


# ---------- 陈旧产物（最危险的一类） ----------

def test_stale_artifact_is_rejected_by_default(tmp_path):
    """**核心回归**：产物属于另一份 PRD 时必须拒绝，而不是静默复用。"""
    root = _write_run(tmp_path)
    problems = check_one(root, PLAN_MD, prd_hash="newhash111111",
                         recorded_hash="oldhash000000")
    assert problems and problems[0].code == "stale", problems
    assert "--stage all" in problems[0].fix
    assert "--allow-stale-artifacts" in problems[0].fix


def test_stale_artifact_allowed_with_opt_in(tmp_path):
    root = _write_run(tmp_path)
    assert check_one(root, PLAN_MD, prd_hash="newhash111111",
                     recorded_hash="oldhash000000", allow_stale=True) == []


def test_matching_hash_is_not_stale(tmp_path):
    root = _write_run(tmp_path)
    assert check_one(root, PLAN_MD, prd_hash="samehash", recorded_hash="samehash") == []


def test_no_recorded_hash_skips_staleness_check(tmp_path):
    """没有历史记录（首次运行）时不做新鲜度判断，避免误报。"""
    root = _write_run(tmp_path)
    assert check_one(root, PLAN_MD, prd_hash="whatever", recorded_hash="") == []


def test_recorded_hash_is_sampled_before_manifest_reset(tmp_path):
    """**核心回归**：陈旧检测必须用**重置清单之前**采样的哈希。

    `init_output`（`resume=False` 时调用）会重写清单，抹掉「产物属于哪份 PRD」。
    若在其后才读取，拿到的永远是刚写入的新哈希，检测形同虚设 ——
    实测「改了 PRD 直接 `--stage 3`」因此静默通过。
    """
    root = _write_run(tmp_path)
    (root / "manifest.json").write_text(
        json.dumps({"prdHash": "hash-of-original-prd"}, ensure_ascii=False), encoding="utf-8")
    sampled = artifacts.read_manifest_hash(root)
    assert sampled == "hash-of-original-prd"

    # 模拟 init_output 重写清单
    (root / "manifest.json").write_text(
        json.dumps({"prdHash": "hash-of-new-prd"}, ensure_ascii=False), encoding="utf-8")

    # 用**采样值**判定 → 能识别出陈旧
    problems = check_one(root, PLAN_MD, prd_hash="hash-of-new-prd", recorded_hash=sampled)
    assert problems and problems[0].code == "stale", problems
    # 用**重读值**判定 → 检测失效（这正是修复前的行为）
    assert check_one(root, PLAN_MD, prd_hash="hash-of-new-prd",
                     recorded_hash=artifacts.read_manifest_hash(root)) == []


def test_missing_artifact_takes_precedence_over_staleness(tmp_path):
    """产物缺失时报 missing，而不是 stale（否则修复建议会误导）。"""
    root = _write_run(tmp_path, plan="missing")
    problems = check_one(root, PLAN_MD, prd_hash="new", recorded_hash="old")
    assert problems[0].code == "missing"


# ---------- 阶段输入契约 ----------

def test_stage_inputs_match_dependency_graph():
    """阶段依赖必须与流水线实际顺序一致。"""
    assert artifacts.STAGE_INPUTS["1"] == ()
    assert set(artifacts.STAGE_INPUTS["2"]) == {ANALYSIS_MD, REQUIREMENTS_JSON}
    assert set(artifacts.STAGE_INPUTS["3"]) == {PLAN_MD, REQUIREMENTS_JSON}


def test_check_inputs_reports_all_missing(tmp_path):
    root = _write_run(tmp_path, analysis="missing", requirements="missing")
    problems = check_inputs(root, "2")
    assert {p.artifact.key for p in problems} == {"analysis_md", "requirements_json"}


def test_check_inputs_passes_for_complete_run(tmp_path):
    root = _write_run(tmp_path)
    assert check_inputs(root, "2") == []
    assert check_inputs(root, "3") == []


def test_format_problems_is_actionable(tmp_path):
    root = _write_run(tmp_path, plan="missing")
    text = format_problems(check_inputs(root, "3"), "3")
    assert "阶段 3" in text and "cases" in text      # 含语义名
    assert "测试方案" in text
    assert "--from-stage 2" in text                   # 给出**具体**补跑建议
    assert "--stage 3" in text                        # 与「只跑本阶段」区分
    assert "--allow-stale-artifacts" in text


# ---------- --stage / --from-stage 语义 ----------

def test_stage_only_runs_that_stage():
    assert stages_to_run("1") == ["1"]
    assert stages_to_run("3") == ["3"]


def test_stage_all_runs_everything():
    assert stages_to_run("all") == ["1", "2", "3"]


def test_from_stage_runs_to_the_end():
    """**核心增量**：`--from-stage N` 从 N 跑到最后（用户要的「从中间某步继续」）。"""
    assert stages_to_run("all", "1") == ["1", "2", "3"]
    assert stages_to_run("all", "2") == ["2", "3"]
    assert stages_to_run("all", "3") == ["3"]


def test_from_stage_with_invalid_value_yields_nothing():
    assert stages_to_run("all", "9") == []


def test_stage_plan_marks_reuse():
    assert stage_plan("all", "3") == [("1", "复用已有产物"), ("2", "复用已有产物"), ("3", "执行")]
    assert stage_plan("all") == [("1", "执行"), ("2", "执行"), ("3", "执行")]


# ---------- 端到端：从阶段 3 续跑 ----------

def test_resume_from_stage_3_reuses_upstream_artifacts(tmp_path):
    """**核心价值**：已有分析与方案时，只跑阶段 3 —— 不必重新生成 plan。"""
    from prd2tc.config import Config
    from prd2tc.pipeline import GenerateRequest, PipelineRunner

    prd = tmp_path / "PRD.md"
    prd.write_text("# 需求\n\nREQ-A-001 登录功能\n\n## 6.1 登录\n\n支持登录。\n", encoding="utf-8")
    out = tmp_path / "out"
    cfg = Config("k", "https://x", "m", 0.4, 10, 1)
    runner = PipelineRunner(cfg)

    # 先跑阶段 1 / 2
    runner.generate(GenerateRequest(prd=prd, out=out, product="P", version="v1",
                                    stub=True, jobs=1, stage="1"))
    runner.generate(GenerateRequest(prd=prd, out=out, product="P", version="v1",
                                    stub=True, jobs=1, stage="2", resume=True))
    root = out / "P" / "v1"
    assert (root / "plan" / "测试方案.md").is_file()
    plan_before = (root / "plan" / "测试方案.md").read_text(encoding="utf-8")

    # 记下方案 mtime，用于证明阶段 2 未被重跑
    import time
    mtime_before = (root / "plan" / "测试方案.md").stat().st_mtime
    time.sleep(0.05)

    # 只跑阶段 3
    result = runner.generate(GenerateRequest(prd=prd, out=out, product="P", version="v1",
                                             stub=True, jobs=1, stage="3", resume=True))
    assert (result.root / "cases" / "cases").is_dir()
    cases = list((result.root / "cases" / "cases").glob("*.json"))
    assert cases, "阶段 3 应产出用例"
    # 阶段 2 的产物未被重写
    assert (root / "plan" / "测试方案.md").read_text(encoding="utf-8") == plan_before
    assert (root / "plan" / "测试方案.md").stat().st_mtime == mtime_before


def test_stage_3_fails_loudly_when_plan_is_empty(tmp_path):
    """空方案文件必须**明确失败**，而不是产出无依据的用例。"""
    from prd2tc.config import Config
    from prd2tc.pipeline import GenerateRequest, PipelineRunner

    prd = tmp_path / "PRD.md"
    prd.write_text("# 需求\n\nREQ-A-001 登录\n", encoding="utf-8")
    out = tmp_path / "out"
    cfg = Config("k", "https://x", "m", 0.4, 10, 1)
    runner = PipelineRunner(cfg)
    runner.generate(GenerateRequest(prd=prd, out=out, product="P", version="v1",
                                    stub=True, jobs=1, stage="1"))
    root = out / "P" / "v1"
    (root / "plan").mkdir(parents=True, exist_ok=True)
    (root / "plan" / "测试方案.md").write_text("", encoding="utf-8")   # 空文件
    assert (root / "plan" / "测试方案.md").exists()                    # 满足 exists()

    with pytest.raises(Exception) as exc:
        runner.generate(GenerateRequest(prd=prd, out=out, product="P", version="v1",
                                        stub=True, jobs=1, stage="3", resume=True))
    detail = str(exc.value)
    assert "测试方案" in detail, detail


def test_stage_3_rejects_stale_plan_from_another_prd(tmp_path):
    """改了 PRD 后直接跑阶段 3 → 必须拒绝复用旧方案（静默错配的典型）。

    注意有**两层**防护，本用例覆盖第二层：
      1. `--resume` 会先比对 PRD 哈希，报「PRD 与历史运行不一致」；
      2. **不带 `--resume`** 时（`--stage 3` 的常见用法）没有第 1 层，
         此时由产物完备性检查用 `stale` 拦下 —— 否则会静默用旧方案生成新用例。
    """
    from prd2tc.config import Config
    from prd2tc.pipeline import GenerateRequest, PipelineRunner

    out = tmp_path / "out"
    cfg = Config("k", "https://x", "m", 0.4, 10, 1)
    runner = PipelineRunner(cfg)
    prd = tmp_path / "PRD.md"
    prd.write_text("# 需求\n\nREQ-A-001 登录\n", encoding="utf-8")
    runner.generate(GenerateRequest(prd=prd, out=out, product="P", version="v1",
                                    stub=True, jobs=1, stage="1"))
    runner.generate(GenerateRequest(prd=prd, out=out, product="P", version="v1",
                                    stub=True, jobs=1, stage="2", resume=True))

    # 换一份 PRD（标题与正文都变），**不带 --resume** 直接跑阶段 3
    prd.write_text("# 需求（已改）\n\nREQ-A-001 注册\n\n新增注册流程与校验。\n", encoding="utf-8")
    with pytest.raises(Exception) as exc:
        runner.generate(GenerateRequest(prd=prd, out=out, product="P", version="v1",
                                        stub=True, jobs=1, stage="3"))
    detail = str(exc.value)
    assert "另一份 PRD" in detail or "stale" in detail.lower(), detail

    # 显式放行后可以通过
    runner.generate(GenerateRequest(prd=prd, out=out, product="P", version="v1",
                                    stub=True, jobs=1, stage="3",
                                    allow_stale_artifacts=True))


def test_resume_flag_also_catches_prd_change(tmp_path):
    """第一层防护：`--resume` 时 PRD 变了要明确报错（不依赖产物检查）。"""
    from prd2tc.config import Config
    from prd2tc.pipeline import GenerateRequest, PipelineRunner

    out = tmp_path / "out"
    runner = PipelineRunner(Config("k", "https://x", "m", 0.4, 10, 1))
    prd = tmp_path / "PRD.md"
    prd.write_text("# 需求\n\nREQ-A-001 登录\n", encoding="utf-8")
    runner.generate(GenerateRequest(prd=prd, out=out, product="P", version="v1",
                                    stub=True, jobs=1, stage="1"))
    prd.write_text("# 需求（已改）\n\nREQ-A-001 注册\n", encoding="utf-8")
    with pytest.raises(Exception) as exc:
        runner.generate(GenerateRequest(prd=prd, out=out, product="P", version="v1",
                                        stub=True, jobs=1, stage="2", resume=True))
    assert "PRD 与历史运行不一致" in str(exc.value)


def test_manifest_name_constants_have_single_source_of_truth():
    """**核心回归**：清单文件名常量只能有**一处分发**，`artifacts` 与 `io` 必须一致。

    为什么必须锁：这两处曾**各自定义且语义相反** ——
    `io` 认为 `run-manifest.json` 是主、`manifest.json` 是兼容副本；
    `artifacts` 则完全反过来。因为读写两侧都会兜底尝试两个名字，
    当时没有功能影响，但「哪个是权威」的理解已经分叉：
    一旦有人只写其一（例如新增一条只写 MANIFEST_NAME 的路径），
    两侧就会指向不同文件，而这种错配不会有任何报错。

    修法是让 `artifacts` 直接复用 `io` 的常量（写入方为权威）。
    本测试同时锁住「值一致」与「语义方向」，防止改回去。
    """
    from prd2tc import artifacts, io
    assert artifacts.MANIFEST_NAME == io.MANIFEST_NAME
    assert artifacts.LEGACY_MANIFEST_NAME == io.LEGACY_MANIFEST_NAME
    # 语义固定：权威清单是 run-manifest.json（写入方 io 的约定）
    assert io.MANIFEST_NAME == "run-manifest.json"
    assert io.LEGACY_MANIFEST_NAME == "manifest.json"
