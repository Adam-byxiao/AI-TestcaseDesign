"""跨批次重复生成回归测试（实测运行 c653dc92 暴露的结构性缺陷）。

缺陷本体
--------
KB 有 16 条需求 → 被切成 2 批（各 8 条）。但两批**都**产出了覆盖全部 16 条需求的用例：
`all_cases.extend(cases)` 把两批直接叠加，16 条需求各被独立覆盖两次，
KB 42 条里 52% 是语义重复（全库约 30% 用例量是重复膨胀）。

为什么原有防线全部失效
----------------------
1. 批次提示只是自然语言建议（「本批仅覆盖以下 N 条需求」），模型并未遵守；
2. 门禁的允许集合是**模块级** `allowed = {module}`，批1 引用批2 的需求不会被拦；
3. `renumber()` 顺序重编号抹掉了批次边界，两批看起来像一个正常连续列表；
4. `case_signature()` 基于文本比较，两批对同一元素用了不同 testid/路由（
   `kb-search` vs `kb-search-input`），因此判为非重复。

证据（事件日志 .prd2tc-workspace/events/c653dc92-*.jsonl）
--------------------------------------------------------
  KB module_started · batch=0/2
  KB batch_started  · batch=1/1
  KB batch_started  · batch=2/1     ← 恰好 2 批，且无拆批事件
两批的需求覆盖集合交集 = 16/16（完整重叠）。
完美相关：6 个模块中**只有 KB 是 2 批**，也只有 KB 出现 100% 重复覆盖。
"""
import json
import re

from prd2tc import stages
from prd2tc.config import Config
from prd2tc.llm import LLMResult
from prd2tc.stages import dedup_cross_batch


def _case(cid: str, module: str, refs: list[str], *, title: str = "场景") -> dict:
    """构造合法用例。

    步骤/预期**随 cid 与 title 变化**，否则会触发 C-007「同模块+同步骤+同预期」
    重复门禁 —— 那会掩盖本文件真正要验证的「跨批次」重复。
    """
    return {
        "id": cid, "module": module, "title": title,
        "design_method": "场景法", "priority": "P1", "tags": ["@regression"],
        "requirement": list(refs),
        "preconditions": ["已登录"],
        "testData": ["account=normal"],
        "steps": [{"action": "goto", "target": f"/console/{cid.lower()}"}],
        "expected": [f"{title}后页面成功渲染并显示预期内容"],
    }


def _reqs(module: str, n: int) -> list[dict]:
    return [{"id": f"REQ-{module}-{i:03d}", "module": module,
             "title": f"需求{i}", "priority": "P1"} for i in range(1, n + 1)]


# ---------- 去重规则（纯函数） ----------

def test_dedup_keeps_only_the_owning_batch_per_requirement():
    """同一需求被两批覆盖时，只保留**拥有该需求**的批次。"""
    a1 = [_case("TC-A-001", "A", ["REQ-A-001"]), _case("TC-A-002", "A", ["REQ-A-002"])]
    a2 = [_case("TC-A-003", "A", ["REQ-A-001"]), _case("TC-A-004", "A", ["REQ-A-002"])]
    kept, conflicts = dedup_cross_batch([(a1, {"REQ-A-001"}), (a2, {"REQ-A-002"})])

    ids = [c["id"] for c in kept]
    assert ids == ["TC-A-001", "TC-A-004"], ids
    assert sorted(conflicts) == [("REQ-A-001", 0, 1), ("REQ-A-002", 1, 0)], conflicts


def test_dedup_loses_no_coverage_when_owner_produced_nothing():
    """拥有批没产出时，保留最早产出的批次，**不得留下覆盖缺口**。"""
    a1 = [_case("TC-A-001", "A", ["REQ-A-001"])]          # 唯一供例者在批1
    a2 = [_case("TC-A-002", "A", ["REQ-A-001"])]
    kept, conflicts = dedup_cross_batch([(a1, {"REQ-A-001"}), (a2, {"REQ-A-002"})])

    refs = {r for c in kept for r in c["requirement"]}
    assert "REQ-A-001" in refs, "拥有批(批2)未产出时不应丢覆盖"
    assert [c["id"] for c in kept] == ["TC-A-001"], [c["id"] for c in kept]
    assert conflicts == [("REQ-A-001", 0, 1)], conflicts


