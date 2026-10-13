"""testData 扁平化契约测试。

背景：嵌套对象/数组需要多层引号转义，是 JSON 写坏的首要来源。
实测 MiniMax-M3 因 testData 嵌套而在思考里反复纠错，整轮输出归零。
"""
import json

from prd2tc.schema import (flatten_test_data, normalize_cases, normalize_test_data,
                           validate_step_data, validate_test_data)
from prd2tc.validator import validate_case


def test_flatten_legacy_object_to_key_value_list():
    assert flatten_test_data({"phone": "13800138000", "password": "123456"}) == \
        ["phone=13800138000", "password=123456"]


def test_flatten_preserves_scalar_types():
    flat = flatten_test_data({"subtotal": 4727.05, "shipping": 0, "vip": True, "note": None})
    assert flat == ["subtotal=4727.05", "shipping=0", "vip=true"]


def test_flatten_nested_object_degrades_to_compact_json_text():
    """旧版嵌套值不能丢信息，降为紧凑 JSON 文本。"""
    flat = flatten_test_data({"tool": {"techName": "tool_query"}})
    assert flat == ['tool={"techName":"tool_query"}']
    assert json.loads(flat[0].split("=", 1)[1]) == {"techName": "tool_query"}


def test_flatten_already_flat_is_unchanged():
    assert flatten_test_data(["a=1", "b=2"]) == ["a=1", "b=2"]
    assert flatten_test_data([]) == []
    assert flatten_test_data(None) == []
    assert flatten_test_data("a=1") == ["a=1"]


def test_normalize_cases_flattens_every_case():
    data = {"schemaVersion": "2.0", "cases": [
        {"id": "TC-A-001", "testData": {"k": "v"}},
        {"id": "TC-A-002"},
    ]}
    out = normalize_cases(data)
    assert out[0]["testData"] == ["k=v"]
    assert "testData" not in out[1]


def test_normalize_test_data_leaves_absent_field_absent():
    case = {"id": "TC-A-001"}
    assert normalize_test_data(case) == {"id": "TC-A-001"}


def test_nesting_is_reported_as_issue():
    """嵌套值必须被校验器报出——它正是需要转义、会把 JSON 写坏的部分。"""
    issues = validate_test_data({"tool": {"params": [{"name": "city"}]}})
    assert issues and "嵌套" in issues[0]
    issues = validate_test_data([{"a": 1}, "ok=1"])
    assert issues and "testData[0]" in issues[0]


def test_flat_forms_pass_validation():
    assert validate_test_data(["a=1", "b=2"]) == []
    assert validate_test_data("a=1") == []
    assert validate_test_data(None) == []
    # 旧版 object + 标量值：允许（读取时归一化），不报错
    assert validate_test_data({"phone": "13800138000"}) == []


def test_step_data_must_be_scalar():
    assert validate_step_data({"steps": [
        {"action": "fill", "target": "x", "data": "文本"},
        {"action": "click", "target": "y", "data": ""},
    ]}) == []
    issues = validate_step_data({"steps": [{"action": "fill", "target": "x", "data": {"a": 1}}]})
    assert issues and "steps[1].data" in issues[0]


def _case(**over):
    base = {"id": "TC-A-001", "module": "A", "title": "t", "design_method": "场景法", "priority": "P1",
            "tags": ["@smoke"], "requirement": ["REQ-A-001"],
            "steps": [{"action": "goto", "target": "/", "data": ""},
                  {"action": "expectVisible", "target": "页面主体", "data": ""}],
            "expected": ["页面成功渲染并显示预期内容"]}
    base.update(over)
    return base


def test_validator_reports_nested_test_data():
    errors = validate_case(_case(testData={"tool": {"name": "x"}}), 0, set(), {"A"})
    assert any("嵌套" in e for e in errors), errors


def test_validator_accepts_flat_test_data():
    errors = validate_case(_case(testData=["phone=13800138000"]), 0, set(), {"A"})
    assert errors == [], errors


def test_validator_accepts_legacy_object_with_scalar_values():
    """向后兼容：旧产物（object + 标量）不得被校验器拒绝。"""
    errors = validate_case(_case(testData={"phone": "13800138000"}), 0, set(), {"A"})
    assert errors == [], errors


def test_validator_rejects_nested_step_data():
    errors = validate_case(_case(steps=[{"action": "fill", "target": "x", "data": {"a": 1}}]),
                           0, set(), {"A"})
    assert any("steps[1].data" in e for e in errors), errors


def test_example_library_uses_flat_form():
    """示例库是 few-shot 格式标准，必须已是扁平形态，否则模型会照抄 object。"""
    from pathlib import Path
    path = Path(__file__).parents[1] / "examples" / "youxuan-mall-examples.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    checked = 0
    for group in data:
        for case in group.get("cases", []):
            if "testData" not in case:
                continue
            checked += 1
            value = case["testData"]
            assert isinstance(value, list), f'{case.get("id")} testData 应为数组'
            assert all(isinstance(x, str) for x in value), f'{case.get("id")} 元素应为字符串'
            assert validate_test_data(value) == [], case.get("id")
    assert checked >= 1, "示例库应至少有一条带 testData 的用例"
