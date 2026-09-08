"""与 02-Playwright-Automation 对接的执行结果契约。"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path


def make_execution_result(run_id: str, results: list[dict]) -> dict:
    return {"schemaVersion": "1.0", "runId": run_id, "results": results}


def load_execution_result(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    errors = validate_execution_result(data)
    if errors:
        raise ValueError("；".join(errors))
    return data


def validate_execution_result(data: dict) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return ["执行结果必须是对象"]
    if data.get("schemaVersion") != "1.0":
        errors.append("schemaVersion 必须为 1.0")
    if not isinstance(data.get("runId"), str) or not data.get("runId", "").strip():
        errors.append("runId 必须是非空字符串")
    if not isinstance(data.get("results"), list):
        errors.append("results 必须是数组")
        return errors
    seen: set[str] = set()
    for i, item in enumerate(data.get("results", [])):
        if not isinstance(item, dict):
            errors.append(f"results[{i}] 必须是对象")
            continue
        if not item.get("caseId"):
            errors.append(f"results[{i}] 缺少 caseId")
        elif str(item["caseId"]) in seen:
            errors.append(f"results[{i}] caseId 重复: {item['caseId']}")
        else:
            seen.add(str(item["caseId"]))
        if item.get("status") not in {"passed", "failed", "skipped", "error"}:
            errors.append(f"results[{i}] status 非法")
        if "durationMs" in item and (not isinstance(item["durationMs"], (int, float)) or item["durationMs"] < 0):
            errors.append(f"results[{i}] durationMs 非法")
        if "evidence" in item and not isinstance(item["evidence"], list):
            errors.append(f"results[{i}] evidence 必须是数组")
        if item.get("status") in {"failed", "error"} and not item.get("failureCategory"):
            errors.append(f"results[{i}] 失败结果缺少 failureCategory")
    return errors


def apply_execution_results(cases: list[dict], data: dict) -> list[dict]:
    """将最近一次执行状态附加到用例副本，不改变设计状态字段。"""
    errors = validate_execution_result(data)
    if errors:
        raise ValueError("；".join(errors))
    by_id = {str(x.get("caseId")): x for x in data.get("results", [])}
    case_ids = {str(c.get("id")) for c in cases}
    unknown = sorted(set(by_id) - case_ids)
    if unknown:
        raise ValueError("执行结果包含未知用例: " + ", ".join(unknown))
    output = []
    for case in cases:
        item = dict(case)
        result = by_id.get(str(case.get("id")))
        if result:
            item["lastExecution"] = {
                "runId": data.get("runId", ""),
                "status": result.get("status"),
                "durationMs": result.get("durationMs"),
                "failureCategory": result.get("failureCategory"),
                "evidence": result.get("evidence", []),
                "executedAt": result.get("executedAt"),
            }
        output.append(item)
    return output