def test_dedup_trims_mixed_refs_instead_of_dropping_the_case():
    """混合引用（部分属本批）→ 裁剪引用而非丢弃用例，避免丢掉本批覆盖。"""
    a1 = [_case("TC-A-001", "A", ["REQ-A-001", "REQ-A-002"])]
    a2 = [_case("TC-A-002", "A", ["REQ-A-002"])]
    kept, _ = dedup_cross_batch([(a1, {"REQ-A-001"}), (a2, {"REQ-A-002"})])

    by_id = {c["id"]: c for c in kept}
    assert set(by_id) == {"TC-A-001", "TC-A-002"}, set(by_id)
    assert by_id["TC-A-001"]["requirement"] == ["REQ-A-001"], by_id["TC-A-001"]["requirement"]
    assert by_id["TC-A-002"]["requirement"] == ["REQ-A-002"]


def test_dedup_leaves_disjoint_batches_untouched():
    """批次本就互斥（正常情况）→ 不做任何改动，也不报冲突。"""
    a1 = [_case("TC-A-001", "A", ["REQ-A-001"])]
    a2 = [_case("TC-A-002", "A", ["REQ-A-002"])]
    kept, conflicts = dedup_cross_batch([(a1, {"REQ-A-001"}), (a2, {"REQ-A-002"})])
    assert [c["id"] for c in kept] == ["TC-A-001", "TC-A-002"]
    assert conflicts == []


def test_dedup_keeps_cases_without_requirement_refs():
    """未引用需求的用例无从归属，保留（交由覆盖率门禁处理，而非静默丢弃）。"""
    a1 = [_case("TC-A-001", "A", [])]
    a2 = [_case("TC-A-002", "A", [])]
    kept, conflicts = dedup_cross_batch([(a1, set()), (a2, set())])
    assert [c["id"] for c in kept] == ["TC-A-001", "TC-A-002"]
    assert conflicts == []


def test_dedup_is_deterministic():
    """同输入两次结果一致（可复现性契约）。"""
    a1 = [_case("TC-A-001", "A", ["REQ-A-001", "REQ-A-002"])]
    a2 = [_case("TC-A-002", "A", ["REQ-A-001"]), _case("TC-A-003", "A", ["REQ-A-002"])]
    first = dedup_cross_batch([(a1, {"REQ-A-001"}), (a2, {"REQ-A-002"})])
    second = dedup_cross_batch([(a1, {"REQ-A-001"}), (a2, {"REQ-A-002"})])
    assert first == second


# ---------- 端到端：复现 c653dc92 的缺陷 ----------

