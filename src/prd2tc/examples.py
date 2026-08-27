"""学习能力：历史 PRD-用例对示例库 → 相关性检索 → prompt 注入（RAG-lite）。

用户可把「已有 PRD 需求点 + 对应测试用例」沉淀为示例文件（JSON 数组），
pipeline 按与当前 PRD 的相关性检索 Top-K 注入各阶段 prompt，
让大模型学习既有测试设计逻辑（模块划分、优先级口径、格式、设计逻辑说明）。

示例文件格式（examples/*.json，数组）：
[
  {
    "module": "AUTH",
    "title": "账号体系（登录/注册/停用拦截）",
    "keywords": ["登录", "注册", "手机号", "密码", "停用", "账号"],
    "requirement": "该模块需求要点摘要…",
    "design_logic": "为什么这么设计（P0 必带 @smoke、反向给明确断言…）",
    "cases": [ {…与 case-template.md 字段一致的用例…} ]
  }
]
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_WORD_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]{2,}")


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(text or ""))


@dataclass
class Example:
    module: str
    title: str
    keywords: list[str] = field(default_factory=list)
    requirement: str = ""
    design_logic: str = ""
    cases: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Example":
        return cls(
            module=str(d.get("module", "")),
            title=str(d.get("title", "")),
            keywords=[str(k) for k in d.get("keywords", [])],
            requirement=str(d.get("requirement", "")),
            design_logic=str(d.get("design_logic", "")),
            cases=d.get("cases", []) or [],
        )


def load_examples(examples_dir: str | Path) -> list[Example]:
    """扫描目录下所有 *.json 示例文件（每个文件为数组）。"""
    d = Path(examples_dir)
    if not d.is_dir():
        raise FileNotFoundError(f"示例目录不存在: {d}")
    out: list[Example] = []
    for f in sorted(d.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8").lstrip("\ufeff"))
        items = data if isinstance(data, list) else [data]
        for item in items:
            ex = Example.from_dict(item)
            if ex.module and ex.cases:
                out.append(ex)
    return out


def score(query: str, ex: Example) -> float:
    """相关性打分：模块命中 + 关键词覆盖 + 需求要点覆盖（轻量无依赖）。"""
    s = 0.0
    q = _tokens(query)
    if ex.module and ex.module.lower() in query.lower():
        s += 3.0
    if ex.keywords:
        hit = sum(1 for k in ex.keywords if k in query)
        s += hit * 1.0
    if ex.requirement:
        req_tokens = _tokens(ex.requirement)
        overlap = len(q & req_tokens)
        s += overlap * 0.2
    if ex.title and any(t in query for t in _tokens(ex.title)):
        s += 0.5
    return s


def retrieve(examples: list[Example], query: str, k: int = 3) -> list[Example]:
    """按相关性取 Top-K（分数为 0 的示例不注入，避免噪声）。"""
    ranked = sorted(examples, key=lambda ex: score(query, ex), reverse=True)
    return [ex for ex in ranked if score(query, ex) > 0][:k]


def render_examples(examples: list[Example]) -> str:
    """把示例渲染为 prompt 注入块（学习设计逻辑与输出格式）。"""
    if not examples:
        return ""
    lines = [
        "# 参考示例（历史 PRD-用例对，请学习其模块划分、优先级口径、字段格式与设计逻辑，再完成当前任务）",
        "",
    ]
    for i, ex in enumerate(examples, 1):
        lines.append(f"## 参考示例{i} [{ex.module}] {ex.title}")
        if ex.design_logic:
            lines.append(f"\n设计逻辑：{ex.design_logic}")
        if ex.requirement:
            lines.append(f"\n需求要点：{ex.requirement}")
        lines.append(f"\n用例示例（{len(ex.cases)} 条，字段与最终输出一致）：")
        lines.append("```json")
        lines.append(json.dumps(ex.cases, ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")
    return "\n".join(lines)
