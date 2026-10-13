"""优先级确定性判定测试。

背景：原先优先级完全由模型自由发挥，存在三个问题：
1. 提示词**口径混用** —— P0 是风险导向（核心链路/安全/金额/权限），
   P2 却是维度导向（边界/次要），两条规则不同源；
2. 不可复现 —— 同一输入两次生成可能不同；
3. 与需求优先级无强制关系。

现行模型：`优先级 = 需求优先级 × 测试维度修正`，并设**数据安全例外**。

被否决的方案：「边界/兼容/异常/反向 一律 P2」。实测（71 条真实用例）会得到
**P0=1(1%) / P2=50(70%)**，且其中 40 条属核心链路（含保存落盘、删除确认、
上传校验）—— 把最有价值的一类用例整体降级，冒烟集只剩 1 条。
"""
from prd2tc.priority import (DIM_BOUNDARY, DIM_EXCEPTION, DIM_NEGATIVE, DIM_POSITIVE,
                             grade, infer_dimension, regrade)


def _case(title, *, method="场景法", tags=None, expected=None, requirement=None, priority=None):
    c = {
        "id": "TC-X-001", "module": "X", "title": title,
        "design_method": method, "priority": priority or "P1",
        "tags": tags if tags is not None else ["@regression"],
        "requirement": requirement or ["REQ-X-001"],
        "steps": [{"action": "click", "target": "按钮 (class=primary)", "data": ""}],
        "expected": expected or ["页面展示预期内容且状态正确"],
        "testData": [],
    }
    return c


# ---------- 维度推断 ----------

def test_strip_domain_noise_removes_prompt_word_only():
    """领域名词剥离是**独立契约**：只剥离领域词，不改动其它文本。

    为什么单独测：`_NEGATIVE_RE` 目前用 `提示(?:文案|…)` 限定了裸 `提示`，
    因此剥离函数对现有词表是冗余的 —— 但那只是「恰好现在不需要」。
    词表一旦被改回宽松写法（本项目刚经历过一次），
    这道归一化就是唯一防线；因此必须让它的行为被测试锁定，
    而不是因为「当前冗余」就无人守护（变异测试曾因此漏报）。
    """
    from prd2tc.priority import strip_domain_noise
    assert strip_domain_noise("主提示词编辑器") == "主编辑器"
    assert strip_domain_noise("提示词内容校验") == "内容校验"
    assert strip_domain_noise("无领域名词") == "无领域名词"
    assert strip_domain_noise("") == ""
    # 不误伤：单独的「提示」不是领域词，应保留
    assert strip_domain_noise("提示文案") == "提示文案"


def test_domain_noun_prompt_word_does_not_trigger_hint_keyword():
    """**核心回归**：领域名词「提示词」不得触发「提示」这个校验类关键词。

    `_NEGATIVE_RE` 原先含裸 `提示`，而本项目核心领域词是**提示词**（prompt）——
    其字面包含「提示」，导致「编辑后点击保存全部草稿」这类**主干正向**用例
    被判为「反向」，进而污染优先级判定与冒烟集选取。
    实测 v3 的 P0 中多数因此被错分，「主干正向」只剩 1 条，冒烟集无从选取。
    """
    main_flow = _case("编辑后点击保存全部草稿统一落盘",
                      expected=["保存后 toast 显示草稿已自动保存（模拟）"])
    assert infer_dimension(main_flow) == DIM_POSITIVE, infer_dimension(main_flow)

    concat = _case("工具调用后回复基于主提示词与场景提示词拼接生成",
                   expected=["回复遵循业务场景提示词中定义的规则"])
    assert infer_dimension(concat) == DIM_POSITIVE, infer_dimension(concat)

    # 但真正的「提示文案」仍应判为反向（不能因为剥离过度而漏判）
    assert infer_dimension(_case("校验提示文案显示错误信息",
                                 expected=["提示文案为请填写文档名称"])) == DIM_NEGATIVE


def test_dimension_from_design_method():
    assert infer_dimension(_case("任意标题", method="边界值分析")) == DIM_BOUNDARY
    assert infer_dimension(_case("任意标题", method="错误推测")) == DIM_NEGATIVE
    assert infer_dimension(_case("任意标题", method="场景法")) == DIM_POSITIVE


