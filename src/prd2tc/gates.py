"""机器强制门禁：对每个阶段的模型输出做程序化校验（对应 prompt 00 的「输出自检门」）。

- 阶段 1 需求分析：必备章节 + REQ 覆盖（与 PRD 对齐）
- 阶段 2 测试方案：必备章节
- 阶段 3 用例生成：JSON 可解析 + 字段强校验（复用 validator）

用法：check_* 返回错误列表（空 = 通过）。
门禁失败时由 stages.generate_with_feedback 把错误反馈给模型修正重出。
"""
from __future__ import annotations

import json
import re

from prd2tc import stages as _stages  # noqa: F401  （仅类型提示，避免循环依赖在模块级）
from prd2tc.validator import validate_case
from prd2tc.traceability import build_traceability, validate_traceability

REQ_RE = re.compile(r"REQ-[A-Z][A-Z0-9_]*-\d{3}")

ANALYSIS_SECTIONS = ["功能模块清单", "角色清单", "需求点明细", "业务规则", "非功能需求", "风险与疑点"]
_SECTION_HINTS = {
    "业务规则": "（PRD 中的校验规则/状态规则/错误与边界处理/数据联动规则均应归入此章节）",
    "非功能需求": "（PRD 中的性能/兼容性/安全/可用性/可维护性/NFR 均应归入此章节）",
    "风险与疑点": "（PRD 中的风险/依赖/待确认事项/Open Questions 均应归入此章节）",
}
PLAN_SECTIONS = [
    "测试范围",
    "测试策略",
    "优先级口径",
    "风险与应对",
    "测试环境与数据",
    "准入",
    "准出",
    "用例量",
]


def _missing_sections(text: str, sections: list[str]) -> list[str]:
    return [f"缺少必备章节「{s}」{_SECTION_HINTS.get(s, '')}" for s in sections if s not in text]


def check_analysis(text: str, prd_text: str | None = None) -> list[str]:
    """阶段 1 门禁：必备章节 + 与 PRD 的 REQ 覆盖对齐。"""
    errors = _missing_sections(text, ANALYSIS_SECTIONS)
    if prd_text:
        prd_reqs = set(REQ_RE.findall(prd_text))
        analysis_reqs = set(REQ_RE.findall(text))
        if prd_reqs:
            missing = sorted(prd_reqs - analysis_reqs)
            errors += [f"需求分析遗漏需求点 {r}（PRD 中存在但报告未覆盖）" for r in missing]
            extra = sorted(analysis_reqs - prd_reqs)
            for r in extra:
                errors.append(f"需求分析出现 PRD 中不存在的需求点 {r}（如属自动编号请标注「自动编号」）")
    return errors


def check_plan(text: str) -> list[str]:
    """阶段 2 门禁：测试方案必备章节。"""
    return _missing_sections(text, PLAN_SECTIONS)


def check_four_sections(text: str) -> list[str]:
    """阶段 1 分段合并小调用门禁：角色清单/业务规则/非功能需求/风险与疑点。"""
    return _missing_sections(text, ["角色清单", "业务规则", "非功能需求", "风险与疑点"])


def check_cases_text(text: str, allowed_modules: set[str] | None = None) -> list[str]:
    """阶段 3 门禁：JSON 可解析 + 用例字段强校验（id 唯一/枚举/非空）。"""
    try:
        cases = _stages.extract_and_normalize(text)
    except (ValueError, json.JSONDecodeError) as e:
        return [f"用例 JSON 解析失败: {e}（请只输出一个 JSON 数组）"]
    if not cases:
        return ["未生成任何用例"]
    seen: set[str] = set()
    errors = [e for i, c in enumerate(cases) for e in validate_case(c, i, seen, allowed_modules)]
    return errors


def check_traceability(requirements: list[dict], cases: list[dict], *, min_cases: int = 1) -> tuple[dict, list[str]]:
    """校验用例引用真实性和覆盖要求，返回矩阵与错误列表。"""
    matrix = build_traceability(requirements, cases, min_cases=min_cases)
    return matrix, validate_traceability(matrix)
