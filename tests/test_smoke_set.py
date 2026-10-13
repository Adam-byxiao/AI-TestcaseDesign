"""冒烟集（@smoke）规则测试。

背景：此前 `@smoke` 与 `P0` 是 **1:1 等同**的 —— 校验器强制「P0 必带 @smoke」，
于是该标签不承载任何信息，只是 P0 的同义词。实测评测：

| 运行 | P0 占比 | P0 带 @smoke | 冒烟集占比 |
| --- | ---: | ---: | ---: |
| v2 | 90/121 | 100% | **74%** |
| v3 | 18/71 | 100% | **25%** |

「冒烟」本该是**部署后最先跑的最小集合**（回答「这个版本还能不能继续测」），
占全量 74% 的集合毫无「冒烟」意义。

现行规则：`@smoke` = P0 的**真子集**，四条约束：
  1. 必须是 P0；
  2. 排除「边界」「异常」维度（冒烟验证能不能走通，不验证边界处理）；
  3. 每个需求最多 1 条（同需求内优先「正向」）；
  4. 总量不超过 `min(25, 20% × 总数)`。
"""
import pytest

from prd2tc.priority import (DIM_BOUNDARY, DIM_EXCEPTION, DIM_NEGATIVE, DIM_POSITIVE,
                             SMOKE_MAX, SMOKE_RATIO, apply_smoke, infer_dimension,
                             regrade, select_smoke)
from prd2tc.validator import check_smoke_set, validate_case


def _case(cid, module, title, *, priority="P0", method="场景法", reqs=None,
          expected=None, tags=None):
    return {
        "id": cid, "module": module, "subModule": "S", "title": title,
        "design_method": method, "priority": priority,
        "tags": tags if tags is not None else ["@regression"],
        "requirement": reqs or [f"REQ-{module}-001"],
        "preconditions": ["已登录"], "testData": [],
        "steps": [{"action": "goto", "target": "首页", "data": ""},
                  {"action": "expectVisible", "target": "主体区域", "data": ""}],
        "expected": expected or ["页面成功渲染并显示预期内容"],
    }


def _reqs(pairs):
    return [{"id": rid, "module": rid.split("-")[1], "priority": p, "title": t}
            for rid, p, t in pairs]


# ---------- 基本方向：@smoke ⊆ P0 ----------

def test_smoke_is_never_assigned_to_non_p0():
    cases = [_case(f"TC-A-{i:03d}", "A", f"主流程 {i}", priority=p)
             for i, p in enumerate(["P1", "P2", "P0"], 1)]
    reqs = _reqs([("REQ-A-001", "P1", "普通需求")])
    regrade(cases, reqs)
    smoke = [c for c in cases if "@smoke" in c["tags"]]
    assert all(c["priority"] == "P0" for c in smoke), smoke


def test_p0_without_smoke_is_legal():
    """P0 **不必**带 @smoke —— 这正是本次修正的核心（旧规则强制 P0 必带）。"""
    case = _case("TC-A-001", "A", "边界校验", method="边界值分析",
                 expected=["恰好 20 字时通过"])
    errors = validate_case(case, 0, set())
    assert not any("@smoke" in e for e in errors), errors


def test_smoke_on_p1_is_rejected():
    case = _case("TC-A-001", "A", "普通流程", priority="P1", tags=["@smoke", "@regression"])
    errors = validate_case(case, 0, set())
    assert any("@smoke" in e and "只能标在 P0" in e for e in errors), errors


# ---------- 选取规则 ----------

