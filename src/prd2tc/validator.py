"""用例 JSON 校验（与 docs/case-template.md 字段规范一致）。

规则与 scripts/orchestrate.mjs 对齐：
  - id 必填且唯一，格式 TC-<MOD>[-<SUB>]-<3位序号>
  - module ∈ 模块表；priority ∈ P0..P3
  - P0 必带 @smoke 标签；tags/requirement/steps/expected 非空
  - step.action ∈ 动作枚举，step.target 非空
"""
from __future__ import annotations

import json
import re
from pathlib import Path

MODULES = [
    "AUTH", "HOME", "PROD", "SEARCH", "CART", "CHECKOUT", "COUPON",
    "ORDER", "ACCOUNT", "COMMENT", "COMMON", "PERF",
]
PRIORITIES = ["P0", "P1", "P2", "P3"]
ACTIONS = [
    "goto", "fill", "click", "selectOption", "check", "hover", "press", "reload",
    "expectVisible", "expectText", "expectUrl", "waitFor",
]
ID_RE = re.compile(r"^TC-[A-Z]+(-[A-Z0-9]+)?-\d{3}$")
REQ_RE = re.compile(r"^REQ-[A-Z]+-\d{3}$")


class ValidationError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(f"{len(errors)} 个校验问题")


def validate_case(case: dict, index: int, seen_ids: set[str]) -> list[str]:
    """校验单条用例，返回错误列表（尽可能收集全部问题）。"""
    errors: list[str] = []
    at = f"[用例#{index + 1} {case.get('id', '(无id)')}]"

    cid = case.get("id")
    if not cid or not isinstance(cid, str):
        errors.append(f"{at} 缺少 id")
    else:
        if not ID_RE.match(cid):
            errors.append(f"{at} id 格式非法: {cid}")
        if cid in seen_ids:
            errors.append(f"{at} id 重复: {cid}")
        seen_ids.add(cid)

    if case.get("module") not in MODULES:
        errors.append(f"{at} module 非法: {case.get('module') or '(空)'}")
    if case.get("priority") not in PRIORITIES:
        errors.append(f"{at} priority 非法: {case.get('priority') or '(空)'}")
    tags = case.get("tags")
    if not isinstance(tags, list) or len(tags) == 0:
        errors.append(f"{at} tags 为空")
    if case.get("priority") == "P0" and (not isinstance(tags, list) or "@smoke" not in tags):
        errors.append(f"{at} P0 用例必须含 @smoke 标签")

    reqs = case.get("requirement")
    if not isinstance(reqs, list) or len(reqs) == 0:
        errors.append(f"{at} requirement 为空")
    for r in reqs or []:
        if not REQ_RE.match(str(r)):
            errors.append(f"{at} requirement 格式非法: {r}")

    steps = case.get("steps")
    if not isinstance(steps, list) or len(steps) == 0:
        errors.append(f"{at} steps 为空")
    for s in steps or []:
        if not isinstance(s, dict):
            errors.append(f"{at} step 不是对象")
            continue
        if s.get("action") not in ACTIONS:
            errors.append(f"{at} step.action 非法: {s.get('action') or '(空)'}")
        if not s.get("target"):
            errors.append(f"{at} step.target 为空")

    expected = case.get("expected")
    if not isinstance(expected, list) or len(expected) == 0:
        errors.append(f"{at} expected 为空")
    if not case.get("title"):
        errors.append(f"{at} 缺少 title")
    return errors


def validate_dir(cases_dir: str | Path) -> tuple[int, list[str]]:
    """校验目录下所有用例 JSON（顶层为数组）。返回 (用例数, 错误列表)。"""
    d = Path(cases_dir)
    if not d.is_dir():
        raise FileNotFoundError(f"目录不存在: {d}")
    seen: set[str] = set()
    errors: list[str] = []
    total = 0
    for f in sorted(d.glob("*.json")):
        try:
            cases = json.loads(f.read_text(encoding="utf-8").lstrip("\ufeff"))
        except json.JSONDecodeError as e:
            errors.append(f"[文件 {f.name}] JSON 解析失败: {e}")
            continue
        if not isinstance(cases, list):
            errors.append(f"[文件 {f.name}] 顶层必须是数组")
            continue
        for i, case in enumerate(cases):
            errors.extend(validate_case(case, total + i, seen))
        total += len(cases)
    return total, errors