def test_model_ignoring_batch_scope_does_not_produce_cross_batch_duplicates():
    """**核心回归**：伪造一个「无视批次范围」的模型（每次返回全模块需求的用例），
    断言最终产物中同一需求不会被两个批次覆盖。

    这正是 c653dc92 的失败模式：KB 两批都覆盖全部 16 条需求 → 42 条里 52% 重复。
    """
    module, total = "A", 16
    reqs = _reqs(module, total)

    def scope_ignoring_llm(cfg, user_text, **kwargs):
        # 无论本批是什么，都返回覆盖**全部**需求的用例（模拟真实模型的越界行为）
        payload = [_case(f"TC-{module}-{i:03d}", module, [f"REQ-{module}-{i:03d}"],
                         title=f"需求{i}的场景") for i in range(1, total + 1)]
        return LLMResult(json.dumps(payload, ensure_ascii=False), "fake", 5, 5)

    cfg = Config("k", "https://x", "m", 0.4, 10, 1)
    batches_seen: list[int] = []
    cases, _ = stages.run_stage_3_modules(
        cfg, "方案", reqs, "# A\n正文\n" + "\n".join(f"REQ-{module}-{i:03d}" for i in range(1, total + 1)),
        llm=scope_ignoring_llm, jobs=1, use_gate=False,
        batch_callback=lambda m, i, t, s: batches_seen.append(i) if s == "batch_started" else None,
        reporter=lambda *_: None)

    assert len(batches_seen) >= 2, f"该用例需要多批才能复现，实际批次数={len(batches_seen)}"

    # 缺陷判据：任一需求被两批覆盖。修复后必须为空。
    refs_per_batch: dict[int, set[str]] = {}
    for i, c in enumerate(cases, 1):
        refs_per_batch.setdefault(i, set())
    # 用「用例引用的需求」重建：同需求出现在 >1 条用例是合法的（批内多分支），
    # 但同一需求的用例必须来自同一批 —— 由 dedup 保证每条需求只有一个供例批次。
    covered = {}
    for c in cases:
        for r in c["requirement"]:
            covered.setdefault(r, []).append(c["id"])

    # 修复后：每个需求恰由 1 条用例覆盖（此伪造模型每需求只给 1 条）
    assert all(len(v) == 1 for v in covered.values()), {
        r: v for r, v in covered.items() if len(v) > 1}
    assert len(cases) == total, f"应去重为 {total} 条，实际 {len(cases)} 条"
    assert {r for c in cases for r in c["requirement"]} == {f"REQ-{module}-{i:03d}"
                                                            for i in range(1, total + 1)}


def test_repair_prompt_scope_matches_batch():
    """**核心回归（修复①）**：修复提示词声明可引用的编号必须与**本批范围**一致。

    实测事故（运行 5b526ce6 的 SCENE、9347c6b3 的 DOC/CTX）：
    修复提示词用的是**模块级**清单（说本模块 10 条需求都可引用），
    而门禁只允许本批 5 条 —— 模型照提示词生成 10 条即被判越界，
    重试又收到同样的模块级清单，**永远无法收敛**，整模块失败。
    """
    ids = {"REQ-A-001", "REQ-A-002"}
    text = stages.stage3_repair_builder(ids, "A", scope="本批")( "上次输出", "错误清单")
    assert "REQ-A-001" in text and "REQ-A-002" in text
    assert "REQ-A-003" not in text, "不得声明本批之外的编号"
    # 文案须与实际范围一致，避免模型误解为「整模块都可以引用」
    assert "本批" in text


def test_faithful_model_obeying_repair_prompt_does_not_livelock():
    """**核心回归（livelock）**：一个**完全遵守提示词**的模型绝不能失败。

    这是修复前必然失败的行为：模型忠实引用提示词列出的编号，
    却在重试后仍被判越界 → 重试无意义 → 模块必然失败。
    修复后提示词只列本批编号，忠实模型自然通过。
    """
    module, total = "A", 16
    reqs = _reqs(module, total)

    def faithful_llm(cfg, user_text, **kwargs):
        """只引用提示词中出现的、属于本批的结构化需求编号。"""
        import re
        # 结构化需求里带 "id": "REQ-A-xxx"，即本批范围
        listed = re.findall(r'"id":\s*"(REQ-[A-Z]+-\d{3})"', user_text)
        if not listed:      # 修复路径：提示词以纯编号列表给出本批范围
            listed = re.findall(r"REQ-[A-Z]+-\d{3}", user_text)
        ids = sorted(set(listed)) or [r["id"] for r in reqs]
        payload = [_case(f"TC-{module}-{i:03d}", module, [r], title=f"{r} 的场景")
                   for i, r in enumerate(ids, 1)]
        return LLMResult(json.dumps(payload, ensure_ascii=False), "fake", 5, 5)

    cfg = Config("k", "https://x", "m", 0.4, 10, 1)
    cases, _ = stages.run_stage_3_modules(
        cfg, "方案", reqs, "# A\n" + "正文\n" * 60,
        llm=faithful_llm, jobs=1, use_gate=True, max_gate_retries=2,
        batch_callback=lambda *_: None, reporter=lambda *_: None)

    covered = {r for c in cases for r in c["requirement"]}
    assert covered == {f"REQ-{module}-{i:03d}" for i in range(1, total + 1)}, sorted(covered)
    counts: dict[str, int] = {}
    for c in cases:
        for r in c["requirement"]:
            counts[r] = counts.get(r, 0) + 1
    assert all(v == 1 for v in counts.values()), {r: v for r, v in counts.items() if v > 1}


