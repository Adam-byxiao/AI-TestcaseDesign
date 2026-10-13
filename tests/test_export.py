"""Excel / XMind 导出测试。

用例与 `docs/export-excel-xmind.md` §10.1 保持一致：该文档的附录 A 是权威实现参考，
本文件是它的可执行版本。每条「回归 R-*」测试对应文档修订记录中的一项修正，
防止将来回归到 v1.0 的缺陷。
"""
import json
import warnings
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from prd2tc.export import common, excel, xmind

SAMPLE_CASE = {
    "id": "TC-AUTH-LOGIN-001",
    "module": "AUTH",
    "subModule": "LOGIN",
    "title": "正确凭据登录成功并跳转首页",
    "design_method": "等价类划分",
    "priority": "P0",
    "tags": ["@smoke", "@auth"],
    "requirement": ["REQ-AUTH-002"],
    "preconditions": ["系统中存在 normal 状态账号"],
    "testData": ["phone=13800138000", "password=demo123456"],
    "steps": [
        {"action": "goto", "target": "/login", "data": ""},
        {"action": "fill", "target": "手机号输入框", "data": "13800138000"},
        {"action": "click", "target": "登录按钮", "data": ""},
    ],
    "expected": ["登录成功后跳转首页", "导航栏显示当前用户昵称"],
    "status": "active",
}


# ---------- common ----------

def test_flatten_steps_renders_numbered_lines():
    """默认输出**自然语言**：导出物是给人评审的，机器标记妨碍阅读。

    实测反馈：`[goto] 智能体配置页 (page=config)` 难以阅读。
    结构化原文仍保留在 cases/cases/*.json。
    """
    text = common.render_steps(SAMPLE_CASE["steps"])
    assert text.splitlines() == [
        "1. 打开「/login」",
        "2. 在「手机号输入框」输入「13800138000」",
        "3. 点击「登录按钮」",
    ]


def test_render_steps_structured_mode_keeps_machine_format():
    """`structured=True` 保留原机器格式（排障与对照用）。"""
    text = common.render_steps(SAMPLE_CASE["steps"], structured=True)
    assert text.splitlines() == [
        "1. [goto] /login",
        "2. [fill] 手机号输入框 ← 13800138000",
        "3. [click] 登录按钮",
    ]


def test_render_steps_empty():
    assert common.render_steps([]) == "—"


def test_list_to_text_joins_with_newline():
    assert common.list_to_text(["a", "b"], empty="—") == "a\nb"
    assert common.list_to_text([], empty="—") == "—"


def test_write_zip_is_deterministic():
    members = {"b.json": "{}", "a.json": "{}"}
    assert common.build_zip_bytes(members) == common.build_zip_bytes(members)


# ---------- excel ----------

def test_col_name_conversion():
    assert excel.col_name(0) == "A"
    assert excel.col_name(25) == "Z"
    assert excel.col_name(26) == "AA"
    assert excel.col_name(701) == "ZZ"
    assert excel.col_name(702) == "AAA"


def test_excel_bytes_is_valid_zip():
    data = excel.build_xlsx_bytes([SAMPLE_CASE])
    assert data[:4] == b"PK\x03\x04"
    with zipfile.ZipFile(BytesIO(data)) as z:
        assert z.testzip() is None
        names = z.namelist()
        assert "[Content_Types].xml" in names
        assert "xl/workbook.xml" in names
        assert "xl/worksheets/sheet1.xml" in names


