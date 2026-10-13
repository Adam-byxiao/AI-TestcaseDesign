"""Excel（.xlsx / OOXML）导出：纯标准库实现，不依赖 openpyxl。

.xlsx 本质是 ZIP 容器 + 若干 XML part，导出只需写 OOXML 的**子集**
（单/多 sheet、inlineStr、无公式、无图表、无共享字符串），故无需第三方库。
已用 openpyxl 交叉验证过产物可被严格解析器正常读取。
"""
from __future__ import annotations

import re
from typing import Any
from xml.sax.saxutils import escape

from prd2tc.export.common import EMPTY, build_zip_bytes, list_to_text, render_steps
from prd2tc.nlsteps import clean_items

EXCEL_MAX_ROWS = 1_048_576

# 样式索引（与下方 _STYLES 的 cellXfs 顺序一一对应）
STYLE_NORMAL = 0   # 普通（带换行）
STYLE_HEADER = 1   # 表头（加粗居中）
STYLE_P0 = 2       # P0 红底 + 换行
STYLE_P1 = 3       # P1 橙底 + 换行
STYLE_WARN = 4     # 覆盖不足 + 换行
STYLE_WRAP = 5     # 纯换行（兜底）

P0_FILL = "FFFFC7CE"    # 浅红：P0 用例
P1_FILL = "FFFFEB9C"    # 浅黄：P1 用例
WARN_FILL = "FFFFE1E1"  # 浅红：覆盖不足/未覆盖的需求

# OOXML 关系类型命名空间前缀。
# ⚠️ 必须**完整包含 `/2006/`**：曾因 f-string 拆行时漏掉它而生成非法 URI
# （.../officeDocument/relationships/worksheet），Excel 判文件损坏；
# 「修复」后只剩下工作表分页、没有任何内容。故提为常量，杜绝再次拆行出错。
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

COLUMNS: list[tuple[str, int, str]] = [
    # (标题, 列宽, 字段键)
    ("用例编号", 18, "id"),
    ("模块", 12, "module"),
    ("子模块", 12, "subModule"),
    ("用例标题", 40, "title"),
    ("设计方法", 14, "design_method"),
    ("优先级", 8, "priority"),
    ("标签", 20, "tags"),
    ("关联需求", 24, "requirement"),
    ("前置条件", 32, "preconditions"),
    ("测试数据", 24, "testData"),
    ("测试步骤", 52, "steps"),
    ("预期结果", 40, "expected"),
    ("状态", 12, "status"),
]

# Excel sheet 名非法字符： : \ / ? * [ ]
_ILLEGAL_SHEET_CHARS = re.compile(r"[:\\/?*\[\]]")


def safe_sheet_name(value: str, fallback: str = "sheet") -> str:
    """清洗 Excel sheet 名：替换非法字符并截断到 31 字符上限。

    注意：这里只处理 Excel 自身的命名限制，XML 属性转义由 `_attr()` 负责，
    两者缺一不可（只做前者会让含引号的名字把 workbook.xml 写坏）。
    """
    cleaned = _ILLEGAL_SHEET_CHARS.sub("-", str(value or "")).strip()
    return cleaned[:31] or fallback


def _attr(value: str) -> str:
    """XML **属性值**转义：属性里必须额外转义双引号（元素正文则不必）。"""
    return escape(str(value), {'"': "&quot;"})


_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
{sheets}<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets>{sheets}</sheets>
</workbook>"""

_WB_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
{rels}
</Relationships>"""

_WRAP = '<alignment wrapText="1" vertical="top"/>'

