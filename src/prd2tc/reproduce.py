"""运行结构指纹与轮间漂移报告（可复现性 L2，方案见 docs/reproducibility.md）。

字节级复现受 LLM 采样限制不可承诺；结构级复现（模块集合、每模块 REQ/用例数、
覆盖率、门禁结果）是可达且可度量的契约。本模块把一次运行的结构性不变项固化为
指纹（排除自由文本），并提供两轮对拍：
  - 源不同（prd/prompt/config hash 任一不同）→ 不属于复现问题，单独归因；
  - 源相同但结构指纹漂移 → 归因 LLM 采样，输出漂移明细。
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from prd2tc.io import LEGACY_MANIFEST_NAME, MANIFEST_NAME, load_cases, load_requirements


def _json(path: Path, default=None):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default if default is not None else {}


def _manifest(root: Path) -> dict:
    path = root / MANIFEST_NAME if (root / MANIFEST_NAME).exists() else root / LEGACY_MANIFEST_NAME
    return _json(path)


def run_fingerprint(root: str | Path) -> dict:
    """固化一次运行的结构指纹：hash（源标识）+ 每模块 REQ/用例数 + 覆盖率 + 门禁。"""
    root = Path(root)
    data = _manifest(root)
    state = _json(root / "run-state.json")
    requirements = load_requirements(root / "analysis" / "requirements.json")
    cases: list[dict] = []
    cases_dir = root / "cases" / "cases"
    if cases_dir.is_dir():
        cases = load_cases(cases_dir)

    req_by_module = Counter(str(r.get("module")) for r in requirements if r.get("module") and r.get("id"))
    req_ids = {str(r.get("id")) for r in requirements if r.get("id")}
    case_by_module = Counter(str(c.get("module")) for c in cases if c.get("module"))
    covered = {str(ref) for c in cases for ref in (c.get("requirement") or [])} & req_ids
    modules = sorted(set(req_by_module) | set(case_by_module))
    gates = {name: str(value.get("status", "")) for name, value in (data.get("stages") or {}).items()}
    return {
        "schemaVersion": "1.0",
        "runId": data.get("runId"),
        "hashes": {
            "prd": str(data.get("prdHash", "")),
            "prompt": str(state.get("promptHash", "")),
            "config": str(state.get("configHash", "")),
        },
        "modules": {m: {"requirements": int(req_by_module.get(m, 0)),
                        "cases": int(case_by_module.get(m, 0))} for m in modules},
        "reqTotal": int(sum(req_by_module.values())),
        "reqCovered": int(len(covered)),
        "caseTotal": int(len(cases)),
        "gates": gates,
    }


def compare_fingerprints(a: dict, b: dict) -> dict:
    """对拍两份指纹。返回 ok / sameSource / changedHashes / drift 明细。"""
    hashes_a, hashes_b = a.get("hashes", {}), b.get("hashes", {})
    same_source = hashes_a == hashes_b
    changed_hashes = [k for k in ("prd", "prompt", "config") if hashes_a.get(k) != hashes_b.get(k)]
    drift: list[dict] = []
    if same_source:
        modules_a, modules_b = a.get("modules", {}), b.get("modules", {})
        for module in sorted(set(modules_a) | set(modules_b)):
            x, y = modules_a.get(module, {}), modules_b.get(module, {})
            if x != y:
                drift.append({"module": module, "before": x, "after": y})
        for metric in ("reqTotal", "reqCovered", "caseTotal"):
            if a.get(metric) != b.get(metric):
                drift.append({"metric": metric, "before": a.get(metric), "after": b.get(metric)})
        if a.get("gates") != b.get("gates"):
            drift.append({"metric": "gates", "before": a.get("gates"), "after": b.get("gates")})
    return {
        "ok": bool(same_source and not drift),
        "sameSource": bool(same_source),
        "changedHashes": changed_hashes,
        "drift": drift,
    }


def compare_runs(root_a: str | Path, root_b: str | Path) -> dict:
    """加载两份运行并返回对拍报告。"""
    a, b = run_fingerprint(root_a), run_fingerprint(root_b)
    comparison = compare_fingerprints(a, b)
    comparison["a"] = a
    comparison["b"] = b
    return comparison


def render_report(report: dict) -> str:
    """把对拍报告渲染为可读文本。"""
    a, b = report["a"], report["b"]
    lines = []
    lines.append(f"运行 A: {a.get('runId')}（req={a.get('reqTotal')} cases={a.get('caseTotal')} 覆盖={a.get('reqCovered')}）")
    lines.append(f"运行 B: {b.get('runId')}（req={b.get('reqTotal')} cases={b.get('caseTotal')} 覆盖={b.get('reqCovered')}）")
    lines.append(f"源（prd/prompt/config）：{'一致' if report['sameSource'] else '不一致: ' + ','.join(report['changedHashes'])}")
    if report["sameSource"] and not report["drift"]:
        lines.append("✓ 复现：结构指纹完全一致（模块/需求/用例数/覆盖率/门禁均无漂移）")
        return "\n".join(lines)
    lines.append("结构漂移明细：")
    for item in report["drift"]:
        lines.append(f"  - {item.get('metric') or item.get('module')}: {item.get('before')} → {item.get('after')}")
    if not report["sameSource"]:
        lines.append("  注：源不同属正常变更（PRD/Prompt/参数变了），非采样漂移，不算复现失败。")
    return "\n".join(lines)