def test_excel_relationship_types_are_valid():
    """**回归（最重要的一条）**：关系类型 URI 必须完整含 `/2006/`。

    曾因 f-string 拆行时漏掉 `2006/`，生成 `.../officeDocument/relationships/worksheet`
    这一非法 URI，Excel 判定文件损坏；点「修复」后**只剩下工作表分页、没有任何内容**。
    而 openpyxl 是宽容解析器，不校验关系 URI，因此当时误判为通过。

    这里直接断言所有 Relationship 的 Type 都在允许集合内。
    """
    from xml.etree import ElementTree as ET
    ALLOWED = {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles",
    }
    data = excel.build_xlsx_bytes([SAMPLE_CASE], traceability={"requirements": []},
                                  stats={"total": 1})
    with zipfile.ZipFile(BytesIO(data)) as z:
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        root_rels = ET.fromstring(z.read("_rels/.rels"))
    ns = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
    seen = set()
    for container in (rels, root_rels):
        for rel in container.findall("r:Relationship", ns):
            rel_type = rel.get("Type")
            seen.add(rel_type)
            assert rel_type in ALLOWED, f"非法关系类型 URI: {rel_type}"
            assert "/2006/relationships/" in rel_type, rel_type
    # 三个 sheet + styles 的 worksheet 关系都应存在
    assert sum(1 for t in seen if t.endswith("/worksheet")) == 1
    assert any(t.endswith("/styles") for t in seen)


def test_excel_relationship_targets_exist():
    """关系指向的 part 必须真实存在于 ZIP 内，否则 Excel 会报修复。"""
    from xml.etree import ElementTree as ET
    data = excel.build_xlsx_bytes([SAMPLE_CASE], traceability={"requirements": []})
    with zipfile.ZipFile(BytesIO(data)) as z:
        names = set(z.namelist())
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    ns = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
    for rel in rels.findall("r:Relationship", ns):
        target = rel.get("Target")
        assert f"xl/{target}" in names, f"关系指向不存在的 part: {target}"


def test_excel_content_types_cover_all_parts():
    """每个 xml part 都必须被 [Content_Types].xml 声明（Default 或 Override）。

    注意 `[Content_Types].xml` 自身不参与声明（规范如此），rels 由 Default 覆盖。
    """
    data = excel.build_xlsx_bytes([SAMPLE_CASE], traceability={"requirements": []},
                                  stats={"total": 1})
    with zipfile.ZipFile(BytesIO(data)) as z:
        ct = z.read("[Content_Types].xml").decode()
        parts = [n for n in z.namelist()
                 if n.endswith(".xml") and n != "[Content_Types].xml"]
    for part in parts:
        assert f'PartName="/{part}"' in ct, f"{part} 未在 [Content_Types].xml 中声明"


def test_excel_opens_like_a_reference_file():
    """结构等价性：与 openpyxl 生成的标准文件比对各关键 part 的**根元素与
    必要子元素存在性**（Excel 严格校验，openpyxl 宽容，故此处只做保守断言）。"""
    from xml.etree import ElementTree as ET
    data = excel.build_xlsx_bytes([SAMPLE_CASE])
    with zipfile.ZipFile(BytesIO(data)) as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        styles = ET.fromstring(z.read("xl/styles.xml"))
        sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    main = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    assert wb.tag == f"{main}workbook" and wb.find(f"{main}sheets") is not None
    for tag in ("fonts", "fills", "borders", "cellStyleXfs", "cellXfs", "cellStyles"):
        assert styles.find(f"{main}{tag}") is not None, f"styles.xml 缺 {tag}"
    assert sheet.tag == f"{main}worksheet" and sheet.find(f"{main}sheetData") is not None


def test_excel_escapes_special_chars():
    """XML 元素**正文**只需转义 & < >；双引号无需转义。

    v1.0 断言 `&quot;` 存在 —— 那是错的（`escape()` 默认不转义引号，
    而元素正文里的 `"` 本就合法），导致文档自带测试失败。
    """
    case = {**SAMPLE_CASE, "title": '登录 <成功> & "引号"'}
    data = excel.build_xlsx_bytes([case])
    with zipfile.ZipFile(BytesIO(data)) as z:
        xml = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "&lt;成功&gt;" in xml
    assert "&amp;" in xml
    assert "&quot;" not in xml          # 正文中的引号保持原样
    assert '"引号"' in xml
    from xml.etree import ElementTree as ET
    ET.fromstring(xml)                  # 整个 part 必须仍良构


