"""XMind（Zen / content.json）导出：纯标准库实现。

.xmind 本质是 ZIP 包，现代版本用 content.json 存储导图结构（XMind Zen 2017 起），
且当前版本仍能读取该格式，因此输出 JSON 覆盖面最广。
"""
from __future__ import annotations

import hashlib
import json
import warnings
from collections import defaultdict
from typing import Any

from prd2tc import __version__ as _TOOL_VERSION   # 避免写死版本随发布漂移
from prd2tc.export.common import EMPTY, build_zip_bytes

MAX_NODES = 3000

# 优先级 → XMind 内置 markers 的 markerId（渲染为优先级图标）。
# 必须走 markers；放进 labels 只会显示成 "priority-1" 这样的英文文本。
PRIORITY_MARKERS = {
    "P0": "priority-1",
    "P1": "priority-2",
    "P2": "priority-3",
    "P3": "priority-4",
}


class _IdFactory:
    """从语义内容派生确定性 topic id，保证同输入同输出（可复现性要求）。"""

    def __init__(self) -> None:
        self._taken: set[str] = set()

    def make(self, seed: str) -> str:
        candidate = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:26]
        index = 1
        while candidate in self._taken:      # 碰撞概率极低，仍做防御
            candidate = hashlib.sha1(f"{seed}#{index}".encode("utf-8")).hexdigest()[:26]
            index += 1
        self._taken.add(candidate)
        return candidate


def _notes_text(case: dict) -> str:
    """用例详情 → XMind 纯文本 notes。

    用 plain.content 而非 HTML：兼容性最好，且免去标签转义。
    """
    sections: list[str] = []
    sections.append(f"【用例编号】{case.get('id', EMPTY)}")
    sections.append(f"【优先级】{case.get('priority', EMPTY)}")
    if case.get("design_method"):
        sections.append(f"【设计方法】{case['design_method']}")
    tags = [str(t) for t in (case.get("tags") or [])]
    if tags:
        sections.append(f"【标签】{' '.join(tags)}")
    requirements = [str(r) for r in (case.get("requirement") or [])]
    if requirements:
        sections.append(f"【关联需求】{', '.join(requirements)}")
    if case.get("preconditions"):
        body = "\n".join(f"{i}. {t}" for i, t in enumerate(case["preconditions"], 1))
        sections.append(f"【前置条件】\n{body}")
    if case.get("testData"):
        sections.append("【测试数据】\n" + "\n".join(f"• {t}" for t in case["testData"]))
    steps = case.get("steps") or []
    if steps:
        lines = []
        for index, step in enumerate(steps, 1):
            if not isinstance(step, dict):
                lines.append(f"{index}. {step}")
                continue
            action = str(step.get("action") or "").strip()
            target = str(step.get("target") or "").strip()
            data = str(step.get("data") or "").strip()
            line = f"{index}. [{action}] {target}" if action else f"{index}. {target}"
            if data:
                line += f" ← {data}"
            lines.append(line)
        sections.append("【测试步骤】\n" + "\n".join(lines))
    if case.get("expected"):
        sections.append("【预期结果】\n" + "\n".join(f"• {t}" for t in case["expected"]))
    return "\n".join(sections)


def _step_text(index: int, step: Any) -> str:
    if not isinstance(step, dict):
        return f"{index}. {step}"
    action = str(step.get("action") or "")
    target = str(step.get("target") or "")
    data = str(step.get("data") or "")
    return f"{index}. [{action}] {target}" + (f" ← {data}" if data else "")


def _case_topic(case: dict, ids: _IdFactory, mode: str) -> dict:
    case_id = str(case.get("id") or EMPTY)
    topic: dict[str, Any] = {
        "id": ids.make(case_id),
        "class": "topic",
        "title": f"{case_id} {case.get('title', '')}".strip(),
        "notes": {"plain": {"content": _notes_text(case)}},
    }
    # 优先级走 markers（彩色优先级图标）；放 labels 只会显示成英文文本
    marker = PRIORITY_MARKERS.get(str(case.get("priority")))
    if marker:
        topic["markers"] = [{"markerId": marker}]
    # labels 只承载标签与设计方法（文本标签）
    labels = [str(t) for t in (case.get("tags") or [])]
    if case.get("design_method"):
        labels.append(str(case["design_method"]))
    if labels:
        topic["labels"] = labels

    if mode == "full":
        children = [
            {"id": ids.make(f"{case_id}#step#{i}"), "class": "topic", "title": _step_text(i, s)}
            for i, s in enumerate(case.get("steps") or [], 1)
        ]
        if children:
            topic["children"] = {"attached": children}
    return topic