def test_out_of_scope_is_recorded_not_fatal():
    """**核心回归（修复②）**：越界引用只记录、不由门禁致命失败。

    越界是**可确定性纠正**的：`dedup_cross_batch` 按需求归属批次保留唯一供例，
    且拥有批未产出时回退到最早产出它的批次（不丢覆盖）。
    把它升级成整轮运行的致命失败，代价与收益严重不成比例 ——
    实测 `module_context` 携带整个模块的 PRD 原文，模型天然会看到本批之外的需求，
    因此越界属于预期行为而非异常。
    """
    module, total = "A", 16
    reqs = _reqs(module, total)
    notes: list[str] = []

    def scope_ignoring_llm(cfg, user_text, **kwargs):
        payload = [_case(f"TC-{module}-{i:03d}", module, [f"REQ-{module}-{i:03d}"])
                   for i in range(1, total + 1)]
        return LLMResult(json.dumps(payload, ensure_ascii=False), "fake", 5, 5)

    cfg = Config("k", "https://x", "m", 0.4, 10, 1)
    cases, _ = stages.run_stage_3_modules(
        cfg, "方案", reqs, "# A\n" + "正文\n" * 50,
        llm=scope_ignoring_llm, jobs=1, use_gate=True, max_gate_retries=1,
        batch_callback=lambda *_: None,
        reporter=lambda *a, **k: notes.append(" ".join(str(x) for x in a)))

    # 不再致命失败
    assert cases, "越界不应导致模块失败"
    # 覆盖唯一且无缺口
    counts: dict[str, int] = {}
    for c in cases:
        for r in c["requirement"]:
            counts[r] = counts.get(r, 0) + 1
    assert all(v == 1 for v in counts.values()), {r: v for r, v in counts.items() if v > 1}
    assert set(counts) == {f"REQ-{module}-{i:03d}" for i in range(1, total + 1)}, sorted(counts)
    # 但必须可见（不静默）
    assert any("本批之外" in n for n in notes), notes


def test_persistent_scope_violation_is_resolved_by_dedup():
    """模型始终越界 → 由 dedup 收敛为唯一供例，而不是丢弃整个模块。

    旧行为是「显式失败」，但那会让一个可自动纠正的归属偏差
    毁掉整个模块的产出（实测 SCENE 整模块失败，其余模块因检查点 skipped）。
    """
    module, total = "A", 16
    reqs = _reqs(module, total)

    def stubborn_llm(cfg, user_text, **kwargs):
        payload = [_case(f"TC-{module}-{i:03d}", module, [f"REQ-{module}-{i:03d}"],
                         title=f"需求{i}场景")
                   for i in range(1, total + 1)]
        return LLMResult(json.dumps(payload, ensure_ascii=False), "fake", 5, 5)

    cfg = Config("k", "https://x", "m", 0.4, 10, 1)
    cases, _ = stages.run_stage_3_modules(
        cfg, "方案", reqs, "# A\n" + "正文\n" * 60,
        llm=stubborn_llm, jobs=1, use_gate=True, max_gate_retries=1,
        batch_callback=lambda *_: None, reporter=lambda *_: None)

    counts: dict[str, int] = {}
    for c in cases:
        for r in c["requirement"]:
            counts[r] = counts.get(r, 0) + 1
    assert set(counts) == {f"REQ-{module}-{i:03d}" for i in range(1, total + 1)}
    assert all(v == 1 for v in counts.values()), {r: v for r, v in counts.items() if v > 1}
    assert len(cases) == total, f"应去重为 {total} 条，实际 {len(cases)}"


