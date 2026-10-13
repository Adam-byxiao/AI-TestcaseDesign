"""长文档处理测试：程序化渲染 + 多分段 stub 端到端（防输出截断架构）。"""
from pathlib import Path

from prd2tc import stages
from prd2tc.config import Config
from prd2tc.llm import LLMResult
from prd2tc.stub import stub_chat


def _cfg(chunk_size: int = 12000) -> Config:
    return Config(
        api_key="k", api_base="https://x", model="m", temperature=0.4,
        timeout=10, max_retries=1, chunk_size=chunk_size, chunk_overlap=20,
        provider="openai", max_tokens=8192,
    )


def test_render_analysis_head_deterministic():
    modules = [{"id": "AUTH", "name": "账号", "entry": "/login", "requirementIds": ["REQ-AUTH-001", "REQ-AUTH-002"]}]
    reqs = [
        {"id": "REQ-AUTH-001", "module": "AUTH", "priority": "P0", "title": "登录", "description": "登录",
         "acceptanceCriteria": ["登录成功后跳转至首页"], "exceptionFlows": ["密码错误提示"]},
        {"id": "REQ-AUTH-002", "module": "AUTH", "priority": "P1", "title": "注册", "description": "注册",
         "acceptanceCriteria": [], "exceptionFlows": []},
    ]
    md = stages.render_analysis_head(modules, reqs)
    assert "## 1. 功能模块清单" in md
    assert "## 3. 需求点明细" in md
    assert "| AUTH | 账号 | /login | REQ-AUTH-001 ～ REQ-AUTH-002 |" in md
    assert "REQ-AUTH-001" in md and "REQ-AUTH-002" in md
    assert "待确认" in md  # 空验收标准兜底


def test_multichunk_stage1_stub_no_truncation():
    """多分段路径（stub）产出六章齐全的报告，且第 1/3 章为确定性渲染。"""
    prd = "# PRD 测试文档\n\n" + "需求描述内容。" * 60  # 超过 chunk_size=200 → 多分段
    content, result, errors = stages.generate_stage_1(
        _cfg(chunk_size=200), prd, llm=stub_chat, jobs=2,
        use_gate=True, max_gate_retries=1, reporter=lambda *_: None,
    )
    assert errors == []
    for section in ("## 1. 功能模块清单", "## 2. 角色清单", "## 3. 需求点明细",
                    "## 4. 业务规则", "## 5. 非功能需求", "## 6. 风险与疑点"):
        assert section in content, f"缺少章节 {section}"
    # 第 1/3 章来自程序化渲染，不依赖 LLM 长输出
    assert "| 模块代号 | 模块名 | 页面/入口 | 需求点编号 |" in content


def test_single_chunk_path_unchanged():
    """小文档仍走单次生成路径（stub），六章齐全。"""
    prd = "# PRD 测试文档\n\n账号登录功能。"
    content, result, errors = stages.generate_stage_1(
        _cfg(), prd, llm=stub_chat, jobs=1,
        use_gate=True, max_gate_retries=1, reporter=lambda *_: None,
    )
    assert errors == []
    assert all(s in content for s in ("## 1. 功能模块清单", "## 6. 风险与疑点"))


def test_sequential_requirement_ids():
    """生成需求应按模块内顺序编号（REQ-*-001/002…），而非哈希三位数。"""
    md = ("# 需求分析报告\n\n## 3. 需求点明细\n"
          "| REQ 编号 | 优先级 | 需求描述 | 验收标准 | 异常分支/边界 |\n"
          "| REQ-AUTH-999 | P0 | 登录 | 跳转首页 | - |\n"
          "| REQ-AUTH-888 | P1 | 注册 | 注册成功 | - |\n")
    reqs = stages.requirements_from_analysis(md, "无编号 PRD")
    ids = [r["id"] for r in reqs if r["module"] == "AUTH"]
    assert ids == ["REQ-AUTH-001", "REQ-AUTH-002"], ids


def test_prd_module_prefixes():
    """从 PRD 原始编号提取模块码前缀（GLB/CHK/DET…）。"""
    prd = "# PRD\n| GLB-001 | 顶栏 |\n| CHK-001 | 结算 |\n| DET-001 | 详情 |\n无编号模块"
    assert stages.prd_module_prefixes(prd) == {"GLB", "CHK", "DET"}