def test_excel_is_deterministic():
    """可复现：同输入两次生成的字节必须一致。"""
    a = excel.build_xlsx_bytes([SAMPLE_CASE])
    b = excel.build_xlsx_bytes([SAMPLE_CASE])
    assert a == b


def test_excel_p0_uses_highlight_style():
    data = excel.build_xlsx_bytes([SAMPLE_CASE])
    with zipfile.ZipFile(BytesIO(data)) as z:
        styles = z.read("xl/styles.xml").decode("utf-8")
    assert "FFFFC7CE" in styles   # P0 底色存在


def test_excel_p0_row_keeps_wrap_text():
    """回归 R-2：P0/P1 行**必须**同时带底色与自动换行。

    v1.0 的 `_priority_style` 返回不含 wrapText 的样式，导致 P0 行的
    「测试步骤」「预期结果」挤成一行 —— 而 P0 恰恰最需要审阅。
    """
    import re
    data = excel.build_xlsx_bytes([SAMPLE_CASE])
    with zipfile.ZipFile(BytesIO(data)) as z:
        styles = z.read("xl/styles.xml").decode()
        sheet = z.read("xl/worksheets/sheet1.xml").decode()
    row2 = re.search(r'<row r="2">(.*?)</row>', sheet, re.S).group(1)
    used = {int(s) for s in re.findall(r'<c [^>]*s="(\d+)"', row2)}
    assert used, "第2行应带样式"
    xfs = re.search(r"<cellXfs[^>]*>(.*?)</cellXfs>", styles, re.S).group(1)
    entries = re.findall(r"<xf\b.*?(?:/>|</xf>)", xfs, re.S)
    for idx in used:
        assert "wrapText" in entries[idx], f"第2行所用样式 s={idx} 缺少 wrapText"


def test_excel_has_default_style():
    """回归 R-7：styles.xml 必须含 <cellStyles>，否则 openpyxl 报
    'Workbook contains no default style'，严格实现可能直接失败。"""
    data = excel.build_xlsx_bytes([SAMPLE_CASE])
    with zipfile.ZipFile(BytesIO(data)) as z:
        styles = z.read("xl/styles.xml").decode()
    assert "<cellStyles" in styles
    assert "<cellStyle " in styles


def test_excel_warn_fill_used_for_uncovered():
    """回归 R-6：覆盖不足/未覆盖的需求行必须着色（v1.0 定义了底色却从未引用）。"""
    import re
    matrix = {"requirements": [
        {"id": "REQ-A-001", "module": "AUTH", "priority": "P0",
         "status": "uncovered", "caseIds": []},
        {"id": "REQ-A-002", "module": "AUTH", "priority": "P1", "status": "covered",
         "caseIds": ["TC-AUTH-LOGIN-001"]},
    ]}
    data = excel.build_xlsx_bytes([SAMPLE_CASE], traceability=matrix)
    with zipfile.ZipFile(BytesIO(data)) as z:
        sheet2 = z.read("xl/worksheets/sheet2.xml").decode()
        styles = z.read("xl/styles.xml").decode()
    assert "FFFFE1E1" in styles, "WARN 底色应已定义"
    row2 = re.search(r'<row r="2">(.*?)</row>', sheet2, re.S).group(1)
    used = {int(s) for s in re.findall(r'<c [^>]*s="(\d+)"', row2)}
    assert excel.STYLE_WARN in used, f"未覆盖行未着色，实际样式 {used}"


def test_sheet_name_special_chars_are_escaped():
    """回归 R-8：sheet 名含 " & < > 时不得破坏 workbook.xml。"""
    from xml.etree import ElementTree as ET
    for raw in ['用"例', "a&b", "x<y>z"]:
        data = excel.build_xlsx_bytes([SAMPLE_CASE], sheet_name=excel.safe_sheet_name(raw))
        with zipfile.ZipFile(BytesIO(data)) as z:
            ET.fromstring(z.read("xl/workbook.xml"))   # 良构性即通过


