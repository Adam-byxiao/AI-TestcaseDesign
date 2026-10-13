"""自然语言步骤渲染测试。

背景：结构化步骤（`[goto] 智能体配置页 (page=config)`）是给机器看的，
但放进 Excel/XMind/Markdown 给人评审时极难阅读（用户实测反馈）。

约定：**JSON 保留结构化**（门禁与自动化驱动用），**导出物用自然语言**。
"""
import json
from pathlib import Path

import pytest

from prd2tc import nlsteps
from prd2tc.nlsteps import render_steps_nl, render_steps_nl_inline, split_target, step_sentence


# ---------- target 拆分 ----------

def test_split_target_extracts_name_kind_value():
    assert split_target("智能体配置页 (page=config)") == ("智能体配置页", "page", "config")
    assert split_target("文档名称输入框 (id=docName)") == ("文档名称输入框", "id", "docName")
    assert split_target("页签栏 (class=tabs)") == ("页签栏", "class", "tabs")
    assert split_target("保存按钮 (text=保存)") == ("保存按钮", "text", "保存")


def test_split_target_without_annotation():
    assert split_target("工具列表与详情区") == ("工具列表与详情区", "", "")
    assert split_target("") == ("", "", "")


def test_split_target_tolerates_whitespace_and_nesting():
    assert split_target("  删除按钮 ( class=danger )  ") == ("删除按钮", "class", "danger")


def test_split_target_handles_bare_annotation_without_parens():
    """无括号变体 `知识库配置页 page=knowledge` 也必须被识别。

    实测模型对同一语义有两种写法，只有括号版被处理时，
    导出物里会残留 `打开「知识库配置页 page=knowledge」`。
    """
    assert split_target("知识库配置页 page=knowledge") == ("知识库配置页", "page", "knowledge")
    assert split_target("文档名称输入框 id=docName") == ("文档名称输入框", "id", "docName")
    out = step_sentence({"action": "goto", "target": "知识库配置页 page=knowledge"})
    assert out == "打开「知识库配置页」", out


# ---------- 动作 → 自然语言 ----------

def test_goto_renders_as_open():
    assert step_sentence({"action": "goto", "target": "智能体配置页 (page=config)"}) == \
        "打开「智能体配置页」"


def test_click_and_fill_render_naturally():
    assert step_sentence({"action": "click",
                          "target": "＋ 添加文档知识 按钮 (class=primary)"}) == \
        "点击「＋ 添加文档知识 按钮」"
    assert step_sentence({"action": "fill", "target": "文档名称输入框 (id=docName)",
                          "data": "kb.txt"}) == "在「文档名称输入框」输入「kb.txt」"


def test_expect_actions_are_prefixed_with_verify():
    """断言类必须带「验证」前缀，使评审稿能一眼区分操作与检查点。"""
    assert step_sentence({"action": "expectVisible",
                          "target": "添加文档知识弹窗 (id=modal)"}) == \
        "验证「添加文档知识弹窗」可见"
    assert step_sentence({"action": "expectText",
                          "target": "保存状态提示 (id=saveState)",
                          "data": "草稿已自动保存"}) == \
        "验证「保存状态提示」显示「草稿已自动保存」"
    assert step_sentence({"action": "expectDisabled",
                          "target": "保存按钮 (text=保存)"}) == \
        "验证「保存按钮」处于禁用状态"


def test_machine_markers_never_leak_into_natural_language():
    """核心诉求：`[action]`、`(kind=value)` 不得出现在自然语言里。"""
    steps = [
        {"action": "goto", "target": "知识库配置页 (page=knowledge)", "data": ""},
        {"action": "click", "target": "查看按钮 (class=actions)", "data": ""},
        {"action": "expectVisible", "target": "文档片段弹窗 (id=modal)", "data": ""},
    ]
    text = "\n".join(render_steps_nl(steps))
    assert "[goto]" not in text and "[click]" not in text
    assert "page=" not in text and "class=" not in text and "id=" not in text
    assert text.splitlines() == [
        "1. 打开「知识库配置页」",
        "2. 点击「查看按钮」",
        "3. 验证「文档片段弹窗」可见",
    ]


def test_unknown_action_is_not_silently_dropped():
    """未知动作回退为「动作+元素」，信息不得丢失（不能渲染成空串）。"""
    out = step_sentence({"action": "dragTo", "target": "文件区 (class=file-drop)",
                         "data": "a.txt"})
    assert "dragTo" in out and "文件区" in out and "a.txt" in out


