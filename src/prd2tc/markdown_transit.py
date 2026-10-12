"""Markdown 表格 ↔ 用例 JSON 的程序化转换（「Markdown 中转」模式）。

动机（实测）：让模型直接输出 JSON 是本项目最脆弱的环节。
- `testData` 等字段一旦出现嵌套对象/数组，就需要多层引号转义；
- 实测 MiniMax-M3 因此在 `<think>` 里反复自我纠错，整轮 26,504 字符输出
  **100% 是推理、可用产出为 0**，白烧一轮 token 与时间；
- 结构化开销也高：实测字段名 + 标点占 68.1% 的字符。

改用 Markdown 表格作为**中转格式**（产物仍是 JSON，机读契约不变）：
- 表格里没有引号嵌套，转义问题从根上消失；
- 实测同一内容字符数减少约 20%（等价 token 同比例下降）。

设计取舍：只做「模型输出 → 结构化」的单向解析 + 渲染；不改动落盘 JSON 契约，
因此 validator / traceability / Playwright 消费方完全不受影响。
"""
from __future__ import annotations

import json
import re

COLUMNS = ["id", "module", "subModule", "title", "design_method", "priority",
           "tags", "requirement", "preconditions", "testData", "steps", "expected"]

LIST_FIELDS = {"tags", "requirement", "preconditions", "expected"}
CELL_SEP = ";"
ITEM_SEP = ","

_STEP_RE = re.compile(r"^\s*([A-Za-z]+)\s*(.*?)\s*(?:=\s*(.*))?$")


def render_table(cases: list[dict]) -> str:
    """把用例渲染为 Markdown 表格（供 A/B 对比与调试查看）。"""
    lines = ["| " + " | ".join(COLUMNS) + " |",
             "|" + "|".join(["---"] * len(COLUMNS)) + "|"]
    for case in cases:
        lines.append("| " + " | ".join(_cell(case, col) for col in COLUMNS) + " |")
    return "\n".join(lines)


def _sanitize(text) -> str:
    """去掉会破坏表格的字符：竖线与换行（换行统一为「；」）。"""
    return str(text or "").replace("|", "｜").replace("\n", "；").replace("\r", "").strip()


def _cell(case: dict, column: str) -> str:
    value = case.get(column)
    if value is None:
        return ""
    if column == "testData":
        return _sanitize(CELL_SEP.join(str(x) for x in value) if isinstance(value, list) else value)
    if column in LIST_FIELDS:
        return _sanitize(ITEM_SEP.join(str(x) for x in value) if isinstance(value, list) else value)
    if column == "steps":
        return _sanitize(_render_steps(value))
    return _sanitize(value)


def _render_steps(steps) -> str:
    parts = []
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        action = str(step.get("action", "")).strip()
        target = str(step.get("target", "")).strip()
        data = step.get("data")
        text = f"{action} {target}".strip()
        if data not in (None, ""):
            text += f"={data}"
        parts.append(text.strip())
    return CELL_SEP.join(parts)


def _split_cells(line: str) -> list[str]:
    raw = line.strip()
    if raw.startswith("|"):
        raw = raw[1:]
    if raw.endswith("|"):
        raw = raw[:-1]
    return [cell.strip() for cell in raw.split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(bool(c.strip()) and set(c) <= {"-", ":", " "} for c in cells)


def _convert(column: str, value):
    text = str(value or "").replace("｜", "|").strip()
    if column in LIST_FIELDS:
        return [item.strip() for item in text.split(ITEM_SEP) if item.strip()]
    if column == "testData":
        return [item.strip() for item in text.split(CELL_SEP) if item.strip()]
    if column == "steps":
        return _parse_steps(text)
    return text


def _parse_steps(text: str) -> list[dict]:
    steps = []
    for chunk in str(text or "").split(CELL_SEP):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = _STEP_RE.match(chunk)
        if not match:
            continue
        action = match.group(1)
        target = (match.group(2) or "").strip()
        data = match.group(3)
        steps.append({"action": action, "target": target,
                      "data": "" if data is None else data.strip()})
    return steps


# 表头大小写不敏感：模型可能写 SUBMODULE 或 submodule，必须映射回契约中的规范名
_CANONICAL = {column.lower(): column for column in COLUMNS}


def parse_table(text: str) -> list[dict]:
    """把 Markdown 表格解析为用例列表；表头决定列映射。

    只接受含 `id` 与 `module` 表头的表格，避免把说明性表格误当产物。
    """
    lines = [line for line in str(text or "").splitlines() if line.strip().startswith("|")]
    for index, line in enumerate(lines):
        raw_header = _split_cells(line)
        header = [_CANONICAL.get(h.strip().lower(), "") for h in raw_header]
        if "id" not in header or "module" not in header:
            continue
        body = lines[index + 1:]
        if body and _is_separator(_split_cells(body[0])):
            body = body[1:]
        cases = []
        for row in body:
            cells = _split_cells(row)
            if _is_separator(cells) or not any(cells):
                continue
            record = {}
            for position, column in enumerate(header):
                if not column:
                    continue
                value = cells[position] if position < len(cells) else ""
                record[column] = _convert(column, value)
            if record.get("id"):
                cases.append({k: v for k, v in record.items() if v not in ("", [], None)})
        if cases:
            return cases
    return []


def extract_table_cases(text: str) -> list[dict]:
    """从模型输出里抽取 Markdown 表格用例；找不到则抛错（由上层回退 JSON 解析）。"""
    cases = parse_table(text)
    if not cases:
        raise ValueError("模型输出中未找到用例 Markdown 表格")
    return cases


def compact_json(cases: list[dict]) -> str:
    """紧凑 JSON 渲染，仅用于字符/token 对比。"""
    return json.dumps(cases, ensure_ascii=False, separators=(",", ":"))