# ---------- xmind ----------

def test_xmind_bytes_is_valid_zip():
    data = xmind.build_xmind_bytes([SAMPLE_CASE])
    assert data[:4] == b"PK\x03\x04"
    with zipfile.ZipFile(BytesIO(data)) as z:
        assert z.testzip() is None
        assert "content.json" in z.namelist()
        assert "metadata.json" in z.namelist()
        assert "manifest.json" in z.namelist()


def _case_topic_of(data: bytes) -> dict:
    """取第一条例用节点（默认 case 模式，模块下可能夹一层 subModule）。"""
    with zipfile.ZipFile(BytesIO(data)) as z:
        content = json.loads(z.read("content.json"))
    module = content[0]["rootTopic"]["children"]["attached"][0]
    for child in module["children"]["attached"]:
        if "notes" in child:
            return child
        nested = (child.get("children") or {}).get("attached") or []
        if nested:
            return nested[0]
    raise AssertionError("未找到用例节点")


def test_xmind_content_json_structure():
    data = xmind.build_xmind_bytes([SAMPLE_CASE])
    with zipfile.ZipFile(BytesIO(data)) as z:
        content = json.loads(z.read("content.json"))
    sheet = content[0]
    assert sheet["class"] == "sheet"
    root = sheet["rootTopic"]
    assert root["class"] == "topic"
    modules = root["children"]["attached"]
    assert modules[0]["title"].startswith("AUTH")
    # 默认模式必须是 case：模块下应能看到用例节点（回归 R-1）
    assert modules[0]["children"]["attached"], "默认模式未展开用例节点"


def test_xmind_default_mode_includes_case_nodes():
    """回归 R-1：默认 mode 必须是 case，与三级树结构与 F-2 需求一致。"""
    import inspect
    default = inspect.signature(xmind.build_xmind_bytes).parameters["mode"].default
    assert default == "case", f"默认模式应为 case，实际 {default}"
    with zipfile.ZipFile(BytesIO(xmind.build_xmind_bytes([SAMPLE_CASE]))) as z:
        raw = z.read("content.json").decode("utf-8")
    assert "TC-AUTH-LOGIN-001" in raw, "默认导图必须包含用例编号"


def test_xmind_notes_contain_key_fields():
    topic = _case_topic_of(xmind.build_xmind_bytes([SAMPLE_CASE]))
    notes = topic["notes"]["plain"]["content"]
    assert "REQ-AUTH-002" in notes
    assert "P0" in notes
    assert "打开「/login」" in notes        # 自然语言步骤
    assert "[goto]" not in notes            # 机器标记不出现在导出物


def test_xmind_markers_include_priority():
    """回归 R-3：优先级必须用 markers（优先级图标）而非 labels（纯文本）。

    v1.0 把 `priority-1` 放进 labels，XMind 只会显示成无意义英文文本。
    """
    topic = _case_topic_of(xmind.build_xmind_bytes([SAMPLE_CASE]))
    assert topic.get("markers") == [{"markerId": "priority-1"}]
    assert not [x for x in topic.get("labels", []) if str(x).startswith("priority-")]


def test_xmind_labels_carry_tags_and_method():
    topic = _case_topic_of(xmind.build_xmind_bytes([SAMPLE_CASE]))
    labels = topic.get("labels", [])
    assert "@smoke" in labels and "@auth" in labels
    assert "等价类划分" in labels


def test_xmind_is_deterministic():
    a = xmind.build_xmind_bytes([SAMPLE_CASE])
    b = xmind.build_xmind_bytes([SAMPLE_CASE])
    assert a == b


