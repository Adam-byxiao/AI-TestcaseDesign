"""需求和用例的双向追踪矩阵。"""
from __future__ import annotations


def build_traceability(requirements: list[dict], cases: list[dict], *, min_cases: int = 1) -> dict:
    req_ids = [str(r.get("id")) for r in requirements if r.get("id")]
    req_map = {rid: [] for rid in req_ids}
    tc_map: dict[str, list[str]] = {}
    unknown: list[dict] = []
    duplicates: list[dict] = []
    for case in cases:
        if case.get("status") in {"deprecated", "superseded"}:
            continue
        cid = str(case.get("id", ""))
        refs = [str(x) for x in (case.get("requirement") or [])]
        for rid in set(refs):
            if refs.count(rid) > 1:
                duplicates.append({"caseId": cid, "requirement": rid})
        tc_map[cid] = refs
        for rid in refs:
            if rid not in req_map:
                unknown.append({"caseId": cid, "requirement": rid})
            else:
                req_map[rid].append(cid)
    details = []
    by_id = {str(r.get("id")): r for r in requirements}
    for rid in req_ids:
        count = len(req_map[rid])
        priority = by_id[rid].get("priority", "P1")
        status = "excluded" if by_id[rid].get("excluded") else ("covered" if count >= min_cases else ("insufficient" if count else "uncovered"))
        details.append({"id": rid, "module": by_id[rid].get("module", ""), "priority": priority, "caseIds": req_map[rid], "status": status})
    covered = sum(d["status"] == "covered" for d in details)
    insufficient = sum(d["status"] == "insufficient" for d in details)
    uncovered = sum(d["status"] == "uncovered" for d in details)
    excluded = sum(d["status"] == "excluded" for d in details)
    eligible = len(details) - excluded
    return {
        "schemaVersion": "1.0",
        "requirements": details,
        "caseToRequirements": [{"caseId": k, "requirementIds": v} for k, v in sorted(tc_map.items())],
        "unknownReferences": unknown,
        "duplicateMappings": duplicates,
        "summary": {"total": len(details), "covered": covered, "insufficient": insufficient,
                     "uncovered": uncovered, "excluded": excluded,
                     "coverageRate": round((covered + insufficient) / eligible * 100, 1) if eligible else 0},
    }


def validate_traceability(matrix: dict, *, fail_uncovered_p0: bool = True) -> list[str]:
    errors = [f"用例 {x['caseId']} 引用了不存在的需求 {x['requirement']}" for x in matrix.get("unknownReferences", [])]
    errors.extend(f"用例 {x['caseId']} 重复映射需求 {x['requirement']}" for x in matrix.get("duplicateMappings", []))
    for req in matrix.get("requirements", []):
        if req.get("status") == "uncovered" and (fail_uncovered_p0 and req.get("priority") == "P0"):
            errors.append(f"P0 需求 {req.get('id')} 没有 active 用例覆盖")
    return errors
