"""A5/A6/A7 门禁专项测试。

对应 docs/prompt-gate-optimization.md 的 C-005 / C-006 / C-007。
最关键的是最后两个「一致性」测试：门禁**不得拦掉 prompt 自带的示例**——
否则模型会陷入「照抄示例 → 被门禁拦 → 改写」的死循环（文档 R-1 的根因）。
"""
import json
import re
from pathlib import Path

import pytest

from prd2tc import gates
from prd2tc.schema import DESIGN_METHODS
from prd2tc.validator import (case_signature, check_duplicate, is_vague_expected,
                              validate_case, validate_dir)

REPO = Path(__file__).resolve().parents[1]
PROMPT3 = REPO / "prompts" / "03-test-cases.md"
EXAMPLES = REPO / "examples" / "youxuan-mall-examples.json"


def _case(**over):
    base = {"id": "TC-A-001", "module": "A", "title": "登录成功跳转首页",
            "design_method": "场景法", "priority": "P1", "tags": ["@regression"],
            "requirement": ["REQ-A-001"],
            "steps": [{"action": "goto", "target": "/login", "data": ""}],
            "expected": ["登录成功后跳转首页"]}
    base.update(over)
    return base


# ---------- C-005 design_method ----------

def test_design_method_required():
    errors = validate_case(_case(design_method=None), 0, set(), {"A"})
    assert any("design_method" in e for e in errors), errors


def test_design_method_must_be_from_enum():
    errors = validate_case(_case(design_method="凭感觉"), 0, set(), {"A"})
    assert any("design_method 非法" in e for e in errors), errors


@pytest.mark.parametrize("method", DESIGN_METHODS)
def test_all_enum_values_accepted(method):
    assert validate_case(_case(design_method=method), 0, set(), {"A"}) == []


def test_design_method_can_be_relaxed_for_legacy_cases():
    """过渡开关：读取历史产物时可放宽 C-005。"""
    assert validate_case(_case(design_method=None), 0, set(), {"A"},
                         require_design_method=False) == []


# ---------- C-006 空洞/过短预期 ----------

@pytest.mark.parametrize("text", ["表现正常", "无异常", "功能正常", "符合预期", "不报错"])
def test_vague_expectations_rejected(text):
    errors = validate_case(_case(expected=[text]), 0, set(), {"A"})
    assert any("不可验证" in e for e in errors), (text, errors)


@pytest.mark.parametrize("text", [
    "成功", "正常", "通过", "无异常", "ok",
    # 阈值 8 后新纳入拦截范围的短短语（此前 len=4~6 可放行）
    "页面渲染", "合计归零", "跳转首页", "不进入登录态",
])
def test_too_short_expectations_rejected(text):
    errors = validate_case(_case(expected=[text]), 0, set(), {"A"})
    assert errors, text


@pytest.mark.parametrize("text", [
    "登录成功后跳转首页",                 # prompt 自带示例，len=9 > 8 → 必须放行
    "页面成功渲染并显示预期内容",
    "删除后小计与合计金额均归零",
    "登录成功后跳转至首页",
    "仍停留在登录页且不进入登录态",
    '页面跳转正常，提示"余额不足"',        # 含空洞词但长且具体 → 长度保护放行
    "页面显示正常状态码 200",              # 含空洞词但含具体值 → 放行
    "数量为 1 时按钮禁用",                 # 含数字 → 豁免长度
])
def test_acceptable_expectations_pass(text):
    errors = validate_case(_case(expected=[text]), 0, set(), {"A"})
    assert errors == [], (text, errors)


def test_is_vague_expected_length_guard():
    assert is_vague_expected("表现正常") is True
    assert is_vague_expected("页面跳转正常，提示余额不足，且订单状态变为已支付") is False


def test_min_expected_len_threshold_is_pinned():
    """把阈值钉在 8（用户指定的从严取值）。

    这条断言的作用：任何人改动阈值都会立即失败，迫使回到本文件确认
    「示例库/prompt 示例是否同步改写」——阈值与示例必须同时动，
    否则会出现「模型照抄示例 → 被门禁拦」的死循环（docs R-1）。
    """
    from prd2tc import validator
    assert validator._MIN_EXPECTED_LEN == 8, (
        "阈值已按用户指定钉为 8。若需回退到更宽松的值（如 3），"
        "必须同时更新 04-case-constraints.md、示例库与 prompt 示例的长度要求"
    )
    # > 8 字且无数字 → 放行
    assert validate_case(_case(expected=["登录成功后跳转至首页"]), 0, set(), {"A"}) == []
    assert validate_case(_case(expected=["页面成功渲染并显示预期内容"]), 0, set(), {"A"}) == []
    # <= 8 字且无数字 → 拦下
    for short in ("成功", "正常", "无异常", "页面渲染", "合计归零"):
        assert validate_case(_case(expected=[short]), 0, set(), {"A"}), short
    # 含数字则豁免长度
    assert validate_case(_case(expected=["数量 1 时禁用"]), 0, set(), {"A"}) == []
    for short in ("成功", "正常", "无异常"):
        assert validate_case(_case(expected=[short]), 0, set(), {"A"}), short


def test_empty_expected_still_reported():
    errors = validate_case(_case(expected=[]), 0, set(), {"A"})
    assert any("expected 为空" in e for e in errors), errors


# ---------- C-007 重复用例 ----------

def test_duplicate_signature_ignores_data_but_not_steps():
    a = _case(steps=[{"action": "fill", "target": "手机号", "data": "111"}])
    b = _case(steps=[{"action": "fill", "target": "手机号", "data": "222"}])
    assert case_signature(a) == case_signature(b), "同步骤换数据仍属同质用例"

    c = _case(steps=[{"action": "fill", "target": "邮箱", "data": "111"}])
    assert case_signature(a) != case_signature(c), "目标不同即非重复"


