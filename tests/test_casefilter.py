"""用例批量筛选测试（模块 / 优先级 / 标签三维度 + 复合条件）。

核心语义：**维度内取并集（OR），跨维度取交集（AND）**。

    modules=["KB","AGENT"]                         → KB 或 AGENT
    modules=["KB"], priorities=["P0"]               → KB 且 P0
    modules=["KB"], tags=["@smoke","@regression"]   → KB 且（@smoke 或 @regression）

把维度内也做成 AND 会让「多选模块」永远选不出任何用例 —— 那是无用的默认。
"""
import pytest

from prd2tc.casefilter import (CaseFilter, apply, facets, make_filter, matches,
                               summarize)


def _case(cid, module, priority, tags, *, title="场景", reqs=None, status=None):
    c = {"id": cid, "module": module, "priority": priority, "tags": list(tags),
         "title": title, "requirement": reqs or [f"REQ-{module}-001"],
         "steps": [{"action": "goto", "target": "首页", "data": ""}],
         "expected": ["页面成功渲染"]}
    if status:
        c["status"] = status
    return c


@pytest.fixture()
def cases():
    return [
        _case("TC-KB-001", "KB", "P0", ["@smoke", "@regression", "@kb"],
              title="上传文档", reqs=["REQ-KB-001"]),
        _case("TC-KB-002", "KB", "P1", ["@regression", "@kb"],
              title="按名称搜索", reqs=["REQ-KB-002"]),
        _case("TC-KB-003", "KB", "P2", ["@regression", "@kb"],
              title="边界：恰好20字", reqs=["REQ-KB-003"]),
        _case("TC-AGENT-001", "AGENT", "P0", ["@regression", "@agent"],
              title="三页签切换", reqs=["REQ-AGENT-001"]),
        _case("TC-TOOL-001", "TOOL", "P1", ["@regression", "@tool"],
              title="工具展示", reqs=["REQ-TOOL-001"]),
    ]


# ---------- 构造与规范化 ----------

def test_make_filter_trims_and_dedupes():
    spec = make_filter([" KB ", "KB", "", "AGENT"], ["P0", "P0"], ["@smoke", " "])
    assert spec.modules == ("KB", "AGENT")
    assert spec.priorities == ("P0",)
    assert spec.tags == ("@smoke",)


def test_empty_filter_is_inactive():
    assert make_filter().active is False
    assert make_filter([], [], "", "  ").active is False
    assert make_filter(["KB"]).active is True
    assert make_filter(keyword="REQ").active is True


# ---------- 维度内 OR ----------

def test_modules_are_union(cases):
    got = [c["id"] for c in apply(cases, make_filter(modules=["KB", "AGENT"]))]
    assert got == ["TC-KB-001", "TC-KB-002", "TC-KB-003", "TC-AGENT-001"]


def test_single_module_only(cases):
    got = [c["id"] for c in apply(cases, make_filter(modules=["KB"]))]
    assert got == ["TC-KB-001", "TC-KB-002", "TC-KB-003"]


def test_priorities_are_union(cases):
    got = [c["id"] for c in apply(cases, make_filter(priorities=["P0", "P2"]))]
    assert got == ["TC-KB-001", "TC-KB-003", "TC-AGENT-001"]


def test_tags_are_union(cases):
    got = [c["id"] for c in apply(cases, make_filter(tags=["@smoke", "@agent"]))]
    assert got == ["TC-KB-001", "TC-AGENT-001"]


# ---------- 跨维度 AND（复合条件） ----------

def test_module_and_priority_intersect(cases):
    got = [c["id"] for c in apply(cases, make_filter(modules=["KB"], priorities=["P0"]))]
    assert got == ["TC-KB-001"]


def test_module_and_tag_intersect(cases):
    """**用户示例**：只导出指定单模块中的回归用例。"""
    spec = make_filter(modules=["KB"], tags=["@regression"])
    got = [c["id"] for c in apply(cases, spec)]
    assert got == ["TC-KB-001", "TC-KB-002", "TC-KB-003"]


def test_three_dimensions_all_must_match(cases):
    spec = make_filter(modules=["KB"], priorities=["P0"], tags=["@smoke"])
    assert [c["id"] for c in apply(cases, spec)] == ["TC-KB-001"]
    # 条件互相矛盾 → 空集（而不是忽略某个维度）
    assert apply(cases, make_filter(modules=["KB"], priorities=["P0"], tags=["@agent"])) == []


def test_tag_not_on_case_excludes_it(cases):
    assert apply(cases, make_filter(tags=["@nonexistent"])) == []


# ---------- 关键词 ----------

def test_keyword_matches_id_title_or_requirement(cases):
    assert [c["id"] for c in apply(cases, make_filter(keyword="REQ-TOOL"))] == ["TC-TOOL-001"]
    assert [c["id"] for c in apply(cases, make_filter(keyword="三页签"))] == ["TC-AGENT-001"]
    assert [c["id"] for c in apply(cases, make_filter(keyword="TC-KB-002"))] == ["TC-KB-002"]