def test_step_without_target_or_data_still_readable():
    assert step_sentence({"action": "reload", "target": "", "data": ""}) == "刷新页面"
    # 无 data 的 fill 不得出现空引号
    out = step_sentence({"action": "fill", "target": "输入框 (id=testInput)", "data": ""})
    assert "「」" not in out and "输入框" in out


def test_non_dict_step_does_not_crash():
    assert step_sentence("原始文本") == "原始文本"


# ---------- 列表 / 内联渲染 ----------

def test_render_steps_nl_numbering_can_be_disabled():
    steps = [{"action": "goto", "target": "配置页 (page=config)"},
             {"action": "reload", "target": ""}]
    assert render_steps_nl(steps) == ["1. 打开「配置页」", "2. 刷新页面"]
    assert render_steps_nl(steps, numbered=False) == ["打开「配置页」", "刷新页面"]


def test_render_steps_nl_inline_joins_with_semicolon():
    steps = [{"action": "goto", "target": "配置页 (page=config)"},
             {"action": "click", "target": "保存按钮 (class=primary)"}]
    assert render_steps_nl_inline(steps) == "打开「配置页」；点击「保存按钮」。"
    assert render_steps_nl_inline([]) == ""


def test_rendering_is_deterministic():
    steps = [{"action": "fill", "target": "输入框 (id=docName)", "data": "x"}]
    assert render_steps_nl(steps) == render_steps_nl(steps)


# ---------- 接入导出 ----------

@pytest.fixture()
def sample_case():
    return {
        "id": "TC-KB-001", "module": "KB", "subModule": "LIST",
        "title": "查看文档片段", "design_method": "场景法", "priority": "P0",
        "tags": ["@smoke", "@kb"], "requirement": ["REQ-KB-001"],
        "preconditions": ["已登录"], "testData": ["doc=医保政策指南"],
        "steps": [
            {"action": "goto", "target": "知识库配置页 (page=knowledge)", "data": ""},
            {"action": "click", "target": "查看按钮 (class=actions)", "data": ""},
            {"action": "expectVisible", "target": "文档片段弹窗 (id=modal)", "data": ""},
        ],
        "expected": ["弹窗内逐个展示 chunk 内容"],
    }


def test_markdown_export_uses_natural_language(sample_case):
    """用例集.md 必须含自然语言步骤，且不出现机器标记。"""
    from prd2tc.io import render_cases_md
    md = render_cases_md([sample_case], plan_title="PRD v1")
    assert "打开「知识库配置页」" in md
    assert "点击「查看按钮」" in md
    assert "验证「文档片段弹窗」可见" in md
    assert "[goto]" not in md and "page=knowledge" not in md
    # 结构化原文仍指向 JSON
    assert "cases/cases/*.json" in md


def test_markdown_export_lists_preconditions_and_expected(sample_case):
    from prd2tc.io import render_cases_md
    md = render_cases_md([sample_case])
    assert "已登录" in md
    assert "弹窗内逐个展示 chunk 内容" in md


def _xmind_blob(cases: list[dict], mode: str) -> str:
    from prd2tc.export import xmind
    data = xmind.build_xmind_bytes(cases, title="用例集", mode=mode)
    import zipfile
    from io import BytesIO
    with zipfile.ZipFile(BytesIO(data)) as zf:
        return json.dumps(json.loads(zf.read("content.json").decode("utf-8")),
                          ensure_ascii=False)


def test_xmind_notes_use_natural_language(sample_case):
    blob = _xmind_blob([sample_case], "case")
    assert "打开「知识库配置页」" in blob
    assert "[goto]" not in blob


def test_xmind_full_mode_step_nodes_use_natural_language(sample_case):
    """**full 模式的步骤子节点**必须也是自然语言。

    该模式把每一步展开成一个导图节点 —— 节点标题宽度有限，
    机器标记在这里尤其碍眼（此路径此前无测试覆盖，变异测试漏报过一次）。
    """
    blob = _xmind_blob([sample_case], "full")
    assert "打开「知识库配置页」" in blob
    assert "点击「查看按钮」" in blob
    assert "验证「文档片段弹窗」可见" in blob
    for marker in ("[goto]", "[click]", "[expectVisible]", "page=knowledge", "id=modal"):
        assert marker not in blob, marker


