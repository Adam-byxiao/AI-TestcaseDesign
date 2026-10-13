"""用例批量筛选：模块 / 优先级 / 标签三维度，支持复合条件。

语义（唯一规则，避免歧义）
------------------------
**维度内取并集（OR），跨维度取交集（AND）。**

    modules=["KB","AGENT"]                    → KB 或 AGENT
    modules=["KB"], priorities=["P0"]         → KB **且** 为 P0
    modules=["KB"], tags=["@smoke","@regression"] → KB 且（含 @smoke 或 @regression）

这条规则对应实际使用：选多个模块是「这几个模块都要」，
而「指定单模块中的回归用例」是跨维度组合（AND）。
把维度内也做成 AND 会让「多选模块」永远选不出任何用例 —— 那是无用的默认。

设计原则
--------
纯函数、无 I/O、确定性：筛选结果只依赖输入，便于测试与 `--repro` 复现。
`facets()` 支持**分面计数**（给定当前筛选，各维度还各有多少条可选），
使 UI 能在选择过程中显示实时数量，而不是让用户盲选。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 优先级排序：P0 最重要（用于 facets 的稳定输出顺序）
_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

# 文件名安全的描述字符：`@` 在文件名中合法（@smoke 是标签原文），故保留
_UNSAFE_IN_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True)
class CaseFilter:
    """用例筛选条件。空元组表示「该维度不限」。"""
    modules: tuple[str, ...] = ()
    priorities: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    # 关键词：匹配 id / 标题 / 关联需求编号（用于「只看某个需求相关」）
    keyword: str = ""

    @property
    def active(self) -> bool:
        """是否有任何筛选条件 —— 决定导出是否需要重算统计与追踪矩阵。"""
        return bool(self.modules or self.priorities or self.tags or self.keyword.strip())

    def describe(self) -> str:
        """人类可读描述（用于提示与文件名）。"""
        parts: list[str] = []
        if self.modules:
            parts.append("模块=" + ",".join(self.modules))
        if self.priorities:
            parts.append("优先级=" + ",".join(self.priorities))
        if self.tags:
            parts.append("标签=" + ",".join(self.tags))
        if self.keyword.strip():
            parts.append(f"关键词={self.keyword.strip()}")
        return "；".join(parts) if parts else "全部用例"

    def name_suffix(self) -> str:
        """文件名后缀片段（清洗为文件系统安全字符）。

        `KB,P0` → `KB-P0`；标签里的 `@` 保留（`@smoke` 比 `smoke` 更明确）。
        """
        parts: list[str] = []
        if self.modules:
            parts.append("-".join(self.modules))
        if self.priorities:
            parts.append("-".join(self.priorities))
        if self.tags:
            parts.append("-".join(t.lstrip("@") for t in self.tags))
        if self.keyword.strip():
            parts.append(self.keyword.strip())
        raw = "-".join(p for p in parts if p)
        cleaned = _UNSAFE_IN_NAME.sub("_", raw).strip(" .-_")
        return cleaned[:60]

    def to_query(self) -> dict[str, list[str]]:
        """转为查询参数（重复键），供前端拼 URL。"""
        out: dict[str, list[str]] = {}
        if self.modules:
            out["modules"] = list(self.modules)
        if self.priorities:
            out["priorities"] = list(self.priorities)
        if self.tags:
            out["tags"] = list(self.tags)
        if self.keyword.strip():
            out["keyword"] = [self.keyword.strip()]
        return out


def _clean(values) -> tuple[str, ...]:
    """去重并保持首次出现顺序（确定性，且不受调用方传参顺序之外的因素影响）。"""
    seen: set[str] = set()
    out: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


def make_filter(modules=None, priorities=None, tags=None, keyword: str = "") -> CaseFilter:
    """构造筛选条件（自动去空、去重）。"""
    return CaseFilter(modules=_clean(modules), priorities=_clean(priorities),
                      tags=_clean(tags), keyword=str(keyword or ""))


def matches(case: dict, spec: CaseFilter) -> bool:
    """单条用例是否命中筛选。"""
    if spec.modules and str(case.get("module") or "") not in spec.modules:
        return False
    if spec.priorities and str(case.get("priority") or "") not in spec.priorities:
        return False
    if spec.tags:
        case_tags = {str(t) for t in (case.get("tags") or [])}
        if not case_tags.intersection(spec.tags):
            return False
    keyword = spec.keyword.strip()
    if keyword:
        haystack = " ".join([
            str(case.get("id") or ""),
            str(case.get("title") or ""),
            " ".join(str(r) for r in (case.get("requirement") or [])),
        ])
        if keyword not in haystack:
            return False
    return True


def apply(cases: list[dict], spec: CaseFilter | None) -> list[dict]:
    """筛选用例（保持原顺序）。`spec` 为空或无条件时原样返回。"""
    if spec is None or not spec.active:
        return list(cases)
    return [c for c in cases if matches(c, spec)]


def _tag_counts(cases: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in cases:
        for tag in (case.get("tags") or []):
            text = str(tag)
            counts[text] = counts.get(text, 0) + 1
    return counts


def _reduced(spec: CaseFilter | None, dimension: str) -> CaseFilter | None:
    """去掉某维度自身的约束（保留其它维度与关键词）。"""
    if spec is None:
        return None
    return CaseFilter(
        modules=() if dimension == "modules" else spec.modules,
        priorities=() if dimension == "priorities" else spec.priorities,
        tags=() if dimension == "tags" else spec.tags,
        keyword=spec.keyword,
    )


def facets(cases: list[dict], spec: CaseFilter | None = None, *,
           ignore: str = "") -> dict:
    """分面统计：各维度的可选值及其当前数量。

    `ignore` 的两种用法：
      - 具体维度名（`modules`/`priorities`/`tags`）：只忽略该维度自身的条件；
      - `"*"`：**每个维度各自忽略自身**（分面搜索的标准语义），
        一次调用即可得到全部维度的可用候选。

    `"*"` 是 UI 实际使用的模式：三个维度需要各自独立计数，若只能逐维度忽略，
    前端就得发三次请求再合并（实测那样写会互相覆盖结果）。
    """
    base = list(cases) if spec is None or not spec.active else \
        [c for c in cases if matches(c, spec)]

    def pool(dimension: str, *, use_ignore: str, ignore_self: bool) -> list[dict]:
        """统计某维度时的候选池。

        - `use_ignore == dimension`：只忽略该维度；
        - `ignore_self=True`：对**当前**维度忽略其自身约束；
        两者都不满足时用全约束池（`base`）。
        """
        if spec is None or not spec.active:
            return list(cases)
        if use_ignore and use_ignore == dimension:
            return [c for c in cases if matches(c, _reduced(spec, dimension))]
        if ignore_self:
            return [c for c in cases if matches(c, _reduced(spec, dimension))]
        return base

    def counts(pooled: list[dict], field: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for case in pooled:
            key = str(case.get(field) or "")
            if key:
                out[key] = out.get(key, 0) + 1
        return out

    wildcard = (ignore == "*")
    module_counts = counts(pool("modules", use_ignore=ignore, ignore_self=wildcard), "module")
    priority_counts = counts(pool("priorities", use_ignore=ignore, ignore_self=wildcard),
                             "priority")
    tag_counts = _tag_counts(pool("tags", use_ignore=ignore, ignore_self=wildcard))

    return {
        "total": len(cases),
        "matched": len(base),
        "modules": [{"value": k, "count": v}
                    for k, v in sorted(module_counts.items())],
        "priorities": [{"value": k, "count": v}
                       for k, v in sorted(priority_counts.items(),
                                          key=lambda kv: (_PRIORITY_ORDER.get(kv[0], 9), kv[0]))],
        # 标签按数量降序、同数量按名称升序 —— 确定性，且高频标签在前
        "tags": [{"value": k, "count": v}
                 for k, v in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))],
    }


def summarize(cases: list[dict], spec: CaseFilter | None = None) -> dict:
    """筛选摘要：用于导出产物标注「这是子集」以及 UI 提示。"""
    matched = apply(cases, spec)
    return {
        "filter": spec.describe() if spec else "全部用例",
        "filtered": bool(spec and spec.active),
        "total": len(cases),
        "matched": len(matched),
        "excluded": len(cases) - len(matched),
    }
