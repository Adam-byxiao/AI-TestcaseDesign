"""validator 单元测试"""
import json

import pytest

from prd2tc.validator import validate_case

VALID_CASE = {
    "id": "TC-AUTH-LOGIN-001",
    "module": "AUTH",
    "subModule": "LOGIN",
    "title": "正确凭据登录成功",
    "priority": "P0",
    "tags": ["@smoke", "@auth"],
    "requirement": ["REQ-AUTH-002"],
    "steps": [
        {"action": "goto", "target": "/login", "data": ""},
        {"action": "click", "target": "登录按钮", "data": ""},
    ],
    "expected": ["跳转首页"],
}


def test_valid_case_passes():
    assert validate_case(VALID_CASE, 0, set()) == []


def test_invalid_id():
    case = {**VALID_CASE, "id": "BAD"}
    errors = validate_case(case, 0, set())
    assert any("id 格式非法" in e for e in errors)


def test_duplicate_id():
    seen = {"TC-AUTH-LOGIN-001"}
    errors = validate_case(VALID_CASE, 0, seen)
    assert any("id 重复" in e for e in errors)


def test_p0_requires_smoke():
    case = {**VALID_CASE, "tags": ["@auth"]}
    errors = validate_case(case, 0, set())
    assert any("@smoke" in e for e in errors)


def test_bad_enum():
    case = {**VALID_CASE, "module": "XXX", "priority": "PX"}
    errors = validate_case(case, 0, set())
    assert any("module 非法" in e for e in errors)
    assert any("priority 非法" in e for e in errors)


def test_bad_steps():
    case = {**VALID_CASE, "steps": [{"action": "hack", "target": ""}]}
    errors = validate_case(case, 0, set())
    assert any("step.action 非法" in e for e in errors)
    assert any("step.target 为空" in e for e in errors)


def test_missing_fields():
    case = {"id": "TC-X-001"}
    errors = validate_case(case, 0, set())
    # 应尽量收集全部问题（module/priority/tags/requirement/steps/expected/title）
    assert len(errors) >= 5
