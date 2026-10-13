"""用例步骤的**自然语言渲染**（确定性，无 LLM 依赖）。

为什么需要
----------
结构化步骤（`[goto] 智能体配置页 (page=config)`）是**给机器看的**：动作枚举 + 契约定位符，
便于门禁校验与自动化驱动。但把它直接放进 Excel / XMind / Markdown 给人评审时，
`[goto]`、`(page=config)` 这类标记严重妨碍阅读。

因此：**JSON 保留结构化步骤**（门禁与自动化用），**导出物使用自然语言**
（`打开「智能体配置页」`）。

为什么用确定性渲染而非让模型再写一套
------------------------------------
1. `target` 里**本就带中文元素名**（`智能体配置页 (page=config)`），动词映射即可成句，
   无需额外推理；
2. **不会与结构化步骤矛盾** —— 若由模型另写一套，两者可能不一致，
   而结构化那份才是门禁与执行的依据（此前的教训：两份描述一旦分叉就无法追责）；
3. 零 token 成本、零非确定性，`--repro` 下逐字可复现。
"""
from __future__ import annotations

import re
# target 末尾的括号标注（种类=取值 或 裸标识符）
_ANNOT_RE = re.compile(r"\s*\(([^()]*)\)\s*$")
# 无括号的尾随标注：模型有时写成 `知识库配置页 page=knowledge`
# （与 `(page=knowledge)` 等价，但少了括号）
_BARE_ANNOT_RE = re.compile(
    r"\s+(data-testid|id|class|page|tab|text|name|label|placeholder)"
    r"\s*=\s*(\S+)\s*$", re.I)

# 动作 → 渲染模板。`{t}` 元素名、`{d}` data。
# 断言类（expect*）统一加「验证」前缀，使评审稿能一眼区分「操作」与「检查点」。
_VERBS: dict[str, str] = {
    "goto": "打开{t}",
    "click": "点击{t}",
    "fill": "在{t}输入「{d}」",
    "selectOption": "在{t}选择「{d}」",
    "check": "勾选{t}",
    "uncheck": "取消勾选{t}",
    "hover": "悬停于{t}",
    "press": "在{t}按下「{d}」",
    "reload": "刷新页面",
    "upload": "在{t}上传「{d}」",
    "scrollTo": "滚动到{t}",
    "waitFor": "等待{t}",
}
_ASSERTIONS: dict[str, str] = {
    "expectVisible": "{t}可见",
    "expectHidden": "{t}不可见",
    "expectNotVisible": "{t}不可见",
    "expectText": "{t}显示「{d}」",
    "expectValue": "{t}的值为「{d}」",
    "expectEnabled": "{t}处于可用状态",
    "expectDisabled": "{t}处于禁用状态",
    "expectState": "{t}状态为「{d}」",
    "expectCount": "{t}数量为「{d}」",
    "expectUrl": "地址栏为「{d}」",
    "expectChecked": "{t}处于选中状态",
    "expectUnchecked": "{t}处于未选中状态",
}


def split_target(target: str) -> tuple[str, str, str]:
    """拆 target → (元素名, 种类, 取值)。

    `智能体配置页 (page=config)` → `('智能体配置页', 'page', 'config')`
    `文档名称输入框 (id=docName)` → `('文档名称输入框', 'id', 'docName')`
    `知识库配置页 page=knowledge` → `('知识库配置页', 'page', 'knowledge')`（无括号变体）
    `工具列表与详情区`            → `('工具列表与详情区', '', '')`
    """
    text = str(target or "").strip()
    m = _ANNOT_RE.search(text)
    if m:
        inner = m.group(1).strip()
        name = text[:m.start()].strip()
        if "=" in inner:
            kind, _, value = inner.partition("=")
            return (name or value.strip()), kind.strip(), value.strip()
        return (name or inner), "", inner
    # 无括号变体：`名称 page=knowledge`
    bare = _BARE_ANNOT_RE.search(text)
    if bare:
        name = text[:bare.start()].strip()
        kind, value = bare.group(1).lower(), bare.group(2).strip()
        return (name or value), kind, value
    return text, "", ""


