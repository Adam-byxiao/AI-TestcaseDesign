"""资源加载：PRD 读取、prompts/templates 加载（仓库内优先，安装包兜底）。"""
from __future__ import annotations

import importlib.resources as resources
from pathlib import Path

PACKAGE = "prd2tc"
ASSETS_DIR = "assets"

# 仓库内源文件（与 DSH skill 变体共用，便于直接编辑）
_REPO_ROOT = Path(__file__).resolve().parents[2]  # 01-AI-TestCaseDesign/
REPO_PROMPTS = _REPO_ROOT / "prompts"
REPO_TEMPLATES = _REPO_ROOT / "templates"

STAGES = {
    "1": ("01-prd-analysis.md", "需求分析报告.md"),
    "2": ("02-test-plan.md", "测试方案.md"),
    "3": ("03-test-cases.md", "用例集.md"),
}


def load_asset(relative: str) -> str:
    """优先读仓库内源文件（开发态），否则读包内资源（安装态）。"""
    candidates: list[Path | None] = []
    rel = Path(relative)
    if rel.parts[0] == "prompts":
        candidates.append(REPO_PROMPTS / rel.parts[1])
    elif rel.parts[0] == "templates":
        candidates.append(REPO_TEMPLATES / rel.parts[1])
    for p in candidates:
        if p and p.is_file():
            return p.read_text(encoding="utf-8")
    try:
        return resources.files(PACKAGE).joinpath(ASSETS_DIR, relative).read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise FileNotFoundError(f"资源不存在: {relative}") from e


def load_prd(path: str | Path) -> str:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"PRD 文件不存在: {p}")
    return p.read_text(encoding="utf-8")
