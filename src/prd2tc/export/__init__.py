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


def export_cases(cases_dir: str | Path, *, formats: list[str] | None = None,
                 out: str | Path | None = None, root: str | Path | None = None,
                 xmind_mode: str = "case", sheet_name: str = "用例明细",
                 include_traceability: bool = True,
                 product: str = "", version: str = "") -> dict:
    """从用例目录导出 Excel / XMind。

    - cases_dir：用例 JSON 目录（`output/<p>/<v>/cases/cases`）
    - root：运行根目录；提供时可读取 traceability.json 与模块中文名
    - out：输出文件路径（单格式）或目录（多格式）；默认写入 cases_dir 的父目录
    - product / version：影响文件名与导图标题；未给出时退化为「用例集」
    - 返回 `{"files": [Path, ...], "case_count": int}`
    """
    from prd2tc.io import load_cases

    formats = formats or ["excel", "xmind"]
    cases = load_cases(cases_dir)
    if not cases:
        raise ValueError(f"用例目录为空或不存在: {cases_dir}")

    cases_path = Path(cases_dir)
    run_root = Path(root) if root else cases_path.parent.parent
    module_names = _load_module_names(run_root)
    traceability = _load_traceability(run_root) if include_traceability else None
    # 统计概览 sheet 的数据源；与 traceability 无关，始终可用
    from prd2tc.io import stats as _stats
    try:
        summary = _stats(cases)
    except Exception:
        summary = None

    title = f"测试用例集 {product} {version}".strip() or "测试用例集"
    base = cases_path.parent            # output/<p>/<v>/cases
    stem = "用例集" if not product else f"{product}-{version}-用例集"

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

    return {"files": produced, "case_count": len(cases)}