def step_sentence(step: dict) -> str:
    """单步 → 自然语言。未知动作回退为「动作 元素」而非丢弃信息。"""
    if not isinstance(step, dict):
        return str(step)
    action = str(step.get("action") or "").strip()
    name, kind, value = split_target(step.get("target") or "")
    data = str(step.get("data") or "").strip()
    # 「暂无 data 的 fill」等：避免出现空引号
    if not data:
        tmpl = (_VERBS.get(action) or _ASSERTIONS.get(action) or "")
        tmpl = re.sub(r"[「(]?\{d\}[」)]?", "", tmpl).strip()
    else:
        tmpl = _VERBS.get(action) or _ASSERTIONS.get(action) or ""
    if not name:
        name = f"（{kind}={value}）" if kind else "当前页面"
    if tmpl:
        text = tmpl.replace("{t}", f"「{name}」").replace("{d}", data)
    else:
        # 未知动作：保留动作用原文，避免自然语言化时丢失语义
        text = f"{action or '操作'}「{name}」" + (f"：{data}" if data else "")
    if action in _ASSERTIONS:
        return f"验证{text}"
    return text


def render_steps_nl(steps: list[dict] | None, *, numbered: bool = True) -> list[str]:
    """步骤数组 → 自然语言行列表（可编号）。"""
    out: list[str] = []
    for i, step in enumerate(steps or [], 1):
        sentence = step_sentence(step)
        out.append(f"{i}. {sentence}" if numbered else sentence)
    return out


def render_steps_nl_text(steps: list[dict] | None, *, sep: str = "\n") -> str:
    """步骤数组 → 单块文本（Excel 单元格 / Markdown 段落用）。"""
    return sep.join(render_steps_nl(steps))


def render_steps_nl_inline(steps: list[dict] | None) -> str:
    """步骤数组 → 单行内联文本（XMind 节点等窄空间用）。"""
    return "；".join(s.rstrip("。") for s in render_steps_nl(steps, numbered=False)) + (
        "。" if steps else "")


# 自由文本里混入的定位符标注：模型有时把 `（page=config）`、`(id=modal)`
# 写进 preconditions / expected / testData。这在给人看的文档里同样是噪声
# （如「已进入智能体配置页（page=config）」）。
# 只剥离**带 `种类=` 的**标注 —— 裸中文括号多为正常补充说明，不应误删。
_LOCATOR_IN_PROSE_RE = re.compile(
    r"[（(]\s*(?:data-testid|id|class|page|tab|text|name|label|placeholder)"
    r"\s*=\s*[^()（）]*[)）]")
_EMPTY_PAREN_RE = re.compile(r"[（(]\s*[)）]")
_DOUBLE_SPACE_RE = re.compile(r"\s{2,}")


def strip_locators(text: str) -> str:
    """剥离自由文本里的定位符标注，保留其周围的中文表述。

    `已进入智能体配置页（page=config）` → `已进入智能体配置页`
    只影响**导出渲染**，JSON 原文保持不变（可追溯）。
    """
    s = str(text or "")
    if not s:
        return s
    out = _LOCATOR_IN_PROSE_RE.sub("", s)
    out = _EMPTY_PAREN_RE.sub("", out)              # 清掉被掏空的括号
    out = _DOUBLE_SPACE_RE.sub(" ", out)
    out = re.sub(r"\s*[，,；;]\s*$", "", out).strip()
    # 若剥离后什么都没了，宁可保留原文（不产出空字段）
    return out or s


def clean_items(values: list | None) -> list[str]:
    """字符串数组 → 剥离定位符后的字符串数组（导出用）。"""
    return [strip_locators(v) for v in (values or []) if str(v).strip()]