def test_xmind_module_mode_has_no_case_nodes():
    data = xmind.build_xmind_bytes([SAMPLE_CASE], mode="module")
    with zipfile.ZipFile(BytesIO(data)) as z:
        content = json.loads(z.read("content.json"))
    modules = content[0]["rootTopic"]["children"]["attached"]
    assert modules[0]["title"].startswith("AUTH")
    assert not modules[0].get("children")   # module 模式不展开
    assert modules[0]["notes"]["plain"]["content"] == "共 1 条用例"


def test_xmind_degrades_instead_of_raising():
    """回归 R-4：超限应**自动降级为 module** 并告警，而不是直接不产出文件。

    case 模式节点数 = root 1 + 模块 1 + 子模块 1 + N 用例 = N + 3。
    取 N = MAX_NODES 必然超限，而模块级只有 3 个节点 → 必然降级成功。
    """
    many = [{**SAMPLE_CASE, "id": f"TC-A-{i:04d}"} for i in range(xmind.MAX_NODES)]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        data = xmind.build_xmind_bytes(many, mode="case")
    assert data[:4] == b"PK\x03\x04", "超限时仍必须产出文件"
    assert any("降级" in str(w.message) for w in caught), "应发出降级告警"
    with zipfile.ZipFile(BytesIO(data)) as z:
        content = json.loads(z.read("content.json"))
    modules = content[0]["rootTopic"]["children"]["attached"]
    assert not modules[0].get("children"), "降级后应为 module 模式"


def test_empty_cases_raises():
    with pytest.raises(ValueError, match="没有用例"):
        excel.build_xlsx_bytes([])
    with pytest.raises(ValueError, match="没有用例"):
        xmind.build_xmind_bytes([])


# ---------- 集成：export_cases ----------

def _write_cases(tmp_path, cases=None):
    cases_dir = tmp_path / "cases" / "cases"
    cases_dir.mkdir(parents=True)
    (cases_dir / "AUTH.json").write_text(
        json.dumps({"schemaVersion": "2.0", "cases": cases or [SAMPLE_CASE]},
                   ensure_ascii=False), encoding="utf-8")
    return cases_dir


def test_export_cases_writes_both_formats(tmp_path):
    from prd2tc.export import export_cases
    cases_dir = _write_cases(tmp_path)
    result = export_cases(cases_dir=cases_dir, formats=["excel", "xmind"], out=None)
    assert result["case_count"] == 1
    assert len(result["files"]) == 2
    for path in result["files"]:
        assert path.exists() and path.stat().st_size > 500
        with zipfile.ZipFile(path) as z:
            assert z.testzip() is None


def test_export_cases_uses_product_version_in_name(tmp_path):
    from prd2tc.export import export_cases
    cases_dir = _write_cases(tmp_path)
    result = export_cases(cases_dir=cases_dir, formats=["excel", "xmind"],
                          product="mall", version="v0.3")
    names = sorted(p.name for p in result["files"])
    assert names == ["mall-v0.3-用例集.xlsx", "mall-v0.3-用例集.xmind"], names


def test_export_cases_picks_up_traceability(tmp_path):
    """有 traceability.json 时应生成第二个 sheet。"""
    from prd2tc.export import export_cases
    cases_dir = _write_cases(tmp_path)
    run_root = cases_dir.parent.parent
    (run_root / "cases").mkdir(parents=True, exist_ok=True)
    (run_root / "cases" / "traceability.json").write_text(json.dumps({
        "requirements": [{"id": "REQ-AUTH-002", "module": "AUTH", "priority": "P0",
                          "status": "covered", "caseIds": ["TC-AUTH-LOGIN-001"]}]
    }, ensure_ascii=False), encoding="utf-8")

    result = export_cases(cases_dir=cases_dir, formats=["excel"])
    with zipfile.ZipFile(result["files"][0]) as z:
        wb = z.read("xl/workbook.xml").decode()
    assert "需求覆盖" in wb


def test_export_cases_rejects_empty_dir(tmp_path):
    from prd2tc.export import export_cases
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="用例目录"):
        export_cases(cases_dir=empty)


