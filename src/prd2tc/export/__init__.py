"""用例导出：Excel（.xlsx）与 XMind（.xmind）。

两种格式均以标准库实现，保持项目「零第三方运行时依赖」定位
（见 README「技术栈」章节）。共同数据源为 `cases/cases/*.json`——
与 Markdown 评审稿同源，不读 Markdown，避免双份事实源。
"""
from __future__ import annotations

import json
from pathlib import Path

from prd2tc.export.common import atomic_write_bytes, build_zip_bytes, write_zip
from prd2tc.export.excel import build_xlsx_bytes, safe_sheet_name
from prd2tc.export.xmind import build_xmind_bytes

__all__ = ["export_cases", "build_xlsx_bytes", "build_xmind_bytes", "safe_sheet_name",
           "MEDIA_TYPES", "atomic_write_bytes", "build_zip_bytes", "write_zip"]

MEDIA_TYPES = {
    "excel": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xmind": "application/vnd.xmind.workbook",
}


def _load_module_names(root: Path | None) -> dict[str, str]:
    """从 analysis/requirements.json 读取模块中文名（失败则回退代号）。"""
    if root is None:
        return {}
    path = root / "analysis" / "requirements.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        str(m.get("id")): str(m.get("name") or "")
        for m in (data.get("modules") or []) if isinstance(m, dict)
    }


def _load_traceability(root: Path | None) -> dict | None:
    if root is None:
        return None
    path = root / "cases" / "traceability.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _load_requirements(root: Path | None) -> list[dict]:
    """读取结构化需求（筛选导出时用于**重算**追踪矩阵）。"""
    if root is None:
        return []
    path = root / "analysis" / "requirements.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data.get("requirements", []) if isinstance(data, dict) else []


def export_cases(cases_dir: str | Path, *, formats: list[str] | None = None,
                 out: str | Path | None = None, root: str | Path | None = None,
                 xmind_mode: str = "case", sheet_name: str = "用例明细",
                 include_traceability: bool = True,
                 product: str = "", version: str = "",
                 spec=None) -> dict:
    """从用例目录导出 Excel / XMind。

    - cases_dir：用例 JSON 目录（`output/<p>/<v>/cases/cases`）
    - root：运行根目录；提供时可读取 traceability.json 与模块中文名
    - out：输出文件路径（单格式）或目录（多格式）；默认写入 cases_dir 的父目录
    - product / version：影响文件名与导图标题；未给出时退化为「用例集」
    - spec：`casefilter.CaseFilter`。给出时**只导出命中的用例**，
      并按子集**重算**统计与追踪矩阵（详见下方说明）
    - 返回 `{"files": [Path, ...], "case_count": int, "filter": {...}}`
    """
    from prd2tc import casefilter
    from prd2tc.io import load_cases

    formats = formats or ["excel", "xmind"]
    all_cases = load_cases(cases_dir)
    if not all_cases:
        raise ValueError(f"用例目录为空或不存在: {cases_dir}")

    cases = casefilter.apply(all_cases, spec)
    if not cases:
        # 空结果**必须显式失败**：静默产出空文件会让用户以为「导出成功」，
        # 直到打开才发现没有内容（且筛选条件已经忘了）。
        raise ValueError(
            f"筛选后没有匹配的用例（共 {len(all_cases)} 条，条件：{spec.describe() if spec else '无'}）；"
            f"请放宽条件后重试")

    cases_path = Path(cases_dir)
    run_root = Path(root) if root else cases_path.parent.parent
    module_names = _load_module_names(run_root)
    filtered = bool(spec is not None and spec.active)
    if filtered:
        # **按子集重算**追踪矩阵。若沿用磁盘上的完整矩阵，
        # Excel 的「需求覆盖」页会声称覆盖了根本不在此文件里的用例 ——
        # 两个 sheet 自相矛盾，且这种矛盾不会被任何人发现。
        from prd2tc.traceability import build_traceability
        requirements = _load_requirements(run_root)
        traceability = build_traceability(requirements, cases) if requirements else None
    else:
        traceability = _load_traceability(run_root) if include_traceability else None
    # 统计概览 sheet 的数据源；与 traceability 无关，始终可用
    from prd2tc.io import stats as _stats
    try:
        summary = _stats(cases)
    except Exception:
        summary = None
    if summary is not None and filtered:
        # 在统计页显式标注「这是子集」——否则读者会把局部数字当成全量结论
        summary["scope"] = (f"筛选：{spec.describe()}（{len(cases)}/{len(all_cases)} 条，"
                            f"已排除 {len(all_cases) - len(cases)} 条）")

    suffix = spec.name_suffix() if filtered else ""
    title = f"测试用例集 {product} {version}".strip() or "测试用例集"
    if filtered:
        title += f"（{spec.describe()}）"
    base = cases_path.parent            # output/<p>/<v>/cases
    stem = "用例集" if not product else f"{product}-{version}-用例集"
    if suffix:
        stem = f"{stem}-{suffix}"

    produced: list[Path] = []
    if "excel" in formats:
        data = build_xlsx_bytes(cases, traceability=traceability, stats=summary,
                                sheet_name=sheet_name)   # 内部会 safe_sheet_name 清洗
        target = Path(out) if out and Path(out).suffix == ".xlsx" else base / f"{stem}.xlsx"
        produced.append(atomic_write_bytes(target, data))
    if "xmind" in formats:
        data = build_xmind_bytes(cases, title=title, mode=xmind_mode,
                                 module_names=module_names)
        target = Path(out) if out and Path(out).suffix == ".xmind" else base / f"{stem}.xmind"
        produced.append(atomic_write_bytes(target, data))

    return {"files": produced, "case_count": len(cases),
            "total_case_count": len(all_cases),
            "filter": casefilter.summarize(all_cases, spec)}