def test_prd_section_aliases():
    """节标题代号与编号前缀不一致时（CHECKOUT→CHK、ORDER→ORD）应提取别名。"""
    prd = ("### 6.7 结算页（CHECKOUT）\n| CHK-001 | P0 | 路由守卫 |\n| CHK-002 | P0 | 收货地址 |\n"
           "### 6.8 订单成功页（ORDER）\n| ORD-001 | P0 | 订单号 |\n")
    assert stages.prd_section_aliases(prd) == {"CHECKOUT": "CHK", "ORDER": "ORD"}


def test_module_anchor_section_alias():
    """无「原编号」引用、仅节标题代号与编号前缀不一致时，模块码仍应归一化（CHECKOUT→CHK）。"""
    md = ("# 需求分析报告\n\n## 3. 需求点明细\n"
          "| REQ 编号 | 优先级 | 需求描述 | 验收标准 | 异常分支/边界 |\n"
          "| REQ-CHECKOUT-001 | P0 | 结算页加载地址 | 归属当前用户 | - |\n")
    prd = "### 6.7 结算页（CHECKOUT）\n| CHK-001 | P0 | 结算页加载地址 |\n"
    reqs = stages.requirements_from_analysis(md, prd)
    assert [r["module"] for r in reqs] == ["CHK"]
    assert [r["id"] for r in reqs] == ["REQ-CHK-001"]


def test_module_anchor_from_prd_prefix():
    """需求文本引用 PRD 原始编号（原 GLB-001）时，模块码应锚定为 GLB 而非模型自拟的 COMMON。"""
    md = ("# 需求分析报告\n\n## 3. 需求点明细\n"
          "| REQ 编号 | 优先级 | 需求描述 | 验收标准 | 异常分支/边界 |\n"
          "| REQ-COMMON-001 | P0 | 顶栏 Header（原 GLB-001） | 全页面可见 | - |\n"
          "| REQ-COMMON-002 | P1 | 页脚 Footer（原 GLB-003） | 底部展示 | - |\n")
    prd = "# PRD\n| GLB-001 | P0 | 顶栏 Header |\n| GLB-003 | P1 | 页脚 Footer |\n"
    reqs = stages.requirements_from_analysis(md, prd)
    assert {r["module"] for r in reqs} == {"GLB"}
    assert [r["id"] for r in reqs] == ["REQ-GLB-001", "REQ-GLB-002"]


def test_normalize_step_targets():
    cases = [{"id": "TC-AUTH-001", "steps": [
        {"action": "reload"}, {"action": "waitFor"}, {"action": "click", "target": "按钮"}]}]
    stages.normalize_step_targets(cases)
    assert cases[0]["steps"][0]["target"] == "当前页面"
    assert cases[0]["steps"][1]["target"] == "页面就绪"
    assert cases[0]["steps"][2]["target"] == "按钮"


def test_module_batching_and_renumber():
    """40 条需求 → 按输出预算自适应多批 → 合并重编号 TC-PROD-001/002…（与批次容量默认值解耦）。"""
    prd = "# PRD\n" + "需求。" * 100
    reqs = [{"id": f"REQ-PROD-{i:03d}", "module": "PROD", "title": f"需求{i}"} for i in range(1, 41)]
    cases, result = stages.run_stage_3_modules(
        _cfg(), "方案", reqs, prd, llm=stub_chat, jobs=1,
        use_gate=True, max_gate_retries=1, reporter=lambda *_: None,
    )
    ids = [c["id"] for c in cases]
    assert len(cases) >= 2, f"40 需求应产生 ≥2 批（stub 每批 1 条），实际 {len(cases)}"
    assert ids == [f"TC-PROD-{i:03d}" for i in range(1, len(cases) + 1)], ids
    # 模块码 / id 前缀 / requirement 前缀必须三者一致（消除「不匹配」）
    for c in cases:
        assert c["module"] == "PROD"
        assert c["id"].startswith("TC-PROD-")
        assert all(str(r).startswith("REQ-PROD-") for r in c["requirement"])


