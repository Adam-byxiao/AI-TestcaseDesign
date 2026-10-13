"""动作词表测试。

背景：`check` 的语义误用率在评测 v1/v2 中高达 **87%~91%**。
逐条枚举 47 次使用后，性质是「**没有更合适的动作可用**」：

| 实际语义 | 次数 | 当时缺失的动作 |
| --- | ---: | --- |
| 元素状态/位置（禁用、on-off、展开） | 23 | `expectDisabled` / `expectState` |
| 拖拽上传 | 9 | `upload` |
| 网络/数量 | 5 | `expectCount` |
| 排他/反向（不可见、不显示） | 4 | `expectNotVisible` |
| 应为 click/hover | 4 | —（选词错误） |
| 应为 expectVisible/Text | 2 | —（选词错误） |

即 `check` 是模型在词表缺口下**唯一听起来像「验证」**的词。
因此补齐词表比反复强调「不要滥用 check」更有效。

同时引入**断言存在性门禁**：只有操作、没有断言的用例无法判定通过/失败。
（实测 v1/v2/v3 分别有 5/4/1 条这样的用例。）
"""
import re
from pathlib import Path

import pytest

from prd2tc.actions import (ACTION_NAMES, ACTION_SPECS, ACTIONS_WITH_DATA, ASSERTIONS,
                            OPERATIONS, is_assertion, render_actions_for_prompt, spec_of)
from prd2tc.schema import ACTION_SCHEMA_VERSION, ACTIONS_BY_VERSION
from prd2tc.validator import ACTIONS, validate_case

REPO = Path(__file__).resolve().parents[1]


def _case(**over):
    base = {"id": "TC-A-001", "module": "A", "title": "登录成功", "design_method": "场景法",
            "priority": "P1", "tags": ["@regression"], "requirement": ["REQ-A-001"],
            "steps": [{"action": "goto", "target": "/login", "data": ""},
                      {"action": "expectVisible", "target": "登录表单", "data": ""}],
            "expected": ["登录表单成功渲染"], "testData": []}
    base.update(over)
    return base


# ---------- 词表结构 ----------

def test_operations_and_assertions_are_disjoint():
    assert not (OPERATIONS & ASSERTIONS)
    assert ACTION_NAMES == OPERATIONS | ASSERTIONS


def test_registry_covers_the_gap_actions():
    """**核心回归**：曾因缺失而导致 `check` 误用的动作必须存在。"""
    for action in ("expectDisabled", "expectState", "expectNotVisible", "expectCount",
                   "expectEnabled", "upload", "expectChecked", "expectValue"):
        assert action in ACTION_NAMES, action


def test_is_assertion_classifies_correctly():
    assert is_assertion("expectVisible")
    assert is_assertion("expectCount")
    # waitFor 是**同步动作**，不是断言 —— 常见误解
    assert not is_assertion("waitFor")
    assert not is_assertion("check")
    assert not is_assertion("click")
    assert not is_assertion("")


def test_every_spec_has_summary_and_kind():
    for spec in ACTION_SPECS:
        assert spec.kind in ("operate", "assert"), spec
        assert spec.summary.strip(), spec
        assert spec.name in ACTION_NAMES


def test_spec_of_lookup():
    assert spec_of("expectCount").kind == "assert"
    assert spec_of("upload").kind == "operate"
    assert spec_of("不存在的动作") is None


def test_actions_with_data_are_declared():
    assert "fill" in ACTIONS_WITH_DATA
    assert "expectText" in ACTIONS_WITH_DATA
    assert "click" not in ACTIONS_WITH_DATA


# ---------- 单一事实源：不得多处漂移 ----------

def test_schema_derives_from_registry():
    """`schema.ACTIONS_BY_VERSION` 必须由 `actions` 派生（单一事实源）。"""
    assert ACTIONS_BY_VERSION[ACTION_SCHEMA_VERSION] == ACTION_NAMES


def test_validator_actions_match_registry():
    from prd2tc.validator import ACTIONS as V
    assert set(V) == set(ACTION_NAMES)


def test_new_version_is_superset_of_old():
    """新版本必须是旧版本的**超集**，否则旧产物会突然校验失败。"""
    old = ACTIONS_BY_VERSION["1.0"]
    new = ACTIONS_BY_VERSION[ACTION_SCHEMA_VERSION]
    assert old <= new, sorted(old - new)


def test_prompt_mentions_every_action():
    """注入提示词的动作清单必须覆盖全部动作（词表缺一即会诱发误用）。"""
    text = render_actions_for_prompt()
    missing = [a for a in ACTION_NAMES if a not in text]
    assert not missing, missing
    assert "操作用" in text and "断言用" in text


def test_prompt_files_no_longer_hardcode_action_enum():
    """静态提示词不应再手写动作清单 —— 那是漂移的源头。

    允许出现单个动作名（示例/说明），但不得再出现完整的枚举串。
    """
    stale = "goto/fill/click/selectOption/check/hover/press/reload/expectVisible/expectText/expectUrl/waitFor"
    for name in ("prompts/00-testing-principles.md", "prompts/04-case-constraints.md",
                 "docs/case-spec.md"):
        text = (REPO / name).read_text(encoding="utf-8")
        assert stale not in text, f"{name} 仍手写旧动作枚举"