def test_dimension_from_text_when_method_is_neutral():
    """design_method 中性时，从标题/预期的文本特征推断。"""
    assert infer_dimension(_case("文档名称输入恰好20字通过")) == DIM_BOUNDARY
    assert infer_dimension(_case("第21字被拒绝且计数稳定")) == DIM_BOUNDARY
    assert infer_dimension(_case("名称为空提交被拒并提示")) == DIM_BOUNDARY
    assert infer_dimension(_case("接口异常时展示空态而非崩溃")) == DIM_EXCEPTION
    assert infer_dimension(_case("解析失败展示失败状态")) == DIM_EXCEPTION
    assert infer_dimension(_case("删除弹出二次确认弹窗")) == DIM_NEGATIVE
    assert infer_dimension(_case("三页签点击切换渲染正确内容")) == DIM_POSITIVE


def test_exception_takes_precedence_over_boundary_markers():
    """同时含边界与异常特征时，异常优先（后果更重）。"""
    c = _case("超过上限时上传失败并提示", method="边界值分析")
    assert infer_dimension(c) == DIM_EXCEPTION


# ---------- 判定表 ----------

def test_grade_table_follows_requirement_and_dimension():
    """P0 需求：正向/反向 → P0；边界/异常 → P1（无数据安全特征时）。"""
    assert grade(_case("主流程走通", method="场景法"), "P0") == "P0"
    assert grade(_case("输入校验提示", method="错误推测"), "P0") == "P0"
    assert grade(_case("输入恰好20字", method="边界值分析"), "P0") == "P1"
    assert grade(_case("接口异常降级", method="场景法",
                       expected=["服务不可用时展示降级内容"]), "P0") == "P1"


def test_grade_table_for_p1_and_p2_requirements():
    assert grade(_case("主流程走通"), "P1") == "P1"
    assert grade(_case("输入校验提示", method="错误推测"), "P1") == "P1"
    assert grade(_case("输入恰好20字", method="边界值分析"), "P1") == "P2"
    assert grade(_case("主流程走通"), "P2") == "P2"
    assert grade(_case("输入恰好20字", method="边界值分析"), "P2") == "P2"


def test_unknown_requirement_priority_defaults_to_p1():
    assert grade(_case("主流程"), "P9") == "P1"
    assert grade(_case("主流程"), "") == "P1"


# ---------- 数据安全例外（核心） ----------

def test_safety_exception_keeps_p0_for_write_protection():
    """**核心回归**：写操作防护不因维度降级。

    这些用例失败 = 数据错误/丢失/不可恢复，与「次要」完全不符。
    实测若一律降 P2，会把「保存落盘」「删除确认」「上传校验」降到最低级。
    """
    safety = [
        ("编辑后点击保存全部草稿统一落盘", "边界值分析"),
        ("删除文档弹出二次确认弹窗并说明移除范围", "错误推测"),
        ("支持txt/docx/xlsx并拒绝pdf等非法格式", "场景法"),
        ("文件大小恰好1MB通过且超过1MB被拒绝", "边界值分析"),
        ("启用开关切换实时生效，命中测试仅召回启用文档", "边界值分析"),
        ("主提示词失焦不再自动保存", "错误推测"),
    ]
    for title, method in safety:
        # 逐条给出能触发「维度 != 正向」的文本
        got = grade(_case(title, method=method), "P0",
                    requirement_title="保存/删除/上传相关需求")
        assert got == "P0", (title, method, got)


def test_safety_exception_does_not_promote_beyond_requirement():
    """例外只是「不降级」，不会把非 P0 需求拔高到 P0。"""
    assert grade(_case("保存全部草稿落盘", method="边界值分析"), "P1",
                 requirement_title="保存需求") == "P1"
    assert grade(_case("保存全部草稿落盘", method="边界值分析"), "P2",
                 requirement_title="保存需求") == "P2"


def test_non_safety_boundary_still_demoted():
    """非写操作防护的边界用例仍应降级（避免例外被滥用）。"""
    got = grade(_case("列表为空时展示空态", method="边界值分析"), "P0",
                requirement_title="列表展示需求")
    assert got == "P1", got


def test_safety_uses_requirement_title_too():
    """需求标题含写操作特征即可触发例外（用例标题可能只写「恰好1MB」）。"""
    got = grade(_case("恰好 1MB 通过", method="边界值分析"), "P0",
                requirement_title="支持上传文档，单文件≤1MB")
    assert got == "P0", got