def test_duplicate_detected_within_batch():
    dup = '```json\n' + json.dumps([
        _case(id="TC-A-001"), _case(id="TC-A-002")   # 仅 id 不同，步骤/预期全同
    ], ensure_ascii=False) + '\n```'
    errors = gates.check_cases_text(dup, {"A"})
    assert any("重复" in e for e in errors), errors


def test_distinct_cases_not_flagged():
    ok = '```json\n' + json.dumps([
        _case(id="TC-A-001", expected=["登录成功后跳转首页"]),
        _case(id="TC-A-002", steps=[{"action": "goto", "target": "/cart", "data": ""}],
              expected=["购物车页面展示 1 件商品"]),
    ], ensure_ascii=False) + '\n```'
    assert gates.check_cases_text(ok, {"A"}) == []


def test_duplicates_detected_across_files(tmp_path):
    """同模块用例可能分布在不同文件，去重必须跨文件。"""
    import json as _json
    for name, cid in (("A.json", "TC-A-001"), ("B.json", "TC-A-002")):
        (tmp_path / name).write_text(_json.dumps([_case(id=cid)], ensure_ascii=False),
                                     encoding="utf-8")
    total, errors = validate_dir(tmp_path, {"A"})
    assert total == 2, total
    assert any("重复" in e for e in errors), errors


def test_validate_dir_total_is_sum_not_product(tmp_path):
    """R-3 回归：total 必须是各文件用例数之和，不能按 len(cases) 倍膨胀。"""
    import json as _json
    for name in ("A.json", "B.json", "C.json"):
        (tmp_path / name).write_text(_json.dumps([
            _case(id=f"TC-{name[0]}-001", steps=[{"action": "goto", "target": f"/{name}", "data": ""}],
                  expected=[f"{name} 页面成功渲染"]),
            _case(id=f"TC-{name[0]}-002", steps=[{"action": "goto", "target": f"/{name}/2", "data": ""}],
                  expected=[f"{name} 第二页成功渲染"]),
        ], ensure_ascii=False), encoding="utf-8")
    total, errors = validate_dir(tmp_path, {"A", "B", "C"})
    assert errors == [], errors
    assert total == 6, f"3 个文件各 2 条应为 6，实际 {total}"


def test_check_duplicate_registers_first_occurrence():
    signatures: dict[str, str] = {}
    assert check_duplicate(_case(id="TC-A-001"), "[1]", signatures) == []
    assert signatures, "首次出现应登记签名"
    errors = check_duplicate(_case(id="TC-A-002"), "[2]", signatures)
    assert errors and "TC-A-001" in errors[0]


# ---------- 门禁 ↔ prompt 一致性（最关键） ----------

def _prompt_example() -> dict:
    """抽出 prompts/03-test-cases.md 里的唯一 JSON 示例。"""
    text = PROMPT3.read_text(encoding="utf-8")
    match = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", text)
    assert match, "03-test-cases.md 缺少 JSON 示例"
    return json.loads(match.group(1))


def test_prompt_example_declares_design_method():
    """示例必须含 design_method —— 示例优先于描述，否则模型会漏字段。"""
    assert "design_method" in _prompt_example(), (
        "阶段 3 JSON 示例缺少 design_method；模型会服从示例而违反 principles 的 [必须]"
    )
    assert _prompt_example()["design_method"] in DESIGN_METHODS


def test_prompt_example_passes_gate():
    """**核心回归**：门禁不得拦掉 prompt 自带的示例。

    文档 R-1 的根因就是「门禁比示例更严」：示例说 expected=["登录成功后跳转至首页"]，
    而阈值 5 的门禁判它『过短』，把模型拖进照抄→被拦→改写的死循环。
    """
    example = _prompt_example()
    errors = validate_case(example, 0, set(), {example["module"]})
    assert errors == [], (
        f"prompt 自带示例未通过门禁，模型将陷入示例/门禁冲突：{errors}"
    )


def test_prompt_example_passes_gate_as_batch():
    """同一示例经阶段 3 门禁（含重复检测与解析）也应通过。"""
    text = PROMPT3.read_text(encoding="utf-8")
    match = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", text)
    batch = "```json\n[" + match.group(1) + "]\n```"
    assert gates.check_cases_text(batch, {_prompt_example()["module"]}) == []


def test_example_library_all_cases_pass_gate():
    """示例库是 few-shot 输入，其中的用例也必须符合门禁契约。"""
    data = json.loads(EXAMPLES.read_text(encoding="utf-8"))
    checked = 0
    seen: set[str] = set()
    for group in data:
        for case in group.get("cases", []):
            checked += 1
            errors = validate_case(case, checked, seen, {case.get("module")})
            assert errors == [], f'{case.get("id")} 未通过门禁: {errors}'
    assert checked >= 6, checked


def test_transit_mode_table_example_passes_gate():
    """Markdown 中转模式下，程序渲染的表格也必须通过门禁（往返一致）。"""
    from prd2tc.markdown_transit import render_table
    cases = [{
        "id": "TC-AUTH-LOGIN-001", "module": "AUTH", "subModule": "LOGIN",
        "title": "正确凭据登录成功", "design_method": "等价类划分", "priority": "P0",
        "tags": ["@smoke", "@auth"], "requirement": ["REQ-AUTH-002"],
        "preconditions": ["存在 normal 状态账号"],
        "testData": ["phone=13800138000"],
        "steps": [{"action": "goto", "target": "/login", "data": ""}],
        "expected": ["登录成功后跳转首页"],
    }]
    assert gates.check_cases_text(render_table(cases), {"AUTH"}, markdown_transit=True) == []