def build_tree(cases: list[dict], ids: _IdFactory, mode: str,
               module_names: dict[str, str] | None = None) -> dict:
    """用例列表 → 模块 → 子模块 → 用例 的树。"""
    names = module_names or {}
    by_module: dict[str, list[dict]] = defaultdict(list)
    for case in cases:
        by_module[str(case.get("module") or "COMMON")].append(case)

    module_topics = []
    for module in sorted(by_module):
        display = f"{module} {names.get(module, '')}".strip()
        module_topic: dict[str, Any] = {
            "id": ids.make(f"module#{module}"),
            "class": "topic",
            "title": display,
        }
        if mode == "module":
            module_topic["notes"] = {"plain": {"content": f"共 {len(by_module[module])} 条用例"}}
            module_topics.append(module_topic)
            continue

        grouped: dict[str, list[dict]] = defaultdict(list)
        for case in by_module[module]:
            grouped[str(case.get("subModule") or "")].append(case)

        sub_topics = []
        for sub in sorted(grouped):
            case_topics = [_case_topic(c, ids, mode) for c in grouped[sub]]
            if not sub:
                sub_topics.extend(case_topics)   # 无子模块：扁平挂载
                continue
            sub_topics.append({
                "id": ids.make(f"module#{module}#sub#{sub}"),
                "class": "topic",
                "title": sub,
                "children": {"attached": case_topics},
            })
        if sub_topics:
            module_topic["children"] = {"attached": sub_topics}
        module_topics.append(module_topic)
    return {"id": ids.make("root"), "class": "topic", "title": "",
            "children": {"attached": module_topics}}


def _count_nodes(topic: dict) -> int:
    total = 1
    for child in (topic.get("children") or {}).get("attached") or []:
        total += _count_nodes(child)
    return total


def _pack(title: str, root: dict) -> bytes:
    """把导图树打包成 .xmind 字节流。"""
    sheet_id = "sheet-1"
    content = [{
        "id": sheet_id,
        "class": "sheet",
        "title": title,
        "rootTopic": root,
        "topicPositioning": "fixed",
    }]
    metadata = {
        "creator": {"name": "prd2tc", "version": _TOOL_VERSION},
        "activeSheetId": sheet_id,       # 部分 XMind 版本必需，缺失会提示损坏
    }
    manifest = {"file-entries": {
        "content.json": {}, "metadata.json": {}, "manifest.json": {}
    }}
    return build_zip_bytes({
        "content.json": json.dumps(content, ensure_ascii=False, indent=2),
        "metadata.json": json.dumps(metadata, ensure_ascii=False, indent=2),
        "manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2),
    })


def build_xmind_bytes(cases: list[dict], *, title: str = "测试用例集",
                      mode: str = "case",
                      module_names: dict[str, str] | None = None) -> bytes:
    """用例列表 → .xmind 字节流。纯函数，不写盘。

    mode: case（默认，展开到用例）/ full（到步骤）/ module（仅模块层）

    超限时**自动降级**为 module 模式并告警，而不是直接失败——
    保证任何规模都至少有一份可用导图。
    """
    if not cases:
        raise ValueError("没有用例可导出")
    # 降级链：请求的模式 → module（module 已是最粗粒度）
    chain = [mode] if mode == "module" else [mode, "module"]
    for attempt in chain:
        root = build_tree(cases, _IdFactory(), attempt, module_names)
        if _count_nodes(root) <= MAX_NODES:
            if attempt != mode:
                warnings.warn(
                    f"导图节点数超过 {MAX_NODES}，已从 {mode} 自动降级为 module 模式"
                    f"（仅模块层）。如需完整展开，请拆分用例集。",
                    stacklevel=2,
                )
            root["title"] = title
            return _pack(title, root)
    raise ValueError(f"即使降级为 module 模式，节点数仍超过 {MAX_NODES}，请拆分用例集")