def test_module_batch_size_adaptive():
    """P3：批次容量按输出预算自适应；小模块单批全量、大模块按预算分批。

    期望值由模块自身的 TOKENS_PER_REQ_EST 推导，避免调参后测试与实现脱节。
    max_tokens=8192 → budget = int(8192*0.85) = 6963。
    """
    cfg = _cfg()
    budget = int(cfg.max_tokens * stages.OUTPUT_BUDGET_RATIO)
    expected = max(1, min(budget // stages.TOKENS_PER_REQ_EST, stages.CASE_BATCH_CAP))
    assert stages._module_batch_size(cfg, 40) == expected
    assert stages._module_batch_size(cfg, 1) == 1
    # 需求数少于单批容量时，整模块一批（单模块全量）
    assert stages._module_batch_size(cfg, min(expected, 10)) == min(expected, 10)


def test_tokens_per_req_estimate_is_calibrated():
    """估算值必须与实测相符：唯一完整跑完的 KB 模块是 1700 tok/需求，被截断模块 ≥2731。

    取 450（旧值）会让 6~7 条需求的模块只分 1 批，单次输出必然撞满 max_tokens。
    """
    assert stages.TOKENS_PER_REQ_EST >= 1500, (
        "估算低于实测（1700 tok/需求）会导致批次过大："
        f"当前 {stages.TOKENS_PER_REQ_EST}"
    )
    cfg = _cfg()
    # 6~7 条需求的模块（AGENT/SCENE 的规模）必须拆成 ≥2 批
    for count in (6, 7):
        size = stages._module_batch_size(cfg, count)
        assert size < count, f"{count} 条需求只分了 1 批（每批 {size}），截断风险高"


def test_raising_max_tokens_actually_buys_headroom():
    """**关键不变量**：调高 max_tokens 必须真正换来输出余量，而不是被批次膨胀抵消。

    批次容量与 max_tokens 成正比（预算 = max_tokens × OUTPUT_BUDGET_RATIO）。
    若 OUTPUT_BUDGET_RATIO 太高（如 0.85），调高 max_tokens 只会让每批容纳更多需求，
    安全余量恒为 1/0.85≈1.18x，截断率毫无改善 —— 这正是「改了 max_tokens 却没用」的陷阱。
    """
    assert stages.OUTPUT_BUDGET_RATIO <= 0.70, (
        f"预算占用比 {stages.OUTPUT_BUDGET_RATIO} 过高：调高 max_tokens 会被批次膨胀抵消"
    )
    cfg = _cfg()
    size = stages._module_batch_size(cfg, 6)
    expected_output = size * stages.TOKENS_PER_REQ_EST
    margin = cfg.max_tokens / expected_output
    assert margin >= 1.5, (
        f"单批余量仅 {margin:.2f}x，不足以吸收估算偏差（实测偏差可达 1.5 倍）"
    )


def test_batch_size_bounded_by_cap():
    """批次不得随预算无限膨胀：CASE_BATCH_CAP 必须真正生效。"""
    cfg = _cfg()
    # 构造一个极大预算，确认 cap 仍能压住批次
    huge = Config(**{**cfg.__dict__, "max_tokens": 200000})
    assert stages._module_batch_size(huge, 500) == stages.CASE_BATCH_CAP


def _base_cfg(**over) -> Config:
    """构造测试配置（默认 max_tokens=8192，用于覆盖非默认上限的场景）。"""
    base = dict(api_key="k", api_base="https://x", model="m", temperature=0.4,
                timeout=10, max_retries=1, chunk_size=12000, chunk_overlap=20,
                provider="openai", max_tokens=8192)
    base.update(over)
    return Config(**base)


def test_max_tokens_recorded_in_manifest(tmp_path):
    """max_tokens 决定输出是否被截断，必须记入 manifest 供审计。"""
    import json as _json
    from prd2tc.pipeline import GenerateRequest, PipelineRunner
    sample = Path(__file__).parents[1] / "examples" / "sample-prd.md"
    cfg = _base_cfg(max_tokens=16384)
    res = PipelineRunner(cfg).generate(
        GenerateRequest(prd=sample, out=tmp_path / "o", product="p", version="v1", stub=True))
    manifest = _json.loads((res.root / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest["parameters"].get("maxTokens") == 16384


def test_max_tokens_change_invalidates_resume(tmp_path):
    """max_tokens 变化会改变 configHash → --resume 不得复用旧检查点。

    否则会复用「在上限更小时生成的」检查点，那些内容可能本身就不完整。
    """
    import json as _json
    from prd2tc.pipeline import GenerateRequest, PipelineRunner
    sample = Path(__file__).parents[1] / "examples" / "sample-prd.md"
    base = dict(prd=sample, out=tmp_path / "o", product="p", version="v1", stub=True)
    big = _base_cfg(max_tokens=16384)
    small = _base_cfg(max_tokens=8192)

    first = PipelineRunner(big).generate(GenerateRequest(**base))
    hash_big = _json.loads((first.root / "run-state.json").read_text(encoding="utf-8"))["configHash"]

    same = PipelineRunner(big).generate(GenerateRequest(**base, resume=True))
    modules_same = _json.loads((same.root / "run-state.json").read_text(encoding="utf-8"))["modules"]
    assert all(v == "skipped" for v in modules_same.values()), (
        f"同配置 --resume 应复用检查点，实际 {modules_same}"
    )

    changed = PipelineRunner(small).generate(GenerateRequest(**base, resume=True))
    state = _json.loads((changed.root / "run-state.json").read_text(encoding="utf-8"))
    assert state["configHash"] != hash_big
    assert any(v == "succeeded" for v in state["modules"].values()), (
        f"max_tokens 变化后不应复用旧检查点，实际 {state['modules']}"
    )


def test_module_batch_size_override(monkeypatch):
    """PRD2TC_CASE_BATCH_SIZE 硬覆盖仍生效（旧参数兼容）。"""
    monkeypatch.setenv("PRD2TC_CASE_BATCH_SIZE", "5")
    try:
        assert stages._module_batch_size(_cfg(), 40) == 5
    finally:
        monkeypatch.delenv("PRD2TC_CASE_BATCH_SIZE")


def test_module_context_slimmed():
    """P0：module_context 只携带相关片段，不再把整份 PRD 塞进 user 消息。"""
    captured = {}
    prd = "# PRD 标题\n\n" + "\n\n".join(
        f"## 功能段 {i}\n需求描述文本 AUTH-{i:03d} 登录校验场景内容。" for i in range(500))
    assert len(prd) > 15000  # 足够大，旧实现会把整份 PRD 送入上下文

    def fake_llm(cfg, user_text, **kwargs):
        captured["user"] = user_text
        return LLMResult("```json\n[{\"id\":\"TC-AUTH-001\",\"module\":\"AUTH\",\"title\":\"登录\","
                         "\"design_method\":\"场景法\",\"priority\":\"P1\",\"tags\":[\"@regression\"],\"requirement\":[\"REQ-AUTH-001\"],"
                         "\"steps\":[{\"action\":\"goto\",\"target\":\"/\"},{\"action\":\"expectVisible\",\"target\":\"页面主体\"}],\"expected\":[\"页面成功渲染并显示预期内容\"]}]\n```", "fake")

    reqs = [{"id": "REQ-AUTH-001", "module": "AUTH", "title": "登录"}]
    stages.run_stage_3_modules(
        _cfg(chunk_size=1000), "方案", reqs, prd, llm=fake_llm,
        use_gate=False, reporter=lambda *_: None,
    )
    user = captured["user"]
    assert len(user) < len(prd), f"module_context 不应携带整份 PRD（user={len(user)} >= prd={len(prd)}）"
    assert len(user) < 8000, f"上下文应被裁剪到 ~2 段（实际 {len(user)} 字符）"


def test_compact_analysis_replaces_big_table():
    md = ("# 需求分析报告\n\n## 1. 功能模块清单\n| AUTH | 账号 |\n"
          "## 3. 需求点明细\n| REQ 编号 | 优先级 | 需求描述 | 验收标准 | 异常 |\n"
          "| REQ-AUTH-001 | P0 | 登录 | 跳转首页 | - |\n"
          "## 4. 业务规则\n| 编号 | 规则 |\n")
    reqs = [{"id": "REQ-AUTH-001", "module": "AUTH", "priority": "P0", "title": "登录"}]
    out = stages.compact_analysis(md, reqs)
    assert "REQ-AUTH-001 [P0] 登录" in out
    assert "登录成功后跳转至首页" not in out  # 大表已替换


def test_compact_partial_strips_sections():
    md = ("## 1. 功能模块清单\n| AUTH | 账号 |\n"
          "## 2. 角色清单\n| 用户 | 说明 |\n"
          "## 3. 需求点明细\n| REQ 编号 | ... |\n"
          "## 4. 业务规则\n| 编号 | 规则 |\n")
    out = stages._compact_partial(md)
    assert "## 1. 功能模块清单" not in out and "## 3. 需求点明细" not in out
    assert "## 2. 角色清单" in out and "## 4. 业务规则" in out


def test_generate_with_feedback_repair_path():
    """增量修正：重试只回传失败输出+错误，不重发大输入（BIG_PLAN）。"""
    responses = [
        LLMResult(content='```json\n[{"id":"TC-AUTH-001","module":"AUTH","title":"t","design_method": "场景法", "priority":"P1",'
                          '"tags":["@regression"],"requirement":["REQ-AUTH-001"],'
                          '"steps":[{"action":"click"},{"action":"expectVisible","target":"结果区"}],"expected":["页面成功渲染并显示预期内容"]}]\n```', model="fake"),
        LLMResult(content='```json\n[{"id":"TC-AUTH-001","module":"AUTH","title":"t","design_method": "场景法", "priority":"P1",'
                          '"tags":["@regression"],"requirement":["REQ-AUTH-001"],'
                          '"steps":[{"action":"click","target":"按钮"},{"action":"expectVisible","target":"结果区"}],"expected":["页面成功渲染并显示预期内容"]}]\n```', model="fake"),
    ]

    class Fake:
        def __init__(self):
            self.calls = []

        def __call__(self, cfg, user, **kwargs):
            self.calls.append(user)
            return responses[min(len(self.calls) - 1, len(responses) - 1)]

    fake = Fake()
    from prd2tc import gates
    repair = stages.stage3_repair_builder({"REQ-AUTH-001"}, "AUTH")
    content, result, errors = stages.generate_with_feedback(
        _cfg(),
        lambda fb: "BIG_PLAN_CONTENT" + (("\n" + fb) if fb else ""),
        lambda v: gates.check_cases_text(v, {"AUTH"}),
        llm=fake, max_retries=1, label="t", repair_builder=repair, reporter=lambda *_: None,
    )
    assert errors == []
    assert len(fake.calls) == 2
    assert "BIG_PLAN_CONTENT" in fake.calls[0]
    assert "BIG_PLAN_CONTENT" not in fake.calls[1], "增量修正不应重发大输入"
    assert "上一次输出" in fake.calls[1] and "错误清单" in fake.calls[1]
    assert "REQ-AUTH-001" in fake.calls[1]


def test_generate_with_feedback_accumulates_cache_tokens():
    """门禁重试多轮时，cache_read/cache_creation 应跨轮累积（与 prompt/completion 一致）。"""
    calls = {"n": 0}

    def fake(cfg, user, **kwargs):
        calls["n"] += 1
        bad = ("```json\n[{\"id\":\"TC-AUTH-001\",\"module\":\"AUTH\",\"title\":\"t\",\"design_method\":\"场景法\",\"priority\":\"P1\","
               "\"tags\":[\"@regression\"],\"requirement\":[\"REQ-AUTH-001\"],"
               "\"steps\":[{\"action\":\"click\"}],\"expected\":[]}]\n```")
        good = ("```json\n[{\"id\":\"TC-AUTH-001\",\"module\":\"AUTH\",\"title\":\"t\",\"design_method\":\"场景法\",\"priority\":\"P1\","
                "\"tags\":[\"@regression\"],\"requirement\":[\"REQ-AUTH-001\"],"
                "\"steps\":[{\"action\":\"click\",\"target\":\"按钮\"},{\"action\":\"expectVisible\",\"target\":\"结果区\"}],\"expected\":[\"页面成功渲染并显示预期内容\"]}]\n```")
        cache_read = 0 if calls["n"] == 1 else 500  # 第 2 轮命中缓存
        return LLMResult(good if calls["n"] > 1 else bad, "fake", 100, 50,
                         cache_read_input_tokens=cache_read, cache_creation_input_tokens=200)

    from prd2tc import gates
    repair = stages.stage3_repair_builder({"REQ-AUTH-001"}, "AUTH")
    _, result, errors = stages.generate_with_feedback(
        _cfg(), lambda fb: "ctx", lambda v: gates.check_cases_text(v, {"AUTH"}),
        llm=fake, max_retries=1, label="t", repair_builder=repair, reporter=lambda *_: None,
    )
    assert errors == []
    assert result.prompt_tokens == 200  # 100 × 2 轮
    assert result.completion_tokens == 100
    assert result.cache_read_input_tokens == 500  # 仅第 2 轮命中
    assert result.cache_creation_input_tokens == 400  # 200 × 2 轮
