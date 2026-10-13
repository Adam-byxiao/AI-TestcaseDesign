"""动作词表：**单一事实源**。

为什么需要独立模块
------------------
动作枚举原先散落在 6 处：`schema.py`（权威）、`stages.py` 注入的提示词、
`prompts/04-case-constraints.md`、`docs/case-spec.md`、`docs/pipeline.md`、
`scripts/orchestrate.mjs`。任一处的改动都可能与其它处漂移。

而词表缺口有**实测代价**：评测 v1/v2 中 `check` 的误用率高达 **87%~91%**，
逐条枚举后的性质是「没有更合适的动作可用」——
- 元素状态/位置 23 次（`expectDisabled`/`expectState` 不存在）
- 拖拽上传 9 次（`upload` 不存在）
- 网络/数量 5 次（`expectCount` 不存在）
- 排他/反向 4 次（`expectNotVisible` 不存在）

即：`check` 是模型在词表缺口下**唯一听起来像「验证」**的词。
补齐词表比反复强调「不要滥用 check」更有效。

设计
----
`ACTION_SPECS` 是唯一权威；`schema.ACTIONS_BY_VERSION` 由它派生，
提示词文本由 `render_actions_for_prompt()` 生成（不再手写，杜绝漂移）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionSpec:
    """单个动作的元数据。"""
    name: str
    kind: str            # "operate"（改变系统状态） | "assert"（判定通过/失败）
    summary: str         # 一句话中文说明（注入提示词，供模型选词）
    needs_data: bool = False   # 是否通常需要 data（输入值/期望值）


# ---- 操作类：改变系统状态 ----
_OPERATIONS: tuple[ActionSpec, ...] = (
    ActionSpec("goto", "operate", "打开/切换到某页面"),
    ActionSpec("fill", "operate", "在输入框填写内容", needs_data=True),
    ActionSpec("click", "operate", "点击元素"),
    ActionSpec("selectOption", "operate", "在下拉/选择器中选择选项", needs_data=True),
    ActionSpec("check", "operate", "勾选复选框/单选（**仅用于可勾选元素**）"),
    ActionSpec("uncheck", "operate", "取消勾选复选框"),
    ActionSpec("upload", "operate", "上传文件（点击选择或拖拽到上传区）", needs_data=True),
    ActionSpec("hover", "operate", "鼠标悬停在元素上"),
    ActionSpec("press", "operate", "在元素上按键（如 Enter/Esc）", needs_data=True),
    ActionSpec("reload", "operate", "刷新页面"),
    ActionSpec("scrollTo", "operate", "滚动到元素位置"),
    ActionSpec("waitFor", "operate", "等待元素出现或状态就绪（**不是断言**，其后仍需 expect*）"),
)

# ---- 断言类：判定通过/失败。每条用例**至少**要有 1 个 ----
_ASSERTIONS: tuple[ActionSpec, ...] = (
    ActionSpec("expectVisible", "assert", "断言元素可见"),
    ActionSpec("expectNotVisible", "assert", "断言元素不可见/不存在（含关闭弹窗、排他）"),
    ActionSpec("expectText", "assert", "断言元素文案等于给定值", needs_data=True),
    ActionSpec("expectValue", "assert", "断言输入框当前值", needs_data=True),
    ActionSpec("expectEnabled", "assert", "断言元素可交互（按钮可点等）"),
    ActionSpec("expectDisabled", "assert", "断言元素禁用（按钮置灰等）"),
    ActionSpec("expectChecked", "assert", "断言复选框/开关为选中态"),
    ActionSpec("expectUnchecked", "assert", "断言复选框/开关为未选中态"),
    ActionSpec("expectState", "assert", "断言通用状态（激活态/展开态/on-off 等）", needs_data=True),
    ActionSpec("expectCount", "assert", "断言元素或列表项数量", needs_data=True),
    ActionSpec("expectUrl", "assert", "断言当前地址/页面标识", needs_data=True),
)

ACTION_SPECS: tuple[ActionSpec, ...] = _OPERATIONS + _ASSERTIONS

OPERATIONS: frozenset[str] = frozenset(a.name for a in _OPERATIONS)
ASSERTIONS: frozenset[str] = frozenset(a.name for a in _ASSERTIONS)
ACTION_NAMES: frozenset[str] = OPERATIONS | ASSERTIONS

# data 常需填写的动作（门禁在 data 缺失时给出提示，而非判错 —— 有些场景确实无需值）
ACTIONS_WITH_DATA: frozenset[str] = frozenset(a.name for a in ACTION_SPECS if a.needs_data)


def is_assertion(action: str) -> bool:
    """该动作是否为断言（用于「每条用例至少一个断言」门禁）。"""
    return str(action or "") in ASSERTIONS


def spec_of(action: str) -> ActionSpec | None:
    for spec in ACTION_SPECS:
        if spec.name == action:
            return spec
    return None


def render_actions_for_prompt() -> str:
    """生成注入提示词的动作清单（**不再手写**，杜绝与代码漂移）。

    按「操作 / 断言」分组并给出一句话说明 —— 这是让模型选对词的关键：
    原先只列名字，模型不知道 `expectDisabled` 能表达「按钮置灰」。
    """
    lines = ["操作用（改变系统状态）："]
    lines += [f"  {a.name}（{a.summary}）" for a in _OPERATIONS]
    lines.append("断言用（判定通过/失败，**每条用例至少 1 个**）：")
    lines += [f"  {a.name}（{a.summary}）" for a in _ASSERTIONS]
    return "\n".join(lines)