# 每个强调样式都自带 wrapText，不依赖回退到 STYLE_WRAP。
# 否则 P0/P1 行的「测试步骤」「预期结果」会挤成一行——而 P0 恰恰最需要审阅。
_STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="3">
<font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font>
<font><b/><color rgb="FF9C0006"/><sz val="11"/><name val="Calibri"/></font>
</fonts>
<fills count="5">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="{P0_FILL}"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="{P1_FILL}"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="{WARN_FILL}"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="6">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">{_WRAP}</xf>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="0" fontId="2" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1">{_WRAP}</xf>
<xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1" applyAlignment="1">{_WRAP}</xf>
<xf numFmtId="0" fontId="0" fillId="4" borderId="0" xfId="0" applyFill="1" applyAlignment="1">{_WRAP}</xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">{_WRAP}</xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
<dxfs count="0"/>
<tableStyles count="0" defaultTableStyle="TableStyleMedium2" defaultPivotStyle="PivotStyleLight16"/>
</styleSheet>"""


def col_name(idx: int) -> str:
    """列序号 → 列名：0→A, 25→Z, 26→AA, 702→AAA。"""
    name = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        name = chr(65 + rem) + name
    return name


def _cell(ref: str, value: Any, style: int) -> str:
    return (
        f'<c r="{ref}" s="{style}" t="inlineStr">'
        f'<is><t xml:space="preserve">{escape(str(value))}</t></is></c>'
    )


def _sheet_xml(rows: list[list[str]], *, widths: list[int], freeze: bool,
               autofilter: bool, row_style_fn=None) -> str:
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
    if freeze:
        parts.append(
            '<sheetViews><sheetView workbookViewId="0">'
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '</sheetView></sheetViews>'
        )
    # cols 必须位于 sheetData 之前（OOXML 元素顺序要求）
    cols = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
        for i, w in enumerate(widths)
    )
    if cols:
        parts.append(f"<cols>{cols}</cols>")
    parts.append("<sheetData>")
    for r_index, row in enumerate(rows, 1):
        style = STYLE_HEADER if r_index == 1 else None
        if style is None and row_style_fn is not None:
            style = row_style_fn(row)
        parts.append(f'<row r="{r_index}">')
        for c_index, value in enumerate(row):
            cell_style = style if style is not None else STYLE_NORMAL
            parts.append(_cell(f"{col_name(c_index)}{r_index}", value, cell_style))
        parts.append("</row>")
    parts.append("</sheetData>")
    # autoFilter 必须位于 sheetData 之后
    if autofilter and rows:
        last_col = col_name(len(rows[0]) - 1)
        parts.append(f'<autoFilter ref="A1:{last_col}{len(rows)}"/>')
    parts.append("</worksheet>")
    return "".join(parts)


def _case_row(case: dict) -> list[str]:
    return [
        str(case.get("id") or EMPTY),
        str(case.get("module") or EMPTY),
        str(case.get("subModule") or EMPTY) if case.get("subModule") else EMPTY,
        str(case.get("title") or EMPTY),
        str(case.get("design_method") or EMPTY) if case.get("design_method") else EMPTY,
        str(case.get("priority") or EMPTY),
        " ".join(str(t) for t in (case.get("tags") or [])) or EMPTY,
        ", ".join(str(r) for r in (case.get("requirement") or [])) or EMPTY,
        # 自由文本字段也剥离定位符：模型有时把 `（page=config）` 写进这些字段，
        # 在评审列里同样碍眼（JSON 原文保留）。
        list_to_text(clean_items(case.get("preconditions"))),
        list_to_text(clean_items(case.get("testData"))),
        render_steps(case.get("steps")),
        list_to_text(clean_items(case.get("expected"))),
        str(case.get("status") or "active"),
    ]


def _case_rows_with_header(cases: list[dict]) -> list[list[str]]:
    return [[title for title, _, _ in COLUMNS]] + [_case_row(c) for c in cases]


_STYLE_BY_PRIORITY = {"P0": STYLE_P0, "P1": STYLE_P1}


def _priority_style(row: list[str]) -> int:
    """按优先级着色：P0 浅红、P1 浅黄、其余常规。

    注意 STYLE_P0/STYLE_P1 **自带 wrapText**，否则 P0/P1 行的多行单元格会挤成一行。
    """
    priority = row[5] if len(row) > 5 else ""
    return _STYLE_BY_PRIORITY.get(priority, STYLE_NORMAL)


_STATUS_CN = {"covered": "已覆盖", "insufficient": "覆盖不足",
              "uncovered": "未覆盖", "excluded": "已排除"}
# 需要高亮的状态（中文值）：这两类才是评审要盯的
_STATUS_CN_WARN = {_STATUS_CN["insufficient"], _STATUS_CN["uncovered"]}


def _coverage_style(row: list[str]) -> int:
    """需求覆盖 sheet：覆盖不足/未覆盖 → 浅红底（STYLE_WARN，自带换行）。"""
    status = row[3] if len(row) > 3 else ""
    return STYLE_WARN if status in _STATUS_CN_WARN else STYLE_NORMAL


def build_traceability_rows(matrix: dict) -> list[list[str]]:
    """需求覆盖矩阵 → 表格行。"""
    rows = [["需求编号", "模块", "优先级", "覆盖状态", "覆盖用例数", "用例编号列表"]]
    for req in matrix.get("requirements") or []:
        cases = req.get("caseIds") or []
        rows.append([
            str(req.get("id") or EMPTY),
            str(req.get("module") or EMPTY),
            str(req.get("priority") or EMPTY),
            _STATUS_CN.get(req.get("status") or "", str(req.get("status") or EMPTY)),
            str(len(cases)),
            ", ".join(str(c) for c in cases) or EMPTY,
        ])
    return rows


def build_stats_rows(stats: dict) -> list[list[str]]:
    """统计概览 → 表格行（对应 docs/export-excel-xmind.md §9.3）。

    `stats["scope"]` 存在时（筛选导出）置于**首行**：读者必须先知道
    「这是子集」，否则会把局部数字误当全量结论。
    """
    rows = [["统计维度", "分类", "数量"]]
    scope = stats.get("scope")
    if scope:
        rows.append(["导出范围", str(scope), str(stats.get("total", 0))])
    rows.append(["总计", "全部用例", str(stats.get("total", 0))])
    labels = {"by_module": "按模块", "by_priority": "按优先级", "by_tag": "按标签"}
    for key, label in labels.items():
        for name, count in (stats.get(key) or {}).items():
            rows.append([label, str(name), str(count)])
    rows.append(["需求覆盖", "覆盖 REQ 数", str(stats.get("req_covered", 0))])
    return rows


def _dedupe_sheet_names(sheets: list[tuple]) -> list[tuple]:
    """确保 sheet 名唯一：重名时追加 -2、-3…（Excel 要求唯一，否则文件判损坏）。"""
    seen: dict[str, int] = {}
    output = []
    for title, rows, widths, style_fn in sheets:
        base = str(title)
        if base in seen:
            seen[base] += 1
            candidate = f"{base[:28]}-{seen[base]}"      # 留出后缀空间，仍 ≤31 字符
            while candidate in seen:
                seen[base] += 1
                candidate = f"{base[:28]}-{seen[base]}"
            seen[candidate] = 1
            title = candidate
        else:
            seen[base] = 1
        output.append((title, rows, widths, style_fn))
    return output


def build_xlsx_bytes(cases: list[dict], *, traceability: dict | None = None,
                     stats: dict | None = None,
                     sheet_name: str = "用例明细") -> bytes:
    """用例列表 → xlsx 字节流。纯函数，不写盘。

    sheet_name 在此处再清洗一次（Excel 禁止 : \\ / ? * [ ]）；
    属性级 XML 转义由 `_attr()` 负责。
    """
    if not cases:
        raise ValueError("没有用例可导出")
    if len(cases) + 1 > EXCEL_MAX_ROWS:
        raise ValueError(f"用例数 {len(cases)} 超出单 Sheet 上限 {EXCEL_MAX_ROWS}")

    # 每个 sheet 带自己的行样式函数：用例明细按优先级着色，需求覆盖按覆盖状态着色
    sheets = [(safe_sheet_name(sheet_name), _case_rows_with_header(cases),
               [w for _, w, _ in COLUMNS], _priority_style)]
    if traceability:
        sheets.append(("需求覆盖", build_traceability_rows(traceability),
                       [16, 12, 8, 12, 10, 40], _coverage_style))
    if stats:
        sheets.append(("统计概览", build_stats_rows(stats), [16, 20, 10], None))
    # sheet 名必须唯一：若用户的 --sheet-name 与内置名撞车，Excel 会判定文件损坏
    sheets = _dedupe_sheet_names(sheets)

    sheet_entries = []
    rel_entries = []
    for i, (title, _, _, _) in enumerate(sheets, 1):
        sheet_entries.append(f'<sheet name="{_attr(title)}" sheetId="{i}" r:id="rId{i}"/>')
        rel_entries.append(
            f'<Relationship Id="rId{i}" Type="{REL_NS}/worksheet" '
            f'Target="worksheets/sheet{i}.xml"/>'
        )
    style_rid = len(sheets) + 1
    rel_entries.append(
        f'<Relationship Id="rId{style_rid}" Type="{REL_NS}/styles" Target="styles.xml"/>'
    )

    members = {
        "[Content_Types].xml": _CONTENT_TYPES.format(
            sheets="".join(
                f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/'
                f'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for i in range(1, len(sheets) + 1)
            )
        ),
        "_rels/.rels": _ROOT_RELS,
        "xl/workbook.xml": _WORKBOOK_XML.format(sheets="".join(sheet_entries)),
        "xl/_rels/workbook.xml.rels": _WB_RELS_XML.format(rels="".join(rel_entries)),
        "xl/styles.xml": _STYLES,
    }
    for i, (title, rows, widths, style_fn) in enumerate(sheets, 1):
        members[f"xl/worksheets/sheet{i}.xml"] = _sheet_xml(
            rows, widths=widths, freeze=(i == 1), autofilter=(i == 1),
            row_style_fn=style_fn,
        )
    return build_zip_bytes(members)