# ---------- regrade（批量重判） ----------

def test_regrade_assigns_priority_and_records_original():
    cases = [_case("主流程", priority="P1"), _case("恰好20字", method="边界值分析", priority="P0")]
    reqs = [{"id": "REQ-X-001", "priority": "P0", "title": "列表展示需求"}]
    out = regrade(cases, reqs)
    assert out[0]["priority"] == "P0"
    assert out[0]["prioritySource"] == "graded"
    assert out[0]["priorityBasis"]["requirement"] == "P0"
    # 原优先级被保留以便审计
    assert out[1].get("modelPriority") == "P0"
    assert out[1]["priority"] == "P1"


def test_regrade_adds_smoke_tag_for_new_p0():
    """重判会**产生新的 P0**，必须补 @smoke —— 门禁要求 P0 必带该标签。"""
    cases = [_case("主流程走通", tags=["@regression"], priority="P1",
                   requirement=["REQ-X-001"])]
    reqs = [{"id": "REQ-X-001", "priority": "P0", "title": "主流程需求"}]
    regrade(cases, reqs)
    assert cases[0]["priority"] == "P0"
    assert "@smoke" in cases[0]["tags"]


def test_regrade_does_not_duplicate_smoke_tag():
    cases = [_case("主流程走通", tags=["@smoke", "@regression"])]
    regrade(cases, [{"id": "REQ-X-001", "priority": "P0", "title": "x"}])
    assert cases[0]["tags"].count("@smoke") == 1


def test_regrade_handles_missing_tags_field():
    c = _case("主流程走通")
    c.pop("tags")
    regrade([c], [{"id": "REQ-X-001", "priority": "P0", "title": "x"}])
    assert c["tags"] == ["@smoke"]


def test_regrade_multi_requirement_takes_highest_priority():
    """多需求用例取**最高**需求优先级，不因关联次要需求而降级。"""
    cases = [_case("主流程", requirement=["REQ-A-001", "REQ-A-002"])]
    reqs = [{"id": "REQ-A-001", "priority": "P2", "title": "次要"},
            {"id": "REQ-A-002", "priority": "P0", "title": "主流程"}]
    regrade(cases, reqs)
    assert cases[0]["priority"] == "P0"


def test_regrade_without_requirements_uses_default():
    """没有需求表时按默认 P1 处理（不崩溃、不产生 P0 假象）。"""
    cases = [_case("主流程")]
    regrade(cases, None)
    assert cases[0]["priority"] == "P1"


def test_regrade_is_deterministic():
    def build():
        return [_case("恰好20字", method="边界值分析"),
                _case("删除确认", method="错误推测")]
    reqs = [{"id": "REQ-X-001", "priority": "P0", "title": "删除需求"}]
    a = [c["priority"] for c in regrade(build(), reqs)]
    b = [c["priority"] for c in regrade(build(), reqs)]
    assert a == b


def test_priority_distribution_stays_usable():
    """**回归**：口径不得把绝大多数用例压到 P2（被否决方案的问题）。

    构造一批典型用例（含边界/异常/写操作防护），断言 P0 与 P2 都不是极端值。
    """
    titles = [
        ("三页签点击切换渲染正确", "场景法"),
        ("编辑后保存全部草稿落盘", "场景法"),
        ("删除文档二次确认", "错误推测"),
        ("上传非法格式被拒绝", "场景法"),
        ("文档名称恰好20字", "边界值分析"),
        ("第21字被拒绝", "边界值分析"),
        ("文件恰好1MB通过", "边界值分析"),
        ("接口异常展示空态", "场景法"),
        ("列表为空展示空态", "边界值分析"),
        ("解析失败展示失败状态", "场景法"),
        ("长结果折叠展开", "场景法"),
        ("空态点击保留列表", "错误推测"),
    ]
    cases = [_case(t, method=m) for t, m in titles]
    reqs = [{"id": "REQ-X-001", "priority": "P0", "title": "配置保存与删除需求"}]
    regrade(cases, reqs)
    from collections import Counter
    dist = Counter(c["priority"] for c in cases)
    total = len(cases)
    assert dist["P0"] >= 1, dist
    # P2 不应吞掉大多数用例
    assert dist["P2"] / total < 0.6, dist
