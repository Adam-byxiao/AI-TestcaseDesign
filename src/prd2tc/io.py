"""产物写入：目录初始化、用例集 Markdown 渲染、统计。"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

OUT_STAGES = {"analysis": "需求分析报告.md", "plan": "测试方案.md", "cases": "用例集.md"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_output(root: Path, product: str, version: str, prd: str | None) -> Path:
    """创建 output/<product>/<version>/ 骨架并写 manifest.json"""
    for sub in ("analysis", "plan", "cases/cases"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    manifest = {"product": product, "version": version, "prd": prd, "createdAt": now_iso()}
    manifest_path = root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest_path


def write_stage(root: Path, stage: str, content: str) -> Path:
    """写阶段产物：analysis/plan 下为 md，cases 下为用例集 md"""
    filename = OUT_STAGES[stage]
    path = root / stage / filename
    path.write_text(content, encoding="utf-8")
    return path


def save_cases(cases_dir: Path, cases: list[dict]) -> list[Path]:
    """按模块拆分写入 <MODULE>.json"""
    by_module: dict[str, list[dict]] = defaultdict(list)
    for c in cases:
        by_module[c["module"]].append(c)
    paths = []
    for mod in sorted(by_module):
        p = cases_dir / f"{mod}.json"
        p.write_text(
            json.dumps(by_module[mod], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        paths.append(p)
    return paths


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
    return "\n".join(lines)