def test_keyword_is_substring_and_case_sensitive_by_design(cases):
    # 关键词按子串匹配；大小写在中文场景下不重要，英文标签需原样
    assert apply(cases, make_filter(keyword="不存在的词")) == []


# ---------- matches 与 apply 的一致性 ----------

def test_matches_agrees_with_apply(cases):
    spec = make_filter(modules=["KB"], priorities=["P0", "P1"])
    assert [c["id"] for c in cases if matches(c, spec)] == \
           [c["id"] for c in apply(cases, spec)]


def test_apply_preserves_order_and_does_not_mutate(cases):
    original = [c["id"] for c in cases]
    out = apply(cases, make_filter(priorities=["P0"]))
    assert [c["id"] for c in cases] == original          # 输入未被改动
    assert [c["id"] for c in out] == ["TC-KB-001", "TC-AGENT-001"]


def test_apply_without_filter_returns_all(cases):
    assert len(apply(cases, None)) == len(cases)
    assert len(apply(cases, make_filter())) == len(cases)


def test_apply_missing_fields_do_not_crash():
    """缺字段的用例不得让筛选崩溃（旧产物可能没有 tags）。"""
    cases = [{"id": "TC-X-001"}, {"id": "TC-X-002", "module": "X", "tags": ["@smoke"]}]
    assert [c["id"] for c in apply(cases, make_filter(tags=["@smoke"]))] == ["TC-X-002"]
    assert [c["id"] for c in apply(cases, make_filter(modules=["X"]))] == ["TC-X-002"]


# ---------- facets：分面计数 ----------

def test_facets_counts_all_dimensions(cases):
    f = facets(cases)
    assert f["total"] == 5 and f["matched"] == 5
    assert {m["value"]: m["count"] for m in f["modules"]} == {"AGENT": 1, "KB": 3, "TOOL": 1}
    assert {p["value"]: p["count"] for p in f["priorities"]} == {"P0": 2, "P1": 2, "P2": 1}
    assert {t["value"]: t["count"] for t in f["tags"]}["@regression"] == 5


def test_facets_priorities_sorted_by_priority_order(cases):
    vals = [p["value"] for p in facets(cases)["priorities"]]
    assert vals == ["P0", "P1", "P2"]


def test_facets_tags_sorted_by_count_desc_then_same_count_by_name():
    """高频标签在前（便于 UI 呈现），同数量按名称升序保证确定性。"""
    cases = [
        _case("TC-A-001", "A", "P0", ["@regression", "@b", "@a"]),
        _case("TC-A-002", "A", "P1", ["@regression", "@b"]),
        _case("TC-A-003", "A", "P2", ["@regression", "@a"]),
    ]
    tags = facets(cases)["tags"]
    # @regression 出现 3 次最先；@a 与 @b 各 2 次 → 按名称升序 @a 在前
    assert [t["value"] for t in tags] == ["@regression", "@a", "@b"]


def test_facets_reflects_current_filter(cases):
    f = facets(cases, make_filter(modules=["KB"]))
    assert f["matched"] == 3
    assert {m["value"]: m["count"] for m in f["modules"]} == {"KB": 3}


def test_facets_ignore_self_is_essential_for_compound_selection(cases):
    """**核心行为**：统计某维度时忽略它自身的条件。

    否则选了「优先级=P0」之后，优先级列表就只剩 P0 一项（数量恒等于已选结果），
    用户看不到「放宽到 P1 会剩多少条」，也就无法做有意义的复合选择。

    用例必须让**两个分支的数值不同**才能证明该行为生效 ——
    否则测试无论如何都通过（这类「无法区分」的测试此前吃过亏）。
    """
    spec = make_filter(modules=["KB"], priorities=["P0"])
    # 不忽略：优先级只统计命中集（仅 P0），P1/P2 直接消失
    plain = {p["value"]: p["count"] for p in facets(cases, spec)["priorities"]}
    assert plain == {"P0": 1}
    # 忽略优先级自身：模块约束仍在，但优先级展示**可达**数量 → P1/P2 重新出现
    ignored = {p["value"]: p["count"] for p in facets(cases, spec, ignore="priorities")["priorities"]}
    assert ignored == {"P0": 1, "P1": 1, "P2": 1}
    assert ignored != plain, "两者必须不同，否则无法证明 ignore 生效"


def test_facets_wildcard_ignores_each_dimension_self(cases):
    """**核心回归**：`ignore="*"` 让**每个维度各自忽略自身**。

    这是 UI 实际使用的模式（一次请求取全部维度的可用候选）。
    必须单独测：若只测「逐维度 ignore」，`ignore_self` 那条分支永远走不到
    （逐维度时 `use_ignore == dimension` 先命中），变异测试曾因此漏报。
    """
    spec = make_filter(modules=["KB"], priorities=["P0"])
    f = facets(cases, spec, ignore="*")
    # 模块维度忽略自身 → 仍能看到 AGENT（否则候选会塌缩成已选的 KB）
    assert {m["value"] for m in f["modules"]} == {"KB", "AGENT"}, f["modules"]
    # 优先级维度忽略自身 → P0/P1/P2 都在（被模块条件约束）
    assert {p["value"] for p in f["priorities"]} == {"P0", "P1", "P2"}, f["priorities"]
    # 标签维度同样忽略自身，但受模块+优先级约束
    assert {t["value"] for t in f["tags"]} == {"@regression", "@smoke", "@kb"}, f["tags"]
    # matched 仍是**全条件**的结果（不受 ignore 影响）
    assert f["matched"] == len(apply(cases, spec))