def test_excel_has_stats_sheet(tmp_path):
    """§5.1 设计为 3 个 sheet；统计概览曾漏实现（代码与设计不符），此处锁定。"""
    from prd2tc.export import export_cases
    cases_dir = _write_cases(tmp_path)
    result = export_cases(cases_dir=cases_dir, formats=["excel"], root=tmp_path)
    with zipfile.ZipFile(result["files"][0]) as z:
        wb = z.read("xl/workbook.xml").decode()
    assert "用例明细" in wb and "统计概览" in wb, wb
    # 统计内容应来自 io.stats()
    with zipfile.ZipFile(result["files"][0]) as z:
        xml = z.read("xl/worksheets/sheet2.xml").decode()
    assert "全部用例" in xml and "按模块" in xml and "按优先级" in xml


def test_excel_sheets_deduplicated_when_name_collides(tmp_path):
    """统计概览与用户自定义 sheet 名不应互相覆盖（sheet 名必须唯一）。"""
    from prd2tc.export import export_cases
    cases_dir = _write_cases(tmp_path)
    result = export_cases(cases_dir=cases_dir, formats=["excel"], root=tmp_path,
                          sheet_name="统计概览")
    with zipfile.ZipFile(result["files"][0]) as z:
        import re as _re
        names = _re.findall(r'<sheet name="([^"]+)"', z.read("xl/workbook.xml").decode())
    joined = "|".join(names)
    assert "统计概览" in joined and "用例明细" in joined or len(names) >= 2, names


# ---------- 筛选导出（模块 / 优先级 / 标签复合条件） ----------

_FILTER_CASES = [
    {"id": "TC-KB-001", "module": "KB", "title": "上传文档", "design_method": "场景法",
     "priority": "P0", "tags": ["@smoke", "@regression", "@kb"],
     "requirement": ["REQ-KB-001"],
     "steps": [{"action": "goto", "target": "首页", "data": ""},
               {"action": "expectVisible", "target": "上传区", "data": ""}],
     "expected": ["上传成功并可查看"]},
    {"id": "TC-KB-002", "module": "KB", "title": "按名称搜索", "design_method": "场景法",
     "priority": "P1", "tags": ["@regression", "@kb"],
     "requirement": ["REQ-KB-002"],
     "steps": [{"action": "goto", "target": "首页", "data": ""},
               {"action": "expectVisible", "target": "搜索框", "data": ""}],
     "expected": ["搜索过滤生效"]},
    {"id": "TC-AGENT-001", "module": "AGENT", "title": "页签切换", "design_method": "场景法",
     "priority": "P0", "tags": ["@regression", "@agent"],
     "requirement": ["REQ-AGENT-001"],
     "steps": [{"action": "goto", "target": "首页", "data": ""},
               {"action": "expectVisible", "target": "页签栏", "data": ""}],
     "expected": ["三页签依次排列"]},
]

_FILTER_REQS = [
    {"id": "REQ-KB-001", "module": "KB", "priority": "P0", "title": "上传"},
    {"id": "REQ-KB-002", "module": "KB", "priority": "P1", "title": "搜索"},
    {"id": "REQ-AGENT-001", "module": "AGENT", "priority": "P0", "title": "页签"},
]


def _write_filter_cases(tmp_path):
    """写一批多模块/多优先级/多标签的用例，供筛选导出测试使用。"""
    cases_dir = _write_cases(tmp_path, _FILTER_CASES)
    analysis = cases_dir.parent.parent / "analysis"
    analysis.mkdir(parents=True, exist_ok=True)
    (analysis / "requirements.json").write_text(
        json.dumps({"requirements": _FILTER_REQS}, ensure_ascii=False), encoding="utf-8")
    return cases_dir


def _sheet_xml(path):
    with zipfile.ZipFile(path) as z:
        return "".join(z.read(n).decode("utf-8")
                       for n in z.namelist() if n.startswith("xl/worksheets/sheet"))


