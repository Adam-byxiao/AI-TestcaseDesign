"""用例优先级的**确定性判定**。

为什么需要它
------------
原先优先级完全由模型自由发挥，带来三个问题：

1. **口径混用**：提示词里 P0 是**风险导向**（「核心链路/安全/金额/权限」），
   而 P2 是**维度导向**（「边界/次要」）—— 两条规则不同源，模型只能自行取舍。
2. **不可复现**：同一份输入两次生成，优先级可能不同；`--repro` 也无法保证。
3. **与需求优先级无强制关系**：需求已有 P0/P1/P2，用例优先级却可与之无关。

判定模型：`优先级 = 需求优先级（业务重要性） × 测试维度修正`，并设**数据安全例外**。

    维度 \\ 需求      P0        P1        P2
    正向/反向        P0        P1        P2
    边界/异常        P1        P2        P2

为什么不是「边界/异常一律 P2」（曾评估过的方案）
----------------------------------------------
实测（71 条真实用例）该方案会得到 **P0=1(1%) / P2=50(70%)**：
- P0 只剩 1 条，冒烟集失去意义；
- 被降为 P2 的 50 条中有 **40 条属于核心链路**，包含
  「编辑后保存落盘」「删除文档二次确认」「上传类型/大小校验」——
  这些破防即**数据错误或丢失**，与「次要」完全不符；
- 边界/异常恰是缺陷发现率最高的一类，把它们整体降级，
  等于让测试优先跳过最有价值的部分。

因此引入**数据安全例外**：涉及写操作防护（保存/发布/删除确认/上传校验/启用影响检索）
的用例**不因维度而降级**。
"""
from __future__ import annotations

import re

# 维度取值
DIM_POSITIVE = "正向"      # 主流程走通
DIM_NEGATIVE = "反向"      # 校验/拒绝/确认等反向分支
DIM_BOUNDARY = "边界"      # 边界值、上限、空值
DIM_EXCEPTION = "异常"     # 失败/不可用/容错/降级

DIMENSIONS = (DIM_POSITIVE, DIM_NEGATIVE, DIM_BOUNDARY, DIM_EXCEPTION)

# 需求优先级 → 维度修正后的用例优先级
# 正向/反向保持需求级别（写操作的正向与反向都直接决定数据正确性）；
# 边界/异常降一级（真实存在但触发概率低）。
_GRADE_TABLE: dict[tuple[str, str], str] = {
    ("P0", DIM_POSITIVE): "P0", ("P0", DIM_NEGATIVE): "P0",
    ("P0", DIM_BOUNDARY): "P1", ("P0", DIM_EXCEPTION): "P1",
    ("P1", DIM_POSITIVE): "P1", ("P1", DIM_NEGATIVE): "P1",
    ("P1", DIM_BOUNDARY): "P2", ("P1", DIM_EXCEPTION): "P2",
    ("P2", DIM_POSITIVE): "P2", ("P2", DIM_NEGATIVE): "P2",
    ("P2", DIM_BOUNDARY): "P2", ("P2", DIM_EXCEPTION): "P2",
}

# P3 是低频/体验级别（提示词保留该档），一律维持最低级
_DEFAULT_DIMENSION = DIM_POSITIVE

# ---- 维度推断 ----
# 模型显式标注的 design_method 是**首选**依据（它是被测系统的既有契约）；
# 文本特征作为补充，用于 design_method 缺失或与场景不符时。
_BOUNDARY_METHODS = {"边界值分析"}
_NEGATIVE_METHODS = {"错误推测"}

# 维度推断词表。顺序即优先级：异常 > 边界 > 反向 > 正向。
# 「拒绝/拦截/非法」刻意归入**反向**而非异常 —— 它们描述的是「系统正确拒绝非法输入」
# （校验类），不是服务端故障。归错会让 `priorityBasis.dimension` 失去可信度。
_EXCEPTION_RE = re.compile(
    r"异常|失败|错误|不可用|超时|崩溃|降级|容错|越权|无权限|不可恢复")
_BOUNDARY_RE = re.compile(
    r"恰好|边界|上限|下限|第\s*\d+\s*字|超长|超大|为空|空值|越界|不允许|禁用|截断"
    r"|未选|无结果|无启用|空态|超过")