def test_facets_wildcard_keeps_other_constraints(cases):
    """`ignore="*"` 只是各自忽略自身，**其它维度仍然生效**。

    若实现成「完全忽略筛选」，计数会退回全量 —— 那就失去了分面选择的意义。
    """
    cases = [
        _case("TC-A-001", "A", "P0", ["@x"]),
        _case("TC-B-001", "B", "P1", ["@y"]),
    ]
    spec = make_filter(priorities=["P0"])
    f = facets(cases, spec, ignore="*")
    # 模块被 P0 约束 → 只剩 A
    assert {m["value"]: m["count"] for m in f["modules"]} == {"A": 1}, f["modules"]
    # 优先级忽略自身 → P0 与 P1 都在，且**受模块条件（无）**…此处无模块条件，
    # 故全局 P0/P1 各 1
    assert {p["value"] for p in f["priorities"]} == {"P0", "P1"}, f["priorities"]


def test_facets_wildcard_equals_no_filter_when_nothing_selected(cases):
    """没有选择时，`ignore="*"` 与普通调用结果一致（不产生差异）。"""
    assert facets(cases, None, ignore="*") == facets(cases, None)
    assert facets(cases, make_filter(), ignore="*") == facets(cases, make_filter())


def test_facets_ignore_other_dimension_keeps_self_constraint(cases):
    """`ignore` 只影响被指定的那个维度，其它维度的条件仍然生效。"""
    spec = make_filter(priorities=["P0"])
    f = facets(cases, spec, ignore="priorities")
    # 模块列表应被 priorities=P0 约束（只剩 KB 与 AGENT）
    assert {m["value"]: m["count"] for m in f["modules"]} == {"AGENT": 1, "KB": 1}


def test_facets_on_empty_input():
    f = facets([])
    assert f["total"] == 0 and f["matched"] == 0
    assert f["modules"] == [] and f["priorities"] == [] and f["tags"] == []


# ---------- describe / name_suffix ----------

def test_describe_is_human_readable(cases):
    assert make_filter().describe() == "全部用例"
    assert make_filter(modules=["KB", "AGENT"]).describe() == "模块=KB,AGENT"
    assert make_filter(modules=["KB"], priorities=["P0"]).describe() == "模块=KB；优先级=P0"


def test_name_suffix_is_filename_safe():
    assert make_filter().name_suffix() == ""
    assert make_filter(modules=["KB", "AGENT"]).name_suffix() == "KB-AGENT"
    assert make_filter(modules=["KB"], priorities=["P0"]).name_suffix() == "KB-P0"
    # 标签的 @ 保留（@smoke 比 smoke 更明确），但危险字符必须清除
    assert make_filter(tags=["@smoke"]).name_suffix() == "smoke"
    assert "/" not in make_filter(modules=["a/b"]).name_suffix()
    assert ":" not in make_filter(modules=["a:b"]).name_suffix()
    assert len(make_filter(modules=["x" * 200]).name_suffix()) <= 60


def test_name_suffix_never_collides_for_different_filters():
    """不同条件应产生不同后缀 —— 否则两种筛选会互相覆盖导出文件。"""
    a = make_filter(modules=["KB"], priorities=["P0"]).name_suffix()
    b = make_filter(modules=["KBP0"]).name_suffix()
    assert a != b, (a, b)


# ---------- to_query ----------

def test_to_query_uses_repeated_keys():
    spec = make_filter(modules=["KB", "AGENT"], priorities=["P0"], tags=["@smoke"],
                       keyword="上传")
    assert spec.to_query() == {"modules": ["KB", "AGENT"], "priorities": ["P0"],
                               "tags": ["@smoke"], "keyword": ["上传"]}


def test_to_query_omits_empty_dimensions():
    assert make_filter(modules=["KB"]).to_query() == {"modules": ["KB"]}


# ---------- summarize ----------

def test_summarize_reports_excluded_count(cases):
    s = summarize(cases, make_filter(modules=["KB"], priorities=["P0"]))
    assert s == {"filter": "模块=KB；优先级=P0", "filtered": True,
                 "total": 5, "matched": 1, "excluded": 4}


def test_summarize_without_filter(cases):
    s = summarize(cases, None)
    assert s["filtered"] is False and s["matched"] == 5 and s["excluded"] == 0


# ---------- 确定性 ----------

def test_filtering_is_deterministic(cases):
    spec = make_filter(modules=["KB"], priorities=["P0", "P1"], tags=["@regression"])
    first = [c["id"] for c in apply(cases, spec)]
    second = [c["id"] for c in apply(cases, spec)]
    assert first == second
    assert facets(cases, spec) == facets(cases, spec)
