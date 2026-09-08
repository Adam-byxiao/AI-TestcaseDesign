"""用例 Schema 版本和旧格式兼容读取。"""
from __future__ import annotations

SCHEMA_VERSION = "2.0"
ACTION_SCHEMA_VERSION = "1.0"
ACTIONS_BY_VERSION = {
    ACTION_SCHEMA_VERSION: frozenset({
        "goto", "fill", "click", "selectOption", "check", "hover", "press", "reload",
        "expectVisible", "expectText", "expectUrl", "waitFor",
    })
}


def normalize_cases(data) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("cases"), list):
        return data["cases"]
    raise ValueError("用例文件顶层必须是数组或 {cases: [...]} 对象")


def envelope(cases: list[dict]) -> dict:
    return {"schemaVersion": SCHEMA_VERSION, "actionSchemaVersion": ACTION_SCHEMA_VERSION, "cases": cases}