def test_excludes_boundary_and_exception_dimensions():
    """**核心规则**：边界与异常用例不进冒烟。

    冒烟只回答「能不能走通」；「恰好 20 字」「接口异常降级」属于深入测试。

    这里刻意**关闭总量上限**（`ratio=1.0, max_count=1000`）：
    否则上限的截断会先一步把边界用例排除掉，测试就变成在验证上限而非维度规则
    —— 变异测试曾因此漏报（给 32 条用例时上限只有 6，边界用例根本轮不到）。
    隔离单一规则，才能证明「是维度规则在起作用」。
    """
    cases = [_case(f"TC-A-{i:03d}", "A", f"正常主流程 {i}", reqs=[f"REQ-A-{i:03d}"])
             for i in range(1, 6)]
    cases.append(_case("TC-B-001", "A", "文档名称恰好20字通过", method="边界值分析",
                       reqs=["REQ-B-001"], expected=["恰好 20 字时被接受"]))
    cases.append(_case("TC-B-002", "A", "接口异常时展示降级内容", reqs=["REQ-B-002"],
                       expected=["服务不可用时展示降级内容而非崩溃"]))
    # 不设上限，单独观察维度规则
    smoke = select_smoke(cases, ratio=1.0, max_count=1000)
    assert "TC-B-001" not in smoke, "边界用例不得进冒烟"
    assert "TC-B-002" not in smoke, "异常用例不得进冒烟"
    assert smoke == {f"TC-A-{i:03d}" for i in range(1, 6)}, smoke


def test_one_case_per_requirement():
    """同一需求最多 1 条 —— 这是规模的主要控制手段。"""
    cases = [_case(f"TC-A-{i:03d}", "A", f"场景 {i}", reqs=["REQ-A-001"]) for i in range(1, 6)]
    smoke = select_smoke(cases)
    assert len(smoke) == 1, smoke


def test_prefers_positive_over_negative_within_requirement():
    """同一需求内优先取「正向」用例（反向/校验类属深入测试）。"""
    cases = [
        _case("TC-A-001", "A", "删除需二次确认", method="错误推测", reqs=["REQ-A-001"],
              expected=["弹出二次确认弹窗"]),
        _case("TC-A-002", "A", "点击保存全部草稿落盘", reqs=["REQ-A-001"],
              expected=["草稿成功保存并落盘"]),
    ]
    smoke = select_smoke(cases)
    assert smoke == {"TC-A-002"}, smoke


def test_selection_is_deterministic():
    cases = [_case(f"TC-A-{i:03d}", "A", f"主流程 {i}", reqs=[f"REQ-A-{i:03d}"])
             for i in range(1, 6)]
    assert select_smoke(cases) == select_smoke(cases)


def test_cases_without_requirement_are_skipped():
    """无 requirement 的用例无从判定归属，不占配额也不入选。"""
    cases = [_case("TC-A-001", "A", "无需求引用", reqs=[]),
             _case("TC-A-002", "A", "有需求引用", reqs=["REQ-A-001"])]
    for c in cases:
        if c["id"] == "TC-A-001":
            c["requirement"] = []
    assert select_smoke(cases) == {"TC-A-002"}


def test_empty_inputs():
    assert select_smoke([]) == set()


# ---------- 总量上限 ----------

def test_ratio_cap_limits_smoke_count():
    """**核心回归**：冒烟集不得随用例总量线性膨胀（旧实现是 74%）。"""
    cases = [_case(f"TC-A-{i:03d}", "A", f"主流程 {i}", reqs=[f"REQ-A-{i:03d}"])
             for i in range(1, 101)]           # 100 条用例，100 个需求
    smoke = select_smoke(cases)
    assert len(smoke) <= max(1, min(SMOKE_MAX, int(len(cases) * SMOKE_RATIO))), len(smoke)
    assert len(smoke) < len(cases) / 4, len(smoke)   # 远小于全量


def test_smoke_max_absolute_cap():
    cases = [_case(f"TC-A-{i:03d}", "A", f"主流程 {i}", reqs=[f"REQ-A-{i:03d}"])
             for i in range(1, 501)]           # 500 条 → 20% = 100，但绝对上限 25
    assert len(select_smoke(cases)) <= SMOKE_MAX


def test_at_least_one_smoke_when_candidates_exist():
    cases = [_case("TC-A-001", "A", "唯一主流程", reqs=["REQ-A-001"])]
    assert len(select_smoke(cases)) == 1


# ---------- apply_smoke：统一设置标签 ----------

