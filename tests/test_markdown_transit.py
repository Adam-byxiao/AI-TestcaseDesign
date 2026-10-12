"""Markdown 中转测试：解析/渲染往返保真，以及相对 JSON 的体积与抗转义优势。"""
import json
from pathlib import Path

import pytest

from prd2tc.markdown_transit import (COLUMNS, compact_json, extract_table_cases,
                                     parse_table, render_table)
from prd2tc.stages import _table_instruction, extract_and_normalize
from prd2tc.gates import check_cases_text


def _cases():
    return [{
        "id": "TC-AUTH-LOGIN-001", "module": "AUTH", "subModule": "LOGIN",
        "title": "正确凭据登录成功并跳转首页", "design_method": "场景法", "priority": "P0",
        "tags": ["@smoke", "@auth"], "requirement": ["REQ-AUTH-002"],
        "preconditions": ["存在 normal 状态账号"],
        "testData": ["phone=13800138000", "password=123456"],
        "steps": [{"action": "goto", "target": "/login", "data": ""},
                  {"action": "fill", "target": "手机号输入框", "data": "13800138000"},
                  {"action": "click", "target": "登录按钮", "data": ""},
                  {"action": "expectUrl", "target": "/", "data": ""}],
        "expected": ["登录成功后跳转首页", "导航栏显示当前用户昵称"],
    }]


def test_roundtrip_preserves_fields():
    original = _cases()
    restored = parse_table(render_table(original))
    assert len(restored) == 1
    case = restored[0]
    assert case["id"] == "TC-AUTH-LOGIN-001"
    assert case["module"] == "AUTH"
    assert case["priority"] == "P0"
    assert case["tags"] == ["@smoke", "@auth"]
    assert case["requirement"] == ["REQ-AUTH-002"]
    assert case["testData"] == ["phone=13800138000", "password=123456"]
    assert case["expected"] == ["登录成功后跳转首页", "导航栏显示当前用户昵称"]
    assert [s["action"] for s in case["steps"]] == ["goto", "fill", "click", "expectUrl"]
    assert case["steps"][1]["target"] == "手机号输入框"
    assert case["steps"][1]["data"] == "13800138000"


def test_roundtrip_is_idempotent():
    once = render_table(_cases())
    twice = render_table(parse_table(once))
    assert once == twice


def test_pipes_and_newlines_do_not_break_table():
    """单元格内的竖线/换行必须被中和，否则表格结构会被破坏。"""
    cases = [{"id": "TC-A-001", "module": "A", "title": "含|竖线\n与换行",
              "priority": "P1", "tags": ["@smoke"], "requirement": ["REQ-A-001"],
              "steps": [{"action": "goto", "target": "/", "data": ""}],
              "expected": ["行1\n行2"]}]
    table = render_table(cases)
    assert table.count("|") > 0
    body_lines = [l for l in table.splitlines() if l.startswith("|")]
    assert len(body_lines) == 3, "换行不应把一行拆成多行"
    restored = parse_table(table)[0]
    assert "｜" in restored["title"] or "|" in restored["title"]


def test_parse_ignores_unrelated_tables():
    text = "| 列A | 列B |\n|---|---|\n| 1 | 2 |\n"
    assert parse_table(text) == []
    with pytest.raises(ValueError):
        extract_table_cases(text)


def test_extract_and_normalize_prefers_table_when_enabled():
    table = render_table(_cases())
    got = extract_and_normalize(table, markdown_transit=True)
    assert got[0]["id"] == "TC-AUTH-LOGIN-001"
    assert got[0]["testData"] == ["phone=13800138000", "password=123456"]


def test_extract_falls_back_to_json_when_model_ignores_table_mode():
    """模型不听话输出 JSON 时流程仍要继续，不能直接失败。"""
    payload = json.dumps(_cases(), ensure_ascii=False)
    got = extract_and_normalize(payload, markdown_transit=True)
    assert got[0]["id"] == "TC-AUTH-LOGIN-001"


def test_gate_accepts_table_in_transit_mode():
    table = render_table(_cases())
    assert check_cases_text(table, {"AUTH"}, markdown_transit=True) == []
    # JSON 模式下同一表格应报「JSON 解析失败」
    assert check_cases_text(table, {"AUTH"}) != []


def test_gate_rejects_broken_table_with_clear_message():
    errors = check_cases_text("| id | module |\n|---|---|\n| | |", {"A"}, markdown_transit=True)
    assert errors and "表格" in errors[0], errors


def test_table_instruction_lists_all_columns():
    text = _table_instruction()
    for column in COLUMNS:
        assert column in text, column
    assert "分号" in text and "竖线" in text


def test_transit_is_smaller_than_json_on_example_library():
    """真实收益：同一内容的 Markdown 表格应显著小于紧凑 JSON。"""
    path = Path(__file__).parents[1] / "examples" / "youxuan-mall-examples.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = [c for group in data for c in group.get("cases", [])]
    table = render_table(cases)
    raw = compact_json(cases)
    assert len(table) < len(raw), (len(table), len(raw))
    reduction = 1 - len(table) / len(raw)
    assert reduction > 0.10, f"表格应至少省 10% 字符，实测 {reduction:.1%}"


def test_transit_avoids_escaped_quotes_entirely():
    """核心价值：表格形态下不再出现需要转义的引号嵌套。"""
    cases = [{"id": "TC-A-001", "module": "A", "title": "t", "design_method": "场景法", "priority": "P1",
              "tags": ["@smoke"], "requirement": ["REQ-A-001"],
              "steps": [{"action": "fill", "target": "输入框", "data": "13800138000"}],
              "expected": ["页面成功渲染并显示预期内容"]}]
    table = render_table(cases)
    assert '\\"' not in table, "表格不应产生转义引号"
    assert table.count('"') == 0, "表格里不应出现双引号"
