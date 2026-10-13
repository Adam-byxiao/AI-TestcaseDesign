"""用例 Schema 版本和旧格式兼容读取。"""
from __future__ import annotations

import json

from prd2tc.actions import ACTION_NAMES

SCHEMA_VERSION = "2.0"

# 动作词表版本。
# 1.0 → 1.1：补齐词表缺口（缺口是 `check` 误用率 87%~91% 的根因，见 actions.py）。
# 新版本是旧版本的**超集**，因此旧产物仍可通过校验（向后兼容）。
ACTION_SCHEMA_VERSION = "1.1"
ACTIONS_BY_VERSION = {
    "1.0": frozenset({
        "goto", "fill", "click", "selectOption", "check", "hover", "press", "reload",
        "expectVisible", "expectText", "expectUrl", "waitFor",
    }),
    # 单一事实源：由 `prd2tc.actions` 派生，避免与提示词/门禁漂移
    ACTION_SCHEMA_VERSION: ACTION_NAMES,
}

# 测试设计方法枚举：与 prompts/00-testing-principles.md §2、04-case-constraints.md §A.1 同源。
# 单一事实源放在这里，prompt 与 validator 都引用它，避免两边漂移。
DESIGN_METHODS = ("等价类划分", "边界值分析", "判定表", "状态迁移", "场景法", "错误推测")


def normalize_cases(data) -> list[dict]:
    if isinstance(data, list):
        return [normalize_test_data(case) for case in data]
    if isinstance(data, dict) and isinstance(data.get("cases"), list):
        return [normalize_test_data(case) for case in data["cases"]]
    raise ValueError("用例文件顶层必须是数组或 {cases: [...]} 对象")


def envelope(cases: list[dict]) -> dict:
    return {"schemaVersion": SCHEMA_VERSION, "actionSchemaVersion": ACTION_SCHEMA_VERSION, "cases": cases}


# ---- testData 扁平化 ----
# 嵌套对象/数组是 JSON 转义崩坏的首要来源：实测 MiniMax-M3 因 testData 嵌套而
# 在思考里反复纠错，整轮 26,504 字符输出 100% 是推理、可用产出为 0。
# 因此契约收紧为「键=值」字符串数组，值只能是标量；复杂数据写进 preconditions。

def flatten_test_data(value) -> list[str]:
    """把 testData 归一化为「键=值」字符串数组（兼容旧版 object 形式）。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                if item:
                    out.append(item)
            elif isinstance(item, dict):
                out.extend(flatten_test_data(item))
            elif item is not None:
                out.append(str(item))
        return out
    if isinstance(value, dict):
        out = []
        for key, item in value.items():
            if isinstance(item, (dict, list, tuple)):
                # 旧版嵌套值：降为紧凑 JSON 文本，保证读取不丢信息
                out.append(f"{key}={json.dumps(item, ensure_ascii=False, separators=(',', ':'))}")
            elif isinstance(item, bool):
                out.append(f"{key}={str(item).lower()}")
            elif item is not None:
                out.append(f"{key}={item}")
        return out
    return [str(value)]


def normalize_test_data(case: dict) -> dict:
    """就地归一化单条用例的 testData（仅当字段存在时）。"""
    if isinstance(case, dict) and "testData" in case:
        case["testData"] = flatten_test_data(case.get("testData"))
    return case


def validate_test_data(value) -> list[str]:
    """校验 testData 的**原始**形态，返回问题清单。

    刻意不把 object 容器判错（旧产物要能读），但**嵌套值必须拒绝**：
    那正是需要转义、从而把 JSON 写坏的部分。拒绝后由门禁反馈给模型重出。
    """
    if value is None or isinstance(value, str):
        return []
    if isinstance(value, dict):
        return [f"testData.{k} 是嵌套对象/数组，必须改为「键=值」标量（复杂数据请写进 preconditions）"
                for k, v in value.items() if isinstance(v, (dict, list, tuple))]
    if isinstance(value, (list, tuple)):
        return [f"testData[{i}] 是嵌套对象/数组，必须改为「键=值」字符串（复杂数据请写进 preconditions）"
                for i, v in enumerate(value) if isinstance(v, (dict, list, tuple))]
    return [f"testData 类型非法: {type(value).__name__}（应为「键=值」字符串数组）"]


def validate_step_data(case: dict) -> list[str]:
    """steps[].data 必须是字符串。

    嵌套对象同样需要转义，是 JSON 崩坏的另一来源；文档契约本就是字符串。
    """
    issues = []
    for index, step in enumerate(case.get("steps") or [], 1):
        if not isinstance(step, dict):
            continue
        if "data" in step and not isinstance(step.get("data"), (str, int, float, bool, type(None))):
            issues.append(f"steps[{index}].data 必须是字符串/标量，不能是嵌套对象或数组")
    return issues
