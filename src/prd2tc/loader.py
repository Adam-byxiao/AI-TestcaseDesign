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


def load_bundle(prd, *, extra_dirs: list | None = None, warnings: list | None = None):
    """把 PRD 输入归一化为 `ingest.Bundle`（正文 + 资产 + 来源清单）。

    与 `load_prd` 的关系：`load_prd` 只读**单个 UTF-8 文本文件**，
    因此 `.docx` / `.pdf` / `.zip` / 图片 / 多文件这些真实 PRD 的常见形态全都进不来
    （上传层曾直接 400 拒绝）。本函数是生成侧的统一入口。

    接受：
      - 单个文件路径（`.md/.txt/.docx/.pdf/图片` 均可）；
      - 目录（递归，保留相对层级用于主文件判定）；
      - 路径 / `(name, bytes)` / `(name, bytes, origin, relpath)` 的列表
        （多文件、混合格式；末者可还原**文件夹上传**的层级）；
      - `extra_dirs`：额外资源目录（`--images-dir` 的兼容入口）。

    单文件时**自动带上同级约定资源目录**（如 `prd.assets/`），
    对应「PRD 下载为 md、图片另存同目录」的常见形态。
    """
    from prd2tc import images as _images
    from prd2tc import ingest

    raw = list(prd) if isinstance(prd, (list, tuple)) else [prd]
    inputs = [x for x in raw if x is not None]
    if not inputs:
        raise ValueError("未提供 PRD 输入（--prd 为空）")
    # 用户显式给出的第一个**文件**即主文件（PRD）—— 这是最强信号。
    # 不能只靠「最长的 md」：实测附件 rag-service.md（43KB）比真正 PRD（17KB）更长，
    # 按长度会把附件当正文、PRD 降级为附件。
    preferred = ""
    for x in inputs:
        if isinstance(x, (str, Path)) and Path(x).is_file():
            preferred = Path(x).name
            break
    if len(inputs) == 1:
        p = Path(inputs[0])
        if p.is_file():
            # candidate_dirs 首项是文件所在目录本身，需排除，否则会把整目录吞进来
            inputs = inputs + [d for d in _images.candidate_dirs(p) if d != p.parent]
    inputs = inputs + [d for d in (extra_dirs or []) if d is not None]
    return ingest.ingest(inputs, warnings=warnings, preferred_primary=preferred,
                         base_dir=_common_base(inputs))


def _common_base(inputs) -> Path | None:
    """求多个**磁盘路径**的共同父目录，用于计算相对层级（depth）。

    为什么需要：从磁盘读取时，仅凭绝对路径无法判断谁更「浅」——
    运行产物里 `prds/<id>/我的PRD/PRD.md` 与
    `prds/<id>/我的PRD/图片和附件/rag-service.md` 必须被识别为深度 0 与 1，
    否则「子目录里的长附件」会与 PRD 同级并可能抢占正文。

    只对路径输入计算；纯内存上传（`(name, bytes, ...)`）自带 relpath，不依赖此值。
    """
    paths = []
    for x in inputs:
        if isinstance(x, (str, Path)):
            p = Path(x)
            try:
                paths.append(p.resolve() if p.is_dir() else p.resolve().parent)
            except OSError:
                continue
    if len(paths) < 2:
        return None
    try:
        import os
        return Path(os.path.commonpath([str(p) for p in paths]))
    except (ValueError, OSError):
        return None