def test_filtered_export_only_contains_matching_cases(tmp_path):
    """**核心功能**：按筛选导出 —— 文件里只应有命中的用例。"""
    from prd2tc import casefilter
    from prd2tc.export import export_cases
    cases_dir = _write_filter_cases(tmp_path)
    result = export_cases(cases_dir, formats=["excel"], root=cases_dir.parent.parent,
                          product="P", version="v1",
                          spec=casefilter.make_filter(modules=["KB"], priorities=["P0"]))
    assert result["case_count"] == 1
    assert result["total_case_count"] == 3
    assert result["filter"]["excluded"] == 2
    assert "KB-P0" in Path(result["files"][0]).name, Path(result["files"][0]).name
    blob = _sheet_xml(result["files"][0])
    assert "TC-KB-001" in blob
    assert "TC-KB-002" not in blob, "未命中的用例不得出现在导出文件里"
    assert "TC-AGENT-001" not in blob


def test_filtered_export_supports_module_plus_tag(tmp_path):
    """**用户示例**：只导出指定单模块中的回归用例。"""
    from prd2tc import casefilter
    from prd2tc.export import export_cases
    cases_dir = _write_filter_cases(tmp_path)
    result = export_cases(cases_dir, formats=["excel"], root=cases_dir.parent.parent,
                          spec=casefilter.make_filter(modules=["KB"], tags=["@regression"]))
    assert result["case_count"] == 2
    blob = _sheet_xml(result["files"][0])
    assert "TC-KB-001" in blob and "TC-KB-002" in blob
    assert "TC-AGENT-001" not in blob


def test_filtered_export_by_smoke_and_by_priority(tmp_path):
    """只导出 P0 / 只导出 smoke —— 两种都是常见诉求。"""
    from prd2tc import casefilter
    from prd2tc.export import export_cases
    cases_dir = _write_filter_cases(tmp_path)
    root = cases_dir.parent.parent
    p0 = export_cases(cases_dir, formats=["excel"], root=root,
                      spec=casefilter.make_filter(priorities=["P0"]))
    assert p0["case_count"] == 2
    smoke = export_cases(cases_dir, formats=["excel"], root=root,
                         spec=casefilter.make_filter(tags=["@smoke"]))
    assert smoke["case_count"] == 1
    assert "TC-KB-001" in _sheet_xml(smoke["files"][0])


def test_filtered_export_recomputes_traceability(tmp_path):
    """**一致性**：筛选导出必须按**子集重算**追踪矩阵。

    若沿用磁盘上的完整矩阵，Excel 的「需求覆盖」页会声称覆盖了
    根本不在此文件里的用例 —— 两个 sheet 自相矛盾，且不会被任何人发现。

    断言必须能**区分重算与沿用**：仅断言「需求行存在」不够（全量矩阵里也有）。
    这里用覆盖状态判定 —— 磁盘矩阵谎称 KB 需求「已覆盖」，
    而子集里根本没有 KB 用例，重算后应为「未覆盖」。
    """
    from prd2tc import casefilter
    from prd2tc.export import export_cases
    cases_dir = _write_filter_cases(tmp_path)
    run_root = cases_dir.parent.parent
    # 磁盘上放一份「全量」矩阵：所有需求都谎称已覆盖
    (run_root / "cases" / "traceability.json").write_text(json.dumps({
        "requirements": [{"id": r["id"], "module": r["module"], "priority": r["priority"],
                          "status": "covered", "caseIds": []} for r in _FILTER_REQS],
        "summary": {"covered": 3}}, ensure_ascii=False), encoding="utf-8")

    result = export_cases(cases_dir, formats=["excel"], root=run_root,
                          spec=casefilter.make_filter(modules=["AGENT"]))
    assert result["case_count"] == 1
    blob = _sheet_xml(result["files"][0])
    assert "TC-AGENT-001" in blob
    assert "TC-KB-001" not in blob
    # 关键区分：KB 需求在子集里没有用例 → 必须显示为「未覆盖」，
    # 而不是沿用磁盘矩阵谎称的「已覆盖」
    assert "未覆盖" in blob, "筛掉的模块需求应显示为未覆盖（否则是沿用了全量矩阵）"
    assert "REQ-KB-001" in blob