def test_excel_steps_column_uses_natural_language(sample_case):
    """Excel 的「测试步骤」列必须是自然语言（该列是评审主视图）。

    本写入器用**内联字符串**（`t="inlineStr"`），不建 sharedStrings 表，
    因此直接查 sheet XML。
    """
    from prd2tc.export import excel
    data = excel.build_xlsx_bytes([sample_case])
    import zipfile
    from io import BytesIO
    with zipfile.ZipFile(BytesIO(data)) as zf:
        sheets = [n for n in zf.namelist() if n.startswith("xl/worksheets/sheet")]
        assert sheets, zf.namelist()
        blob = "".join(zf.read(n).decode("utf-8") for n in sheets)
    assert "打开「知识库配置页」" in blob
    assert "点击「查看按钮」" in blob
    assert "[goto]" not in blob and "page=knowledge" not in blob


# ---------- 自由文本里的定位符剥离 ----------

def test_strip_locators_removes_kind_value_annotations():
    """模型有时把 `（page=config）` 写进 preconditions/expected —— 导出时同样应剥离。

    实测 68 处：如「已进入智能体配置页（page=config）」。
    """
    from prd2tc.nlsteps import strip_locators as s
    assert s("已进入智能体配置页（page=config）") == "已进入智能体配置页"
    assert s("已打开添加文档知识弹窗（id=modal）") == "已打开添加文档知识弹窗"
    assert s("默认进入知识库配置页(page=knowledge)") == "默认进入知识库配置页"
    assert s("点击按钮 (class=primary) 后提交") == "点击按钮 后提交"


def test_strip_locators_keeps_ordinary_parentheses():
    """裸括号多为正常补充说明，**不得**误删。"""
    from prd2tc.nlsteps import strip_locators as s
    assert s("系统存在账号（正常状态）") == "系统存在账号（正常状态）"
    assert s("支持 txt、docx、xlsx（不含 pdf）") == "支持 txt、docx、xlsx（不含 pdf）"
    assert s("无标注纯文本") == "无标注纯文本"


def test_strip_locators_never_returns_empty():
    """剥离后为空时保留原文 —— 不产出空字段。"""
    from prd2tc.nlsteps import strip_locators as s
    assert s("（page=config）") == "（page=config）"
    assert s("") == ""


def test_clean_items_filters_blank():
    from prd2tc.nlsteps import clean_items
    assert clean_items(["a（id=x）", "", "  ", "b"]) == ["a", "b"]
    assert clean_items(None) == []


def test_markdown_steps_not_double_numbered(sample_case):
    """Markdown 步骤不得出现「1. 1. …」双重编号。"""
    from prd2tc.io import render_cases_md
    md = render_cases_md([sample_case])
    assert "1. 1. " not in md and "- 1. " not in md
    assert "- 打开「知识库配置页」" in md


def test_exports_strip_locators_from_prose_fields():
    """前置条件/预期结果里的定位符在导出物中应被剥离。"""
    from prd2tc.export import excel
    from prd2tc.io import render_cases_md
    case = {
        "id": "TC-X-001", "module": "X", "title": "示例", "design_method": "场景法",
        "priority": "P0", "tags": ["@smoke"], "requirement": ["REQ-X-001"],
        "preconditions": ["已进入智能体配置页（page=config）"],
        "testData": ["doc=x"],
        "steps": [{"action": "goto", "target": "智能体配置页 (page=config)", "data": ""}],
        "expected": ["页签栏可见（class=tabs）"],
    }
    md = render_cases_md([case])
    assert "已进入智能体配置页" in md and "page=config" not in md
    assert "页签栏可见" in md and "class=tabs" not in md

    import zipfile
    from io import BytesIO
    data = excel.build_xlsx_bytes([case])
    with zipfile.ZipFile(BytesIO(data)) as zf:
        blob = "".join(zf.read(n).decode("utf-8")
                       for n in zf.namelist() if n.startswith("xl/worksheets/sheet"))
    assert "page=config" not in blob and "class=tabs" not in blob


def test_structured_json_keeps_machine_format(sample_case):
    """**关键约定**：JSON 里的 steps 仍是结构化的（门禁与自动化依赖它）。"""
    from prd2tc.io import save_cases
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        paths = save_cases(Path(tmp), [sample_case])
        saved = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
    cases = saved if isinstance(saved, list) else saved.get("cases", [])
    steps = cases[0]["steps"]
    assert steps[0] == {"action": "goto", "target": "知识库配置页 (page=knowledge)", "data": ""}
    assert all(isinstance(s, dict) and "action" in s for s in steps)