def test_apply_smoke_adds_and_removes_to_match_rule():
    """**核心行为**：`apply_smoke` 会**移除**不符合规则的 @smoke。

    否则模型在边界用例上标的 @smoke 会留在产物里，
    `@smoke` 就不再等于「冒烟集」这一语义。
    """
    cases = [
        _case("TC-A-001", "A", "正常保存", reqs=["REQ-A-001"]),
        _case("TC-A-002", "A", "恰好20字", method="边界值分析", reqs=["REQ-A-002"]),
        _case("TC-A-003", "A", "普通功能", priority="P1", reqs=["REQ-A-003"]),
    ]
    # 人为把 @smoke 打在错误的位置（边界用例、P1 用例）
    cases[1]["tags"] = ["@smoke", "@regression"]
    cases[2]["tags"] = ["@smoke", "@regression"]
    smoke = apply_smoke(cases)
    assert smoke == {"TC-A-001"}, smoke
    assert "@smoke" in cases[0]["tags"]
    assert "@smoke" not in cases[1]["tags"], "边界用例的 @smoke 应被移除"
    assert "@smoke" not in cases[2]["tags"], "P1 用例的 @smoke 应被移除"


def test_apply_smoke_does_not_duplicate_tag():
    cases = [_case("TC-A-001", "A", "正常保存", reqs=["REQ-A-001"], tags=["@smoke", "@regression"])]
    apply_smoke(cases)
    assert cases[0]["tags"].count("@smoke") == 1


def test_apply_smoke_records_reason_for_audit():
    cases = [_case("TC-A-001", "A", "正常保存", reqs=["REQ-A-001"])]
    apply_smoke(cases)
    reason = cases[0].get("smokeReason")
    assert reason == {"priority": "P0", "dimension": infer_dimension(cases[0])}, reason


def test_regrade_applies_smoke_after_priority():
    """顺序很重要：必须先定稿优先级再选冒烟 —— 否则基于旧优先级挑选。"""
    cases = [_case("TC-A-001", "A", "保存草稿落盘", priority="P1", reqs=["REQ-A-001"])]
    reqs = _reqs([("REQ-A-001", "P0", "保存全部草稿需求")])
    regrade(cases, reqs)
    # 需求是 P0 → 用例被判为 P0（写操作防护）→ 才可能进冒烟
    assert cases[0]["priority"] == "P0"
    assert "@smoke" in cases[0]["tags"]


# ---------- 整体规模校验（validate_dir 层面） ----------

def test_check_smoke_set_rejects_empty_when_p0_exists():
    errors = check_smoke_set([], ["TC-A-001"], total=10)
    assert errors and "冒烟集为空" in errors[0], errors


def test_check_smoke_set_rejects_oversized():
    """**核心回归**：冒烟集过大必须报错（旧规则下它等于全部 P0）。"""
    smoke = [f"TC-A-{i:03d}" for i in range(1, 41)]     # 40 条
    errors = check_smoke_set(smoke, smoke, total=100)   # 上限 = min(25, 20) = 20
    assert errors and "冒烟集过大" in errors[0], errors


def test_check_smoke_set_accepts_reasonable_size():
    smoke = [f"TC-A-{i:03d}" for i in range(1, 11)]
    assert check_smoke_set(smoke, smoke + ["TC-A-099"], total=100) == []


def test_check_smoke_set_no_p0_means_no_constraint():
    assert check_smoke_set([], [], total=10) == []


def test_regrade_smoke_ratio_regression_on_realistic_batch():
    """**端到端回归**：一批用例重判后，冒烟集必须远小于 P0 总量。

    这是本次修正的量化目标：旧规则下冒烟集 == P0（100%），
    新规则下应显著更小。
    """
    cases = []
    for i in range(1, 31):
        method = "边界值分析" if i % 3 == 0 else "场景法"
        cases.append(_case(f"TC-A-{i:03d}", "A", f"主流程场景 {i}",
                           priority="P0", method=method, reqs=[f"REQ-A-{i:03d}"]))
    reqs = _reqs([(f"REQ-A-{i:03d}", "P0", f"需求 {i}") for i in range(1, 31)])
    regrade(cases, reqs)
    p0 = [c for c in cases if c["priority"] == "P0"]
    smoke = [c for c in cases if "@smoke" in c["tags"]]
    assert len(smoke) < len(p0), (len(smoke), len(p0))
    assert len(smoke) <= max(1, min(SMOKE_MAX, int(len(cases) * SMOKE_RATIO)))
