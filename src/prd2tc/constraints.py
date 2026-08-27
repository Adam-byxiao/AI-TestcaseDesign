"""用例生成约束：静态测试原理 + 示例库学习到的模式（RAG 约束模板填充）。

「学习约束」从检索到的参考示例中动态提取：
  - 示例库模块/规模、优先级口径、常用标签
  - 从 design_logic 推断采用的设计方法
  - 断言风格与格式约定
填充进 assets/prompts/04-case-constraints.md 的 {{learned_patterns}} 占位符，
作为阶段 3 prompt 的「学习约束」部分，让模型在生成时遵循既有设计逻辑。
"""
from __future__ import annotations

from collections import Counter

from prd2tc import loader
from prd2tc.examples import Example

CONSTRAINT_TPL = "prompts/04-case-constraints.md"
PRINCIPLES = "prompts/00-testing-principles.md"

METHOD_HINTS = [
    ("边界", "边界值分析"),
    ("临界", "边界值分析"),
    ("门槛", "边界值分析"),
    ("上下限", "边界值分析"),
    ("组合", "判定表"),
    ("状态", "状态迁移"),
    ("场景", "场景法"),
    ("等价", "等价类划分"),
    ("错误推测", "错误推测"),
    ("异常流", "错误推测/异常流"),
    ("反向", "错误推测/异常流"),
]


def infer_methods(design_logic: str) -> list[str]:
    """从设计逻辑文本推断用例采用的设计方法（关键词启发，零依赖）。"""
    found: list[str] = []
    for kw, method in METHOD_HINTS:
        if kw in design_logic and method not in found:
            found.append(method)
    return found


def build_learned_patterns(examples: list[Example]) -> str:
    """从参考示例提取「学习约束」文本。"""
    if not examples:
        return "（无参考示例，仅遵循静态约束）"
    total = sum(len(e.cases) for e in examples)
    prio = Counter(c.get("priority", "") for e in examples for c in e.cases)
    tags = Counter(t for e in examples for c in e.cases for t in c.get("tags", []))
    modules = sorted({e.module for e in examples})
    methods = Counter(m for e in examples for m in infer_methods(e.design_logic))
    lines = [
        f"- 示例库覆盖模块：{'、'.join(modules)}（{len(examples)} 组 / {total} 条用例）。",
        f"- 示例优先级口径：{' / '.join(f'{k}×{v}' for k, v in sorted(prio.items()))}，新用例的优先级分布应与此保持一致。",
    ]
    if tags:
        lines.append(f"- 示例常用标签：{' '.join(t for t, _ in tags.most_common(6))}。")
    if methods:
        lines.append(f"- 示例采用的设计方法：{'、'.join(m for m, _ in methods.most_common(5))}，请沿用同类方法。")
    lines.append("- 保持与示例一致的字段格式与断言风格：金额断言用 ¥ 千分位 2 位小数精确值；状态断言文案给出可容忍变体。")
    for e in examples[:2]:
        if e.design_logic:
            lines.append(f"- [{e.module}] 设计逻辑（必须学习）：{e.design_logic}")
    return "\n".join(lines)


def build_case_constraints(examples: list[Example] | None = None) -> str:
    """填充 04-case-constraints.md：静态约束 + 示例学习约束。"""
    tpl = loader.load_asset(CONSTRAINT_TPL)
    return tpl.replace("{{learned_patterns}}", build_learned_patterns(examples or []))


def load_principles() -> str:
    """共享测试原理层（注入各阶段）。"""
    return loader.load_asset(PRINCIPLES)