_NEGATIVE_RE = re.compile(
    r"校验|提示|必填|二次确认|确认弹窗|取消|仅|不可|不含|不触发|不产生|不显示|互不串扰"
    r"|拒绝|拦截|非法")

# ---- 数据安全例外 ----
# 写操作的防护：这些用例失败意味着数据错误、丢失或不可恢复，
# 不因「它是边界/异常用例」而降级。
_SAFETY_RE = re.compile(
    r"保存|落盘|发布|删除|移除|确认删除|确认移除|取消删除|上传|类型|格式"
    r"|1\s*MB|MB|启用|开关|持久化|覆盖|替换|连接|绑定")


def infer_dimension(case: dict) -> str:
    """推断用例的测试维度。

    优先看 `design_method`（模型显式标注、可审计），再回退到文本特征。
    """
    blob = " ".join([
        str(case.get("title") or ""),
        " ".join(str(x) for x in (case.get("expected") or [])),
        " ".join(str(x) for x in (case.get("testData") or [])),
    ])
    if _EXCEPTION_RE.search(blob):
        return DIM_EXCEPTION
    method = str(case.get("design_method") or "")
    if method in _BOUNDARY_METHODS or _BOUNDARY_RE.search(blob):
        return DIM_BOUNDARY
    if method in _NEGATIVE_METHODS or _NEGATIVE_RE.search(blob):
        return DIM_NEGATIVE
    return _DEFAULT_DIMENSION


def _is_safety(case: dict, requirement_title: str = "") -> bool:
    """是否属于「写操作防护」（数据安全例外）。"""
    blob = " ".join([
        str(case.get("title") or ""),
        requirement_title,
        " ".join(str(x) for x in (case.get("expected") or [])),
    ])
    return bool(_SAFETY_RE.search(blob))


def grade(case: dict, requirement_priority: str = "P1",
          requirement_title: str = "") -> str:
    """判定单条用例的优先级（纯函数，确定性）。

    `requirement_priority` 来自阶段 1 的需求分析（业务重要性的权威来源）；
    `requirement_title` 用于数据安全例外的判定。
    """
    base = str(requirement_priority or "P1").upper()
    if base not in ("P0", "P1", "P2", "P3"):
        base = "P1"
    if base == "P3":
        return "P3"
    dimension = infer_dimension(case)
    if dimension in (DIM_BOUNDARY, DIM_EXCEPTION) and _is_safety(case, requirement_title):
        return base                       # 数据安全例外：不降级
    return _GRADE_TABLE.get((base, dimension), base)


def regrade(cases: list[dict], requirements: list[dict] | None = None) -> list[dict]:
    """按需求优先级与维度**统一重判**全部用例的优先级（原地修改并返回）。

    与 `renumber`（id 重编号）、模块码强制同构：属于**确定性强制**，
    不依赖模型自觉。原优先级记录在 `modelPriority` 以便审计与对比。

    同时维护 `@smoke` 标签：门禁要求 P0 必带 `@smoke`。重判会**产生新的 P0**
    （如 P1 用例因数据安全例外升为 P0），若不补标签，
    产物就会违反既有门禁契约。反向不摘标签（降级后仍带 @smoke 无害）。
    """
    rq_priority: dict[str, str] = {}
    rq_title: dict[str, str] = {}
    for r in requirements or []:
        rid = str(r.get("id") or "")
        if rid:
            rq_priority[rid] = str(r.get("priority") or "P1")
            rq_title[rid] = str(r.get("title") or "")
    for case in cases:
        refs = [str(x) for x in (case.get("requirement") or [])]
        # 多需求用例取**最高**优先级（保守：不因关联了次要需求而降级）
        pris = [rq_priority.get(r, "P1") for r in refs] or ["P1"]
        base = min(pris, key=lambda p: {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(p, 1))
        title = " ".join(rq_title.get(r, "") for r in refs)
        original = case.get("priority")
        graded = grade(case, base, title)
        if original and str(original) != graded:
            case.setdefault("modelPriority", str(original))
        case["priority"] = graded
        case["prioritySource"] = "graded"
        case["priorityBasis"] = {"requirement": base, "dimension": infer_dimension(case)}
        if graded == "P0":
            tags = case.get("tags")
            if not isinstance(tags, list):
                tags = []
            if "@smoke" not in tags:
                tags.insert(0, "@smoke")
            case["tags"] = tags
    return cases