def test_packaged_prompts_stay_in_sync_with_repo_prompts():
    """`src/prd2tc/assets/prompts/` 是**安装包兜底副本**，必须与 `prompts/` 一致。

    为什么必须测：loader 在仓库内优先，但安装后回退到 assets。
    两处不一致会导致「本地改了提示词、安装用户拿到的还是旧的」——
    实测本仓库就曾有 3 个文件不同步且无任何校验。
    """
    repo = REPO / "prompts"
    assets = REPO / "src" / "prd2tc" / "assets" / "prompts"
    if not assets.is_dir():
        pytest.skip("未打包 assets/prompts")
    mismatched = []
    for src in sorted(assets.glob("*.md")):
        twin = repo / src.name
        if not twin.is_file():
            mismatched.append(f"{src.name}: 仓库 prompts/ 缺少对应文件")
            continue
        if src.read_bytes() != twin.read_bytes():
            mismatched.append(f"{src.name}: 内容不一致")
    assert not mismatched, mismatched


def test_packaged_templates_stay_in_sync_with_repo_templates():
    """**核心回归**：`assets/templates/` 也必须与 `templates/` 一致。

    为什么必须单独测：同步脚本原先**只处理 `prompts/`**，模板侧完全没被覆盖 ——
    实测 `assets/templates/` 因此缺少 `test-case.template.md`（仓库里有、安装后没有）。
    上面那个提示词测试也只看 `prompts/`，所以漏了整整一类资产。

    这里做**双向**检查：既查内容不一致，也查「仓库有而 assets 缺」，
    后者才是本次实际发生的缺陷形态。
    """
    repo = REPO / "templates"
    assets = REPO / "src" / "prd2tc" / "assets" / "templates"
    if not assets.is_dir():
        pytest.skip("未打包 assets/templates")
    problems = []
    for src in sorted(repo.glob("*.md")):
        twin = assets / src.name
        if not twin.is_file():
            problems.append(f"{src.name}: assets/templates 缺失（安装后不可用）")
        elif src.read_bytes() != twin.read_bytes():
            problems.append(f"{src.name}: 内容不一致")
    for extra in sorted(assets.glob("*.md")):
        if not (repo / extra.name).is_file():
            problems.append(f"{extra.name}: assets 中存在但仓库 templates/ 已无")
    assert not problems, problems


def test_sync_script_covers_both_asset_kinds():
    """同步脚本必须同时覆盖提示词与模板（防止再次漏掉一类资产）。"""
    script = (REPO / "scripts" / "sync_prompts.py").read_text(encoding="utf-8")
    assert '"prompts"' in script and '"templates"' in script, \
        "sync_prompts.py 未覆盖 templates/（模板会再次漂移）"


# ---------- 断言存在性门禁 ----------

def test_case_without_assertion_is_rejected():
    """**核心回归**：只有操作、没有断言的用例必须被拦。

    否则用例可以只有 goto/fill/click，无法判定通过/失败，却完全合规。
    """
    case = _case(steps=[{"action": "goto", "target": "/login", "data": ""},
                        {"action": "click", "target": "登录按钮", "data": ""}])
    errors = validate_case(case, 0, set())
    assert any("缺少断言" in e for e in errors), errors


def test_waitFor_alone_is_not_an_assertion():
    """`waitFor` 是同步动作，不能当作断言依据。"""
    case = _case(steps=[{"action": "goto", "target": "/a", "data": ""},
                        {"action": "click", "target": "提交", "data": ""},
                        {"action": "waitFor", "target": "加载完成", "data": ""}])
    errors = validate_case(case, 0, set())
    assert any("缺少断言" in e for e in errors), errors


def test_case_with_any_assertion_passes():
    for action in sorted(ASSERTIONS):
        case = _case(steps=[{"action": "goto", "target": "/a", "data": ""},
                            {"action": action, "target": "某元素", "data": "v"}])
        errors = validate_case(case, 0, set())
        assert not any("缺少断言" in e for e in errors), (action, errors)


def test_assertion_error_lists_available_actions():
    """报错必须列出可选断言动作 —— 只报错不给选项，模型仍无从下手。"""
    case = _case(steps=[{"action": "click", "target": "按钮", "data": ""}])
    errors = validate_case(case, 0, set())
    msg = next(e for e in errors if "缺少断言" in e)
    for action in ("expectVisible", "expectText", "expectCount"):
        assert action in msg, (action, msg)


def test_illegal_action_message_lists_options():
    """非法动作的报错同样应给出可选项。"""
    case = _case(steps=[{"action": "hack", "target": "x", "data": ""}])
    errors = validate_case(case, 0, set())
    msg = next(e for e in errors if "step.action 非法" in e)
    assert "expectVisible" in msg, msg


def test_new_actions_are_accepted_by_validator():
    """新增动作必须真的能通过校验（否则词表补了也没用）。"""
    for action in ("expectDisabled", "expectState", "expectNotVisible", "expectCount",
                   "upload", "expectChecked", "expectValue", "uncheck", "scrollTo"):
        case = _case(steps=[{"action": "goto", "target": "/a", "data": ""},
                            {"action": action, "target": "元素", "data": "值"}])
        errors = validate_case(case, 0, set())
        assert not any("step.action 非法" in e for e in errors), (action, errors)


def test_actions_in_rendered_prompt_are_all_valid():
    """提示词里列出的动作，必须全都能通过校验（自洽性）。"""
    text = render_actions_for_prompt()
    listed = set(re.findall(r"^\s{2}(\w+)（", text, re.M))
    assert listed, "未从提示词中解析出动作"
    assert listed <= set(ACTION_NAMES), sorted(listed - set(ACTION_NAMES))
    assert set(ACTION_NAMES) <= listed, sorted(set(ACTION_NAMES) - listed)