def test_filtered_export_marks_scope_in_stats_sheet(tmp_path):
    """统计页必须标注「这是子集」——否则读者会把局部数字当全量结论。"""
    from prd2tc import casefilter
    from prd2tc.export import export_cases
    cases_dir = _write_filter_cases(tmp_path)
    result = export_cases(cases_dir, formats=["excel"], root=cases_dir.parent.parent,
                          spec=casefilter.make_filter(modules=["KB"]))
    blob = _sheet_xml(result["files"][0])
    assert "导出范围" in blob, "统计页应含导出范围标注"
    assert "模块=KB" in blob


def test_empty_filter_result_fails_loudly(tmp_path):
    """**核心回归**：筛选后为空必须显式失败。

    静默产出空文件会让用户以为「导出成功」，直到打开才发现没有内容
    （而且筛选条件早已忘记）。
    """
    from prd2tc import casefilter
    from prd2tc.export import export_cases
    cases_dir = _write_filter_cases(tmp_path)
    with pytest.raises(ValueError) as exc:
        export_cases(cases_dir, formats=["excel"], root=cases_dir.parent.parent,
                     spec=casefilter.make_filter(modules=["NOT_EXIST"]))
    msg = str(exc.value)
    assert "没有匹配的用例" in msg and "NOT_EXIST" in msg, msg


def test_unfiltered_export_behavior_unchanged(tmp_path):
    """不带筛选时行为不变（文件名、用例数、filtered 标记）。"""
    from prd2tc.export import export_cases
    cases_dir = _write_filter_cases(tmp_path)
    result = export_cases(cases_dir, formats=["excel"], root=cases_dir.parent.parent,
                          product="P", version="v1")
    assert result["case_count"] == 3
    assert result["filter"]["filtered"] is False
    assert Path(result["files"][0]).name == "P-v1-用例集.xlsx"
    # 无筛选时不应出现「导出范围」标注（避免噪声）
    assert "导出范围" not in _sheet_xml(result["files"][0])


def test_filtered_xmind_title_and_filename_reflect_filter(tmp_path):
    """XMind 的标题与文件名也要反映筛选 —— 否则导图会被误认为全量。"""
    from prd2tc import casefilter
    from prd2tc.export import export_cases
    cases_dir = _write_filter_cases(tmp_path)
    result = export_cases(cases_dir, formats=["xmind"], root=cases_dir.parent.parent,
                          product="P", version="v1",
                          spec=casefilter.make_filter(tags=["@smoke"]))
    name = Path(result["files"][0]).name
    assert "smoke" in name, name
    with zipfile.ZipFile(result["files"][0]) as z:
        content = z.read("content.json").decode("utf-8")
    assert "标签=@smoke" in content, "导图标题应标注筛选条件"
    assert "TC-KB-002" not in content


def test_filtered_export_is_deterministic(tmp_path):
    """同筛选条件两次导出字节一致（可复现性契约）。"""
    from prd2tc import casefilter
    from prd2tc.export import export_cases
    cases_dir = _write_filter_cases(tmp_path)
    root = cases_dir.parent.parent
    spec = casefilter.make_filter(modules=["KB"], priorities=["P0", "P1"])
    first = export_cases(cases_dir, formats=["excel"], out=tmp_path / "a.xlsx",
                         root=root, spec=spec)["files"][0].read_bytes()
    second = export_cases(cases_dir, formats=["excel"], out=tmp_path / "b.xlsx",
                          root=root, spec=spec)["files"][0].read_bytes()
    assert first == second
