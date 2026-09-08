"""需求版本 Diff：确定性匹配优先，模型语义判断作为后续扩展点。"""
from __future__ import annotations

import hashlib
import re


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").lower())


def fingerprint(req: dict) -> str:
    acceptance = req.get("acceptanceCriteria") or [req.get("description", "")]
    exceptions = req.get("exceptionFlows") or []
    body = "|".join(_norm(str(value)) for value in (req.get("module", ""), req.get("title", ""), acceptance, exceptions))
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def semantic_fingerprint(req: dict) -> str:
    """忽略 REQ 编号和模块代号，用于识别仅编号变化的同一需求。"""
    acceptance = req.get("acceptanceCriteria") or [req.get("description", "")]
    exceptions = req.get("exceptionFlows") or []
    body = "|".join(_norm(str(value)) for value in (req.get("title", ""), acceptance, exceptions))
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def requirements_from_prd(text: str) -> list[dict]:
    """从含显式 REQ 的 Markdown PRD 提取稳定比较字段。"""
    result = []
    for rid in dict.fromkeys(re.findall(r"REQ-[A-Z0-9_]+-\d{3}", text)):
        line = next((x.strip() for x in text.splitlines() if rid in x), rid)
        cells = [c.strip() for c in line.strip("|").split("|")] if "|" in line else []
        has_priority = len(cells) > 2 and cells[0] == rid and cells[1] in {"P0", "P1", "P2", "P3"}
        title_index = 2 if has_priority else 1
        acceptance_index = 3 if has_priority else 2
        exception_index = 4 if has_priority else 3
        title = cells[title_index] if len(cells) > title_index and cells[0] == rid else line.replace(rid, "").strip(" |:-")
        acceptance = [cells[acceptance_index]] if len(cells) > acceptance_index and cells[0] == rid and cells[acceptance_index] else []
        exceptions = [cells[exception_index]] if len(cells) > exception_index and cells[0] == rid and cells[exception_index] else []
        result.append({"id": rid, "module": rid.split("-")[1], "title": title,
                       "description": title, "acceptanceCriteria": acceptance, "exceptionFlows": exceptions,
                       "priority": cells[1] if has_priority else "P1"})
    return result


def diff_requirements(old: list[dict], new: list[dict]) -> dict:
    old_by_id = {str(x.get("id")): x for x in old if x.get("id")}
    new_by_id = {str(x.get("id")): x for x in new if x.get("id")}
    result = {"schemaVersion": "1.0", "added": [], "modified": [], "removed": [], "unchanged": []}
    matched_old: set[str] = set()
    for rid, current in new_by_id.items():
        previous = old_by_id.get(rid)
        if previous is not None:
            matched_old.add(rid)
            if fingerprint(previous) == fingerprint(current):
                result["unchanged"].append({"id": rid, "reason": "id/fingerprint unchanged"})
            else:
                result["modified"].append({"id": rid, "before": previous, "after": current, "reason": "same id, content changed"})
            continue
        # ID 改变但语义相同：保留旧 ID 的关联提示，避免误判删除+新增。
        same = next((o for oid, o in old_by_id.items()
                     if oid not in matched_old and (fingerprint(o) == fingerprint(current)
                                                    or semantic_fingerprint(o) == semantic_fingerprint(current))), None)
        if same:
            stable_id = str(same.get("id"))
            matched_old.add(stable_id)
            result["unchanged"].append({"id": stable_id, "incomingId": rid,
                                        "reason": "semantic match; preserve previous id"})
        else:
            result["added"].append({"id": rid, "after": current, "reason": "new requirement"})
    for rid, previous in old_by_id.items():
        if rid not in matched_old and rid not in new_by_id:
            result["removed"].append({"id": rid, "before": previous, "reason": "missing from new requirements"})
    result["summary"] = {k: len(result[k]) for k in ("added", "modified", "removed", "unchanged")}
    return result


def merge_incremental_cases(old_cases: list[dict], new_cases: list[dict], diff: dict) -> dict:
    """按需求 Diff 合并用例；历史用例进入 history，绝不物理丢弃。"""
    modified = {str(x.get("id")) for x in diff.get("modified", [])}
    removed = {str(x.get("id")) for x in diff.get("removed", [])}
    active: list[dict] = []
    history: list[dict] = []
    for source in old_cases:
        case = dict(source)
        refs = {str(x) for x in case.get("requirement", [])}
        if refs and refs <= removed:
            case["status"] = "deprecated"
            history.append(case)
        elif refs & modified or refs & removed:
            case["status"] = "needs_review"
            history.append(case)
        else:
            case["status"] = "active"
            active.append(case)

    new_ids = {str(c.get("id")) for c in new_cases}
    new_refs = {str(r) for c in new_cases for r in c.get("requirement", [])}
    kept = []
    for case in active:
        refs = {str(x) for x in case.get("requirement", [])}
        if str(case.get("id")) in new_ids or refs & new_refs:
            superseded = dict(case)
            superseded["status"] = "superseded"
            history.append(superseded)
        else:
            kept.append(case)
    for source in new_cases:
        case = dict(source)
        case["status"] = "active"
        kept.append(case)
    return {"schemaVersion": "1.0", "cases": kept, "history": history,
            "summary": {"active": len(kept), "history": len(history)}}