def test_repair_prompt_at_call_site_is_batch_scoped():
    """**接线回归（修复①）**：多批次运行时，修复提示词必须只声明**本批**编号。

    为什么需要单独一条：`test_repair_prompt_scope_matches_batch` 只测了构造函数本身，
    而「调用点是否传入批次级集合」是独立的一行 —— 把它换回模块级集合后，
    前者仍然通过。更隐蔽的是：**修复②上线后会掩盖这个错误**
    （越界不再致命，所以即使提示词退回模块级也不会失败），
    此时唯一的可观测差异就是「提示词声明了哪些编号」。

    危害：提示词声明整模块 → 每批都重新生成整个模块的用例 → 大量跨批重复被
    dedup 丢弃（浪费 token 与轮次，且正是我们想从源头消除的重复）。
    """
    module, total = "A", 16
    reqs = _reqs(module, total)
    repair_decls: list[list[str]] = []

    def llm(cfg, user_text, **kwargs):
        # 修复路径：只按提示词声明的编号生成（忠实模型的正常行为）
        if "上一次输出" in user_text:
            declared = sorted(set(re.findall(rf"REQ-{module}-\d{{3}}", user_text)))
            repair_decls.append(declared)
            payload = [_case(f"TC-{module}-{i:03d}", module, [r], title=f"{r} 场景")
                       for i, r in enumerate(declared, 1)]
            return LLMResult(json.dumps(payload, ensure_ascii=False), "fake", 5, 5)
        # 初稿故意给非法内容 → 触发修复路径（这样才观测得到修复提示词）
        return LLMResult("这不是 JSON 数组", "fake", 5, 5)

    cfg = Config("k", "https://x", "m", 0.4, 10, 1)
    cases, _ = stages.run_stage_3_modules(
        cfg, "方案", reqs, "# A\n" + "正文\n" * 60,
        llm=llm, jobs=1, use_gate=True, max_gate_retries=1,
        batch_callback=lambda *_: None, reporter=lambda *_: None)

    all_ids = {f"REQ-{module}-{i:03d}" for i in range(1, total + 1)}
    assert repair_decls, "应至少触发一次修复（初稿是非法 JSON）"
    for decl in repair_decls:
        assert decl, "修复提示词必须声明编号"
        assert len(decl) < total, (
            f"修复提示词声明了 {len(decl)}/{total} 条 —— 说明传入了**模块级**集合，"
            f"而非本批范围：{decl}")
    # 各批声明集合互斥且并集为全模块 → 证明是「按批切分」而非「每批都拿全量」
    flat = [x for decl in repair_decls for x in decl]
    assert len(flat) == len(set(flat)) or len(repair_decls) == 1, (
        f"各批修复提示词的声明应互斥，实际：{repair_decls}")
    assert set(flat) <= all_ids
    # 覆盖无缺口
    covered = {r for c in cases for r in c["requirement"]}
    assert covered == all_ids, sorted(all_ids - covered)


def test_cross_batch_conflicts_are_surfaced_to_reporter():
    """去重发生时必须可见（不静默修改产物）。"""
    module, total = "A", 16
    reqs = _reqs(module, total)
    notes: list[str] = []

    def scope_ignoring_llm(cfg, user_text, **kwargs):
        payload = [_case(f"TC-{module}-{i:03d}", module, [f"REQ-{module}-{i:03d}"])
                   for i in range(1, total + 1)]
        return LLMResult(json.dumps(payload, ensure_ascii=False), "fake", 5, 5)

    cfg = Config("k", "https://x", "m", 0.4, 10, 1)
    stages.run_stage_3_modules(
        cfg, "方案", reqs, "# A\n" + "正文\n" * 50,
        llm=scope_ignoring_llm, jobs=1, use_gate=False,
        batch_callback=lambda *_: None, reporter=notes.append)

    assert any("跨批次重复覆盖" in n for n in notes), notes
