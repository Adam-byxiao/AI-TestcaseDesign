"""产物写入：目录初始化、用例集 Markdown 渲染、统计。"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from prd2tc import __version__
from prd2tc.models import RunManifest
from prd2tc.nlsteps import clean_items, render_steps_nl
from prd2tc.schema import envelope, normalize_cases

OUT_STAGES = {"analysis": "需求分析报告.md", "plan": "测试方案.md", "cases": "用例集.md"}
MANIFEST_NAME = "run-manifest.json"
LEGACY_MANIFEST_NAME = "manifest.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def atomic_write_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        # Windows 上 os.replace 会因目标文件正被其他线程读取而抛 PermissionError
        # （实时心跳与状态轮询会并发读写同一文件）。短暂重试即可让读者让出，避免偶发失败。
        for attempt in range(5):
            try:
                os.replace(temp_name, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        if os.path.exists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                pass
    return path


def init_output(root: Path, product: str, version: str, prd: str | None) -> Path:
    """创建输出骨架，并同时维护权威清单与旧文件名兼容副本。"""
    for sub in ("analysis", "plan", "cases/cases"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    manifest = RunManifest(toolVersion=__version__, product=product, version=version, prd=prd,
                           runId=f"{product}-{version}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
                           createdAt=now_iso(), updatedAt=now_iso()).to_dict()
    manifest_path = root / MANIFEST_NAME
    serialized = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(manifest_path, serialized)
    atomic_write_text(root / LEGACY_MANIFEST_NAME, serialized)
    return manifest_path


def update_manifest(root: Path, **updates) -> dict:
    path = root / MANIFEST_NAME
    legacy_path = root / LEGACY_MANIFEST_NAME
    source = path if path.exists() else legacy_path
    current = json.loads(source.read_text(encoding="utf-8")) if source.exists() else {}
    current.update(updates)
    current["updatedAt"] = now_iso()
    serialized = json.dumps(current, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(path, serialized)
    atomic_write_text(legacy_path, serialized)
    return current


def manifest_stage(root: Path, name: str, **values) -> dict:
    """原子合并单个阶段记录，避免调用方覆盖其他阶段。"""
    path = root / MANIFEST_NAME
    legacy_path = root / LEGACY_MANIFEST_NAME
    source = path if path.exists() else legacy_path
    current = json.loads(source.read_text(encoding="utf-8")) if source.exists() else {}
    stages = dict(current.get("stages", {}))
    stage = dict(stages.get(name, {}))
    stage.update(values)
    stages[name] = stage
    return update_manifest(root, stages=stages)


def update_run_state(root: Path, **updates) -> dict:
    path = root / "run-state.json"
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"schemaVersion": "1.0"}
    current.update(updates)
    current["updatedAt"] = now_iso()
    atomic_write_text(path, json.dumps(current, ensure_ascii=False, indent=2) + "\n")
    return current


def write_stage(root: Path, stage: str, content: str) -> Path:
    """写阶段产物：analysis/plan 下为 md，cases 下为用例集 md"""
    filename = OUT_STAGES[stage]
    path = root / stage / filename
    atomic_write_text(path, content)
    return path


def save_cases(cases_dir: Path, cases: list[dict]) -> list[Path]:
    """按模块拆分写入 <MODULE>.json"""
    by_module: dict[str, list[dict]] = defaultdict(list)
    for source in cases:
        c = dict(source)
        c.setdefault("status", "active")
        by_module[c["module"]].append(c)
    paths = []
    for mod in sorted(by_module):
        p = cases_dir / f"{mod}.json"
        atomic_write_text(p, json.dumps(envelope(by_module[mod]), ensure_ascii=False, indent=2) + "\n")
        paths.append(p)
    return paths


def load_cases(cases_dir: str | Path) -> list[dict]:
    directory = Path(cases_dir)
    if not directory.is_dir():
        raise FileNotFoundError(f"用例目录不存在: {directory}")
    cases: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        cases.extend(normalize_cases(json.loads(path.read_text(encoding="utf-8").lstrip("\ufeff"))))
    return cases


def save_requirements(path: Path, requirements: list[dict], modules: list[dict] | None = None) -> Path:
    data = {"schemaVersion": "2.0", "modules": modules or [], "requirements": requirements}
    return atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def load_requirements(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("requirements", []) if isinstance(data, dict) else []


def save_traceability(path: Path, matrix: dict) -> Path:
    return atomic_write_text(path, json.dumps(matrix, ensure_ascii=False, indent=2) + "\n")


def artifact_inventory(root: Path) -> list[dict[str, str]]:
    artifacts = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative in {MANIFEST_NAME, LEGACY_MANIFEST_NAME, "run-state.json"} or "/.checkpoints/" in f"/{relative}":
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        artifacts.append({"path": relative, "sha256": digest})
    return artifacts


def stats(cases: list[dict]) -> dict:
    by_module = Counter(c["module"] for c in cases)
    by_priority = Counter(c["priority"] for c in cases)
    tags: Counter[str] = Counter()
    reqs: set[str] = set()
    for c in cases:
        for t in c.get("tags", []):
            tags[t] += 1
        reqs.update(c.get("requirement", []))
    return {
        "total": len(cases),
        "by_module": dict(sorted(by_module.items())),
        "by_priority": dict(sorted(by_priority.items())),
        "by_tag": dict(tags.most_common()),
        "req_covered": len(reqs),
    }


def render_cases_md(cases: list[dict], plan_title: str = "") -> str:
    """生成用例集.md 评审稿（统计 + traceability + 按模块明细表）"""
    s = stats(cases)
    lines = [
        f"# 用例集（评审稿）{'：' + plan_title if plan_title else ''}",
        "",
        "> 由 prd2tc 自动生成，字段规范见 docs/case-template.md；机读源为 cases/cases/*.json。",
        "",
        "## 1. 统计概览",
        "",
        f"- 总数：{s['total']}",
        f"- 按优先级：{s['by_priority']}",
        f"- 按模块：{s['by_module']}",
        f"- 覆盖 REQ 数：{s['req_covered']}",
        "",
        "## 2. 需求回溯表（Traceability）",
        "",
        "| REQ | 覆盖用例 |",
        "| --- | --- |",
    ]
    req_map: dict[str, list[str]] = defaultdict(list)
    for c in cases:
        for r in c.get("requirement", []):
            req_map[r].append(c["id"])
    for r in sorted(req_map):
        lines.append(f"| {r} | {', '.join(req_map[r])} |")

    lines += ["", "## 3. 用例明细（按模块）", ""]
    by_module: dict[str, list[dict]] = defaultdict(list)
    for c in cases:
        by_module[c["module"]].append(c)
    for mod in sorted(by_module):
        lines.append(f"### {mod}")
        lines.append("")
        lines.append("| 用例 | 标题 | 优先级 | 标签 |")
        lines.append("| --- | --- | --- | --- |")
        for c in by_module[mod]:
            tags = " ".join(c.get("tags", []))
            lines.append(f"| {c['id']} | {c['title']} | {c['priority']} | {tags} |")
        lines.append("")

    # 逐条展开：步骤用**自然语言**呈现（结构化原文保留在 cases/cases/*.json）
    lines += ["## 4. 用例步骤（自然语言）", "",
              "> 便于人工评审；机器可读的结构化步骤见 `cases/cases/*.json`（含 action 枚举与契约定位符）。", ""]
    for mod in sorted(by_module):
        for c in by_module[mod]:
            reqs = "、".join(c.get("requirement") or []) or "—"
            lines.append(f"#### {c['id']} {c.get('title', '')}")
            lines.append("")
            lines.append(f"- **关联需求**：{reqs}")
            lines.append(f"- **优先级**：{c.get('priority', '—')}"
                         f"　**设计方法**：{c.get('design_method', '—')}")
            pre = c.get("preconditions") or []
            if pre:
                lines.append("- **前置条件**：" + "；".join(clean_items(pre)))
            data = c.get("testData") or []
            if data:
                lines.append("- **测试数据**：" + "；".join(clean_items(data)))
            lines.append("")
            # numbered=False：`render_steps_nl` 自带序号时不加前缀，否则会出现「1. 1. …」
            for sentence in render_steps_nl(c.get("steps"), numbered=False):
                lines.append(f"- {sentence}")
            lines.append("")
            exp = c.get("expected") or []
            if exp:
                lines.append("**预期结果**")
                lines.append("")
                for e in clean_items(exp):
                    lines.append(f"- {e}")
                lines.append("")
    return "\n".join(lines)
