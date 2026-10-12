"""可复现性 L2 测试：结构指纹对文本变化不敏感；compare 对同源漂移/异源分类正确。"""
import json
from collections import defaultdict
from pathlib import Path

from prd2tc.reproduce import compare_fingerprints, compare_runs, run_fingerprint


def _req(module, idx, title):
    return {"id": f"REQ-{module}-{idx:03d}", "module": module, "title": title, "description": title,
            "acceptanceCriteria": [], "exceptionFlows": [], "design_method": "场景法", "priority": "P1"}


def _case(cid, module, req, title):
    return {"id": cid, "module": module, "title": title, "design_method": "场景法", "priority": "P1",
            "tags": ["@regression"], "requirement": [req],
            "steps": [{"action": "goto", "target": "/"}], "expected": [title]}


def build(root: Path, *, prd_hash="p1", prompt_hash="pt", config_hash="c",
          reqs=None, cases=None, gates=None, run_id="r1"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "run-manifest.json").write_text(json.dumps({
        "runId": run_id, "prdHash": prd_hash,
        "stages": {name: {"status": status} for name, status in (gates or {}).items()},
    }), encoding="utf-8")
    (root / "run-state.json").write_text(json.dumps({"promptHash": prompt_hash, "configHash": config_hash}), encoding="utf-8")
    analysis = root / "analysis"; analysis.mkdir(exist_ok=True)
    (analysis / "requirements.json").write_text(json.dumps({"schemaVersion": "2.0", "modules": [], "requirements": reqs or []}), encoding="utf-8")
    if cases is not None:
        cases_dir = root / "cases" / "cases"; cases_dir.mkdir(parents=True, exist_ok=True)
        by_module: dict[str, list] = defaultdict(list)
        for case in cases:
            by_module[case["module"]].append(case)
        for module in sorted(by_module):
            (cases_dir / f"{module}.json").write_text(json.dumps({"cases": by_module[module]}), encoding="utf-8")


def test_fingerprint_insensitive_to_free_text(tmp_path):
    """仅用例/需求措辞变化时，结构指纹应完全一致（复现 = 结构级，非字节级）。"""
    reqs_a = [_req("AUTH", 1, "用户登录功能说明一"), _req("AUTH", 2, "注册流程说明一")]
    reqs_b = [_req("AUTH", 1, "账号登录另一种措辞"), _req("AUTH", 2, "注册的另一种措辞")]
    cases_a = [_case("TC-AUTH-001", "AUTH", "REQ-AUTH-001", "验证登录成功一"), _case("TC-AUTH-002", "AUTH", "REQ-AUTH-002", "注册成功一")]
    cases_b = [_case("TC-AUTH-001", "AUTH", "REQ-AUTH-001", "登录成功的另一种说法"), _case("TC-AUTH-002", "AUTH", "REQ-AUTH-002", "注册成功的另一种说法")]
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    build(root_a, reqs=reqs_a, cases=cases_a)
    build(root_b, reqs=reqs_b, cases=cases_b)
    assert run_fingerprint(root_a) == run_fingerprint(root_b)
    report = compare_runs(root_a, root_b)
    assert report["sameSource"] is True and report["ok"] is True and report["drift"] == []


def test_compare_detects_structural_drift(tmp_path):
    """同源但某模块用例数变化 → ok=False 且漂移明细指出模块与数量。"""
    reqs = [_req("AUTH", 1, "登录"), _req("CART", 1, "购物车")]
    cases_a = [_case("TC-AUTH-001", "AUTH", "REQ-AUTH-001", "登录成功"), _case("TC-CART-001", "CART", "REQ-CART-001", "加购")]
    cases_b = [_case("TC-AUTH-001", "AUTH", "REQ-AUTH-001", "登录成功")]  # CART 用例缺失
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    build(root_a, reqs=reqs, cases=cases_a)
    build(root_b, reqs=reqs, cases=cases_b)
    report = compare_runs(root_a, root_b)
    assert report["sameSource"] is True
    assert report["ok"] is False
    modules = [item.get("module") for item in report["drift"] if "module" in item]
    assert "CART" in modules and "AUTH" not in modules


def test_compare_marks_different_source(tmp_path):
    """PRD hash 不同（源不同）→ 不算复现问题，单独归因 changedHashes。"""
    reqs = [_req("AUTH", 1, "登录")]
    cases = [_case("TC-AUTH-001", "AUTH", "REQ-AUTH-001", "登录成功")]
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    build(root_a, reqs=reqs, cases=cases, prd_hash="prd-A")
    build(root_b, reqs=reqs, cases=cases, prd_hash="prd-B")
    report = compare_runs(root_a, root_b)
    assert report["sameSource"] is False
    assert report["ok"] is False
    assert report["changedHashes"] == ["prd"]
