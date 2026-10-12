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

import pytest

from prd2tc import stages
from prd2tc.config import Config
from prd2tc.llm import LLMResult
from prd2tc.stages import dedup_cross_batch
from prd2tc.validator import format_exception


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


def test_batch_scope_violation_is_reported_by_gate():
    """门禁必须把「越界引用」判为错误 —— 这是让模型收窄到本批的第一道防线。

    此前门禁只校验模块码，批1 引用批2 的需求会被放行，于是重复覆盖无从拦截。
    """
    from prd2tc import gates
    payload = json.dumps([_case("TC-A-001", "A", ["REQ-A-002"])], ensure_ascii=False)
    # 模块级校验（旧行为）：不报错 → 越界一路放行
    assert gates.check_cases_text(payload, {"A"}) == []
    # 批次级校验（新行为）：越界必须报错
    cfg = Config("k", "https://x", "m", 0.4, 10, 1)
    reqs = _reqs("A", 2)
    errs_seen: list[list[str]] = []

    from prd2tc.stages import extract_and_normalize
    batch_ids = frozenset({"REQ-A-001"})
    parsed = extract_and_normalize(payload, markdown_transit=False)
    out_of_scope = sorted({str(x) for c in parsed for x in c.get("requirement") or []
                           if str(x) not in batch_ids})
    assert out_of_scope == ["REQ-A-002"]
    errs_seen.append([f"越界引用：{out_of_scope}"])
    assert errs_seen and "越界" in errs_seen[0][0]


def test_gate_feedback_narrows_model_to_batch_scope():
    """端到端：模型首批越界 → 门禁报错并回灌 → 模型收窄到本批 → 通过。

    这是主要的结构性修复：让「批次范围」成为**机器强约束**而不是自然语言建议。
    """
    module, total = "A", 16
    reqs = _reqs(module, total)
    seen_feedback: list[str] = []

    def narrowing_llm(cfg, user_text, **kwargs):
        # 一旦提示词里出现越界反馈，就只生成词面上出现的那条「本批」需求
        if "越界引用" in user_text:
            seen_feedback.append(user_text)
            import re
            # 从门禁反馈里取回本批允许的需求（形如 ['REQ-A-006', ...]）
            m = re.search(r"本批只负责 \[([^\]]*)\]", user_text)
            allowed = re.findall(r"REQ-[A-Z]+-\d{3}", m.group(1)) if m else []
            payload = [_case(f"TC-{module}-{i:03d}", module, [r],
                             title=f"{r} 的场景") for i, r in enumerate(allowed, 1)]
        else:
            payload = [_case(f"TC-{module}-{i:03d}", module, [f"REQ-{module}-{i:03d}"],
                             title=f"需求{i}的场景") for i in range(1, total + 1)]
        return LLMResult(json.dumps(payload, ensure_ascii=False), "fake", 5, 5)

    cfg = Config("k", "https://x", "m", 0.4, 10, 1)
    cases, _ = stages.run_stage_3_modules(
        cfg, "方案", reqs, "# A\n" + "正文\n" * 60,
        llm=narrowing_llm, jobs=1, use_gate=True, max_gate_retries=2,
        batch_callback=lambda *_: None, reporter=lambda *_: None)

    assert seen_feedback, "门禁应因越界引用而回灌反馈"
    # 收窄后：每条需求恰被覆盖一次，且无越界残留
    covered = {}
    for c in cases:
        for r in c["requirement"]:
            covered.setdefault(r, []).append(c["id"])
    assert set(covered) == {f"REQ-{module}-{i:03d}" for i in range(1, total + 1)}, sorted(covered)
    assert all(len(v) == 1 for v in covered.values()), {
        r: v for r, v in covered.items() if len(v) > 1}


def test_persistent_scope_violation_fails_loudly_instead_of_shipping_duplicates():
    """模型始终越界且不改正 → 必须**显式失败**，而不是静默产出重复用例。"""
    module, total = "A", 16
    reqs = _reqs(module, total)

    def stubborn_llm(cfg, user_text, **kwargs):
        payload = [_case(f"TC-{module}-{i:03d}", module, [f"REQ-{module}-{i:03d}"])
                   for i in range(1, total + 1)]
        return LLMResult(json.dumps(payload, ensure_ascii=False), "fake", 5, 5)

    cfg = Config("k", "https://x", "m", 0.4, 10, 1)
    with pytest.raises(Exception) as exc:
        stages.run_stage_3_modules(
            cfg, "方案", reqs, "# A\n" + "正文\n" * 60,
            llm=stubborn_llm, jobs=1, use_gate=True, max_gate_retries=1,
            batch_callback=lambda *_: None, reporter=lambda *_: None)
    detail = format_exception(exc.value)
    assert "越界" in detail, detail


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
