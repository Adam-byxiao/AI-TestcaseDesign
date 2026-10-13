"""摄取层测试：多格式、多文件、多模态输入的归一化。

背景：此前整条链路只认「单个 Markdown 文本文件」（上传 `ALLOWED_PRD={.md,.markdown,.txt}`、
单文件、强制 UTF-8；loader 只 `read_text`），因此真实 PRD 的常见形态全都进不来：
`.docx`（且内含图片）、Wiki 导出的 `.zip`（md + 图片 + 附件）、图片本身即需求、
以及 `prd.md` + `图片和附件/` 的多文件组合。

本文件用**自包含合成样本**（最小 docx/zip/pdf）固化摄取行为，不依赖外部文件。
"""
import io
import zipfile
import zlib

import pytest

from prd2tc import ingest
from prd2tc.ingest import (Asset, Bundle, KIND_DOCX, KIND_HTML, KIND_IMAGE, KIND_MARKDOWN,
                           KIND_OTHER, KIND_PDF, KIND_TEXT, KIND_ZIP, decode_text,
                           extract_docx, extract_pdf, sniff_kind, to_json)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001")
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def make_docx(*, body: str = "", styles: str = "", media: dict | None = None) -> bytes:
    """构造最小可用 DOCX（含可选内嵌图片）。"""
    default_body = (
        f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        f'<w:r><w:t>知识库配置需求</w:t></w:r></w:p>'
        f'<w:p><w:r><w:t>支持上传 txt、docx、xlsx，单文件不超过 1MB。</w:t></w:r></w:p>'
        f'<w:p><w:pPr><w:outlineLvl w:val="1"/></w:pPr>'
        f'<w:r><w:t>上传窗口字段</w:t></w:r></w:p>'
        f'<w:tbl>'
        f'<w:tr><w:tc><w:p><w:r><w:t>文档名称</w:t></w:r></w:p></w:tc>'
        f'<w:tc><w:p><w:r><w:t>最多 20 字</w:t></w:r></w:p></w:tc></w:tr>'
        f'<w:tr><w:tc><w:p><w:r><w:t>文件</w:t></w:r></w:p></w:tc>'
        f'<w:tc><w:p><w:r><w:t>txt/docx/xlsx</w:t></w:r></w:p></w:tc></w:tr>'
        f'</w:tbl>'
    )
    default_styles = (
        f'<w:style w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
        f'<w:style w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml",
                   f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   f'<w:document xmlns:w="{W}"><w:body>{body or default_body}</w:body></w:document>')
        z.writestr("word/styles.xml",
                   f'<?xml version="1.0" encoding="UTF-8"?><w:styles xmlns:w="{W}">'
                   f'{styles or default_styles}</w:styles>')
        for name, blob in (media or {}).items():
            z.writestr(f"word/media/{name}", blob)
    return buf.getvalue()


def make_pdf(text: str = "知识库配置需要支持上传") -> bytes:
    """构造含 ToUnicode 映射的最小文本型 PDF（Identity-H 风格两字节码）。"""
    bf = "".join(f"<{i:04x}> <{ord(ch):04x}>\n" for i, ch in enumerate(text))
    cmap = (f"/CIDInit /ProcSet findresource begin\n1 beginbfchar\n{bf}endbfchar\nend")
    hexstr = "".join(f"{i:04x}" for i in range(len(text)))
    content = f"BT /F1 12 Tf 72 720 Td <{hexstr}> Tj ET"

    def obj(payload: bytes) -> bytes:
        comp = zlib.compress(payload)
        return b"<< /Length %d >>\nstream\n" % len(comp) + comp + b"\nendstream\n"
    out = bytearray(b"%PDF-1.4\n")
    out += b"1 0 obj\n" + obj(cmap.encode()) + b"endobj\n"
    out += b"2 0 obj\n" + obj(content.encode()) + b"endobj\n"
    out += b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    return bytes(out)


def make_zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, blob in members.items():
            z.writestr(name, blob)
    return buf.getvalue()


# ---------- 格式嗅探 ----------

def test_sniff_kind_prefers_content_over_extension():
    """扩展名常常不可靠（改名、Wiki 导出统一成 .txt），必须看魔数。"""
    assert sniff_kind("whatever.txt", make_docx()) == KIND_DOCX
    assert sniff_kind("a.bin", b"%PDF-1.7\n") == KIND_PDF
    assert sniff_kind("no-extension", PNG) == KIND_IMAGE
    assert sniff_kind("x.dat", make_zip({"a.txt": "x"})) == KIND_ZIP
    assert sniff_kind("mystery.txt", JPEG) == KIND_IMAGE


def test_sniff_kind_empty_zip():
    """空归档只有 EOCD（PK\\x05\\x06）—— 不单独处理会被误判为 other。"""
    assert sniff_kind("empty.zip", b"PK\x05\x06" + b"\x00" * 18) == KIND_ZIP


def test_sniff_kind_text_and_html():
    assert sniff_kind("a.md", "# 标题\n\n正文".encode()) == KIND_MARKDOWN
    assert sniff_kind("noext", b"plain body") == KIND_TEXT
    assert sniff_kind("a.html", b"<html><body>x</body></html>") == KIND_HTML


def test_sniff_kind_unknown_binary():
    assert sniff_kind("noext", b"\x00\x01\x02\x03" * 10) == KIND_OTHER
    assert sniff_kind("a.xlsx", make_zip({"xl/workbook.xml": "<x/>"})) == KIND_OTHER


def test_sniff_kind_broken_zip_is_other():
    assert sniff_kind("a.zip", b"PK\x03\x04garbage-not-a-zip") == KIND_OTHER


# ---------- 编码 ----------

def test_decode_text_handles_common_encodings():
    """PRD 可能来自 Word/记事本（GBK），硬 decode utf-8 会把上传整个拒掉。"""
    assert decode_text("中文".encode("utf-8")) == "中文"
    assert decode_text("\ufeff中文".encode("utf-8")) == "中文"      # BOM
    assert decode_text("中文".encode("gbk")) == "中文"
    assert decode_text("中文€".encode("gb18030")) == "中文€"


def test_decode_text_normalizes_newlines():
    """换行必须统一为 LF。

    真实回归：`Path.read_text()` 走 universal-newlines 会把 CRLF 折成 LF，
    而按字节解码会保留 `\\r\\n`。不归一化会让同一份 PRD 出现两种正文形态，
    导致 sourceRefs 偏移错位、diff 把未改动 PRD 误判为 modified。
    """
    assert decode_text(b"a\r\nb") == "a\nb"
    assert decode_text(b"a\rb") == "a\nb"
    assert decode_text("行1\r\n行2".encode("utf-8")) == "行1\n行2"
    # 与 read_text 的行为对齐
    import tempfile
    from pathlib import Path
    p = Path(tempfile.mkdtemp()) / "x.md"
    p.write_text("# 标题\n\n正文\n", encoding="utf-8")     # Windows 上落盘为 CRLF
    assert decode_text(p.read_bytes()) == p.read_text(encoding="utf-8")


def test_decode_text_never_raises():
    assert isinstance(decode_text(b"\xff\xfe\x00\x01\x02"), str)


# ---------- DOCX ----------

def test_extract_docx_keeps_heading_hierarchy():
    """必须输出 Markdown 标题层级 —— 下游 split_document 依赖 `#` 分节。"""
    text, _assets, _w = extract_docx(make_docx())
    assert text.startswith("# 知识库配置需求"), text[:40]
    assert "## 上传窗口字段" in text


def test_extract_docx_converts_tables():
    text, _assets, _w = extract_docx(make_docx())
    assert "| 文档名称 | 最多 20 字 |" in text
    assert "| --- | --- |" in text
    assert "| 文件 | txt/docx/xlsx |" in text


def test_extract_docx_escapes_pipes_in_cells():
    """单元格内竖线会破坏 Markdown 表格，须转全角。"""
    body = ('<w:tbl><w:tr><w:tc><w:p><w:r><w:t>a|b</w:t></w:r></w:p></w:tc>'
            '<w:tc><w:p><w:r><w:t>c</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
    text, _a, _w = extract_docx(make_docx(body=body))
    assert "a｜b" in text and "a|b" not in text


def test_extract_docx_collects_embedded_images():
    text, assets, _w = extract_docx(make_docx(media={"image1.png": PNG, "image2.jpeg": JPEG}))
    assert {a.name for a in assets} == {"image1.png", "image2.jpeg"}
    assert {a.mime for a in assets} == {"image/png", "image/jpeg"}
    assert all(a.role == "embedded" for a in assets)


def test_extract_docx_ignores_non_image_media():
    _text, assets, warns = extract_docx(make_docx(media={"image1.png": PNG, "data.bin": b"\x00\x01"}))
    assert len(assets) == 1
    assert any("非图片" in w for w in warns)


def test_extract_docx_rejects_broken_input():
    with pytest.raises(ValueError, match="不是有效的 DOCX"):
        extract_docx(b"PK\x03\x04garbage")
    with pytest.raises(ValueError, match="缺少 word/document.xml"):
        bad = io.BytesIO()
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("other.xml", "<x/>")
        extract_docx(bad.getvalue())


def test_extract_docx_normalizes_nbsp():
    body = '<w:p><w:r><w:t>a\u00a0b\u3000c</w:t></w:r></w:p>'
    text, _a, _w = extract_docx(make_docx(body=body))
    assert text == "a b c", repr(text)


# ---------- PDF ----------

def test_extract_pdf_reads_text_with_tounicode():
    """CJK PDF 必须有 ToUnicode CMap 才能还原文字。"""
    text, warns = extract_pdf(make_pdf("知识库配置需要支持上传"))
    assert "知识库配置" in text, text
    assert warns == [], warns


def test_extract_pdf_warns_on_scanned_document():
    """扫描件/纯图 PDF 抽不到文字 → 必须明确告警（而非静默给空文本）。"""
    text, warns = extract_pdf(b"%PDF-1.4\ntrailer\n<<>>\n%%EOF\n")
    assert text == ""
    assert any("扫描件" in w for w in warns), warns


def test_extract_pdf_rejects_non_pdf():
    with pytest.raises(ValueError, match="不是 PDF"):
        extract_pdf(b"not a pdf")


# ---------- ingest：多文件与主文件判定 ----------

def test_ingest_markdown_plus_image_directory(tmp_path):
    """实测现场形态：prd.md + 图片目录。"""
    d = tmp_path / "图片和附件"
    d.mkdir()
    (tmp_path / "prd.md").write_text("# 需求\n\n![智能体配置整体](https://x/A)\n", encoding="utf-8")
    (d / "智能体配置整体.png").write_bytes(PNG)
    (d / "说明.txt").write_text("附件说明", encoding="utf-8")

    b = ingest.ingest([tmp_path / "prd.md", d])
    assert b.primary == "prd.md"
    assert "# 需求" in b.text
    assert "## 附件：说明.txt" in b.text
    roles = {s.name: s.role for s in b.sources}
    assert roles["prd.md"] == "primary"
    assert roles["说明.txt"] == "attachment"
    assert roles["智能体配置整体.png"] == "asset"
    assert [a.name for a in b.images()] == ["智能体配置整体.png"]


def test_ingest_docx_only():
    b = ingest.ingest([("需求文档.docx", make_docx(media={"image1.png": PNG}), "upload")])
    assert b.primary == "需求文档.docx"
    assert "# 知识库配置需求" in b.text
    assert len(b.images()) == 1


def test_ingest_picks_longest_markdown_as_primary():
    b = ingest.ingest([
        ("a.md", "# 短\n".encode(), "u"),
        ("b.md", ("# 长\n\n" + "内容" * 500).encode(), "u"),
    ])
    assert b.primary == "b.md", b.primary
    assert "## 附件：a.md" in b.text


def test_reference_documents_excluded_from_body():
    """**核心回归**：接口/服务类文档必须**不并入需求正文**。

    实测事故：用户的 PRD 把 `rag-service.md`（43KB《Yiwise RAG 服务接口文档》）
    作为附件引用，并明确其定位为「原始资料…便于研发查看请求、响应与回调细节」。
    我们却把它当需求挖 → 该次运行产出 **319 条需求 / 43 个模块**
    （正文仅 4K 字符，实际约 30 条需求），其中还包含该文档里
    **客户专用开关**「蔚来RAG优化」—— 与被测项目毫无关系。
    """
    prd = ("# 需求\n\n## 1 配置\n\n支持保存。\n\n"
           "## 2 RAG 接口参考\n\n"
           "原始资料：下方保留完整 rag-service.md 附件，便于研发查看请求、响应与回调细节。\n")
    api_doc = ("# Yiwise RAG 服务接口文档\n\n## 接口清单\n\n"
               "| 方法 | 路径 |\n| --- | --- |\n"
               "| GET | /health |\n| POST | /v1/rag/context |\n| POST | /v1/rag/context/v2 |\n"
               "| POST | /v1/rag/retrieve |\n| POST | /v1/rag/knowledge-base/upload |\n"
               "| POST | /v1/rag/documents/upload |\n| POST | /v1/rag/segments/upload |\n"
               "| POST | /v1/rag/segments/delete |\n\n"
               "`system` 包含 `# 蔚来RAG优化` 时启用特殊策略。\n")
    b = ingest.ingest([("PRD.md", prd.encode(), "u"),
                       ("rag-service.md", api_doc.encode(), "u")])
    assert "蔚来" not in b.text, "接口文档内容不得进入需求正文"
    assert "接口清单" not in b.text
    assert b.text.startswith("# 需求")
    roles = {s.name: s.role for s in b.sources}
    assert roles["rag-service.md"] == "reference", roles
    assert roles["PRD.md"] == "primary", roles
    assert [r.name for r in b.references] == ["rag-service.md"]
    assert any("参考资料" in w for w in b.warnings), b.warnings
    # 摘要里可见（可追溯），但不计入正文
    assert b.summary()["referenceCount"] == 1


def test_reference_detection_by_endpoint_density():
    """文件名无线索时，按**端点密度**判定（≥8 个 METHOD /path）。"""
    lines = "\n".join(f"| POST | /v1/api/thing{i} | 说明 |" for i in range(10))
    doc = f"# 某文档\n\n## 接口\n\n{lines}\n\n字段说明若干。\n"
    b = ingest.ingest([("PRD.md", "# 需求\n\n要点。\n".encode(), "u"),
                       ("后端的.md", doc.encode(), "u")])
    roles = {s.name: s.role for s in b.sources}
    assert roles["后端的.md"] == "reference", roles


def test_include_references_escape_hatch():
    """`include_references=True` 时参考资料仍可并入（逃生阀）。"""
    doc = "# 接口文档\n\nGET /a\nPOST /b\nGET /c\nPOST /d\nGET /e\nPOST /f\nGET /g\nPOST /h\n"
    b = ingest.ingest([("PRD.md", "# 需求\n".encode(), "u"),
                       ("x-service.md", doc.encode(), "u")],
                      include_references=True)
    roles = {s.name: s.role for s in b.sources}
    assert roles["x-service.md"] == "attachment", roles
    assert "## 附件：x-service.md" in b.text


def test_include_references_reachable_from_cli_and_loader(tmp_path):
    """**接线回归**：`--include-references` 必须真的能从 CLI 一路传到摄取层。

    为什么单独测：逃生阀原先只存在于库 API（`ingest.ingest(..., include_references=True)`），
    **CLI 参数没有注册、`load_bundle`/`GenerateRequest` 也未透传** ——
    而报错与告警文案却明确写着「如需纳入请加 --include-references」，
    用户照做只会得到 `unrecognized arguments`（实测）。

    因此这里断言**整条链路**：CLI 解析 → loader → ingest 角色判定。
    """
    from prd2tc import loader
    from prd2tc.cli import build_parser

    # ① CLI 参数存在且能解析
    args = build_parser().parse_args(
        ["generate", "--prd", "PRD.md", "--include-references", "--dry-run"])
    assert args.include_references is True
    # 默认关闭（参考资料不应被静默并入）
    args_default = build_parser().parse_args(["generate", "--prd", "PRD.md", "--dry-run"])
    assert args_default.include_references is False

    # ② loader 会把它透传下去（这里用真实文件验证角色判定确实变了）
    prd = tmp_path / "PRD.md"
    prd.write_text("# 需求\n\n要点。\n", encoding="utf-8")
    api = tmp_path / "x-service.md"
    api.write_text("# 接口文档\n\nGET /a\nPOST /b\nGET /c\nPOST /d\n"
                   "GET /e\nPOST /f\nGET /g\nPOST /h\n", encoding="utf-8")

    default_bundle = loader.load_bundle([prd, api])
    assert {s.name: s.role for s in default_bundle.sources}["x-service.md"] == "reference"
    assert "## 附件：x-service.md" not in default_bundle.text

    forced = loader.load_bundle([prd, api], include_references=True)
    assert {s.name: s.role for s in forced.sources}["x-service.md"] == "attachment"
    assert "## 附件：x-service.md" in forced.text


def test_generate_request_carries_include_references():
    """`GenerateRequest` 必须携带该开关（否则 pipeline 重建请求时会丢）。"""
    import dataclasses
    from prd2tc.pipeline import GenerateRequest
    field = next((f for f in dataclasses.fields(GenerateRequest)
                  if f.name == "include_references"), None)
    assert field is not None, "GenerateRequest 缺少 include_references 字段"
    assert field.default is False


def test_prototype_html_excluded_from_body():
    """原型 HTML **不进正文**（只解析为 UI 契约）。

    实测：42K 原型源码内联进 prompt 占输入的 57.6%，模型据其 JS/CSS
    抽出「字体栈 Menlo/Consolas/PingFang SC」「#toast z-index=99」
    这类实现细节当需求 —— 那是噪声，不是需求。
    """
    prd = "# 需求\n\n要点：新增页签。\n"
    html = ("<html><head><style>.x{font-family:Menlo}</style></head><body>"
            "<div id='nav'></div><script>const navs=[['a','b']];</script></body></html>")
    b = ingest.ingest([("PRD.md", prd.encode(), "u"),
                       ("prototype.html", html.encode(), "u")])
    assert "<script" not in b.text and "font-family" not in b.text
    roles = {s.name: s.role for s in b.sources}
    assert roles["prototype.html"] == "prototype", roles
    assert [p.name for p in b.prototypes] == ["prototype.html"]
    assert b.summary()["prototypeCount"] == 1


def test_only_references_raises_actionable_error():
    """只给参考资料时必须**明确报错**，而不是把它当需求正文静默产出。

    这正是「蔚来」污染的形态：若静默把接口文档当 PRD，结果全错却看不出来。
    """
    doc = "# 接口文档\n\nGET /a\nPOST /b\nGET /c\nPOST /d\nGET /e\nPOST /f\nGET /g\nPOST /h\n"
    with pytest.raises(ValueError) as exc:
        ingest.ingest([("x-service.md", doc.encode(), "u")])
    msg = str(exc.value)
    assert "未找到需求正文" in msg, msg
    assert "include-references" in msg, msg


def test_ingest_prefers_explicitly_given_file_as_primary():
    """**核心回归**：显式给出的文件必须是主文件，不能只按长度选。

    实测：把「PRD + 图片和附件」一起摄入时，附件 `rag-service.md`（43KB 接口文档）
    比真正的 PRD（17KB）长得多 —— 只看长度会把附件当正文、PRD 降级为附件。

    注意 `rag-service.md` 现在还会被识别为**参考资料**而不并入正文
    （见 `test_reference_documents_excluded_from_body`），
    因此这里用一份**非接口文档**的长附件来验证「主文件判定」本身。
    """
    short_prd = "# 真正的 PRD\n\n需求点：支持上传。\n"
    long_attachment = "# 附录说明\n\n" + "补充说明。" * 3000
    b = ingest.ingest([("probe.md", short_prd.encode(), "u"),
                       ("附录说明.md", long_attachment.encode(), "u")],
                      preferred_primary="probe.md")
    assert b.primary == "probe.md", b.primary
    assert b.text.startswith("# 真正的 PRD")
    assert "## 附件：附录说明.md" in b.text


def test_ingest_prefers_prd_named_file_over_longer_others():
    """名字含 prd/需求 的文件优先于更长的其它 md。"""
    long_other = "# 附录\n\n" + "内容。" * 5000
    b = ingest.ingest([("appendix.md", long_other.encode(), "u"),
                       ("PRD-v2.md", b"# PRD\n", "u")])
    assert b.primary == "PRD-v2.md", b.primary


def test_ingest_prefers_longest_when_no_hint():
    """无线索时仍取最长的 md（保持原行为，语义上更可能承载完整需求）。"""
    b = ingest.ingest([("a.md", b"# a\n", "u"),
                       ("b.md", ("# b\n\n" + "内容" * 500).encode(), "u")])
    assert b.primary == "b.md"


def test_ingest_preferred_primary_ignored_when_absent():
    """preferred 指向不存在的文件时不得报错，回退到常规判定。"""
    b = ingest.ingest([("prd.md", b"# prd\n", "u")], preferred_primary="nope.md")
    assert b.primary == "prd.md"


def test_loader_marks_first_explicit_file_as_primary(tmp_path):
    """`loader.load_bundle` 必须把用户给的第一个文件当作 PRD。"""
    from prd2tc import loader
    prd = tmp_path / "sample-prd.md"
    prd.write_text("# PRD\n\n需求。\n", encoding="utf-8")
    d = tmp_path / "图片和附件"
    d.mkdir()
    # 用非接口文档的长附件验证主文件判定（接口文档会被判为参考资料）
    (d / "附录说明.md").write_text("# 附录\n\n" + "细节。" * 3000, encoding="utf-8")
    (d / "x.png").write_bytes(PNG)
    b = loader.load_bundle([prd, d])
    assert b.primary == "sample-prd.md", b.primary
    assert "## 附件：附录说明.md" in b.text
    assert len(b.images()) == 1


def test_depth_outranks_name_hint():
    """**隔离 depth 的用例**：浅层文件无名称线索，深层文件有名称线索，浅层必须胜出。

    为什么需要这条：其他 depth 用例里浅层文件恰好叫 `PRD.md`，
    「名称线索」会先命中，于是即使把 depth 从评分里删掉测试也照样通过
    （变异测试因此漏报）。本用例让深层文件独占名称线索，从而真正锁住 depth 优先级。

    设计理由：**目录结构是用户意图的直接表达且与语言无关** ——
    把主文档放根目录、把资料放进子目录是通用约定；而名称线索依赖语言
    （需同时覆盖 `prd`/`需求`/`spec`/`requirement`）。故 depth 先于名称线索。
    """
    deep_named = "# 详细需求说明\n\n" + "细节。" * 3000        # 更深、且名字含「需求」
    b = ingest.ingest([
        ("主文档.md", "# 主文档正文\n".encode(), "u", "包/主文档.md"),          # depth 1，无线索
        ("需求详情.md", deep_named.encode(), "u", "包/资料/需求详情.md"),        # depth 2，有线索
    ])
    assert b.primary == "主文档.md", b.primary


def test_depth_from_relpath_prefers_shallower():
    """文件夹上传：`(name, bytes, origin, relpath)` 的层级必须参与主文件判定。

    真实形态：`我的PRD/PRD.md`（depth 0）vs
    `我的PRD/图片和附件/rag-service.md`（depth 1，且内容更长）。
    只看长度会让附件抢占正文。
    """
    long_attach = "# RAG 接口说明\n\n" + "接口细节。" * 3000
    b = ingest.ingest([
        ("PRD.md", b"# \xe4\xb8\xbb\xe9\x9c\x80\xe6\xb1\x82\n", "u", "我的PRD/PRD.md"),
        ("rag-service.md", long_attach.encode(), "u", "我的PRD/图片和附件/rag-service.md"),
    ])
    assert b.primary == "PRD.md", b.primary
    depths = {s.name: s.depth for s in b.sources}
    assert depths == {"PRD.md": 1, "rag-service.md": 2}, depths


def test_depth_from_base_dir_for_disk_paths(tmp_path):
    """磁盘路径：传入 base_dir 后 depth 必须反映真实层级。

    真实回归：运行产物里 `prds/<id>/我的PRD/PRD.md` 与
    `prds/<id>/我的PRD/图片和附件/rag-service.md` 必须被判为 0 与 1；
    否则子目录里的长附件会与 PRD 同级并抢占正文。
    """
    root = tmp_path / "我的PRD"
    (root / "图片和附件").mkdir(parents=True)
    prd = root / "PRD.md"
    prd.write_text("# 主需求\n", encoding="utf-8")
    attach = root / "图片和附件" / "rag-service.md"
    attach.write_text("# RAG\n\n" + "细节。" * 3000, encoding="utf-8")

    b = ingest.ingest([prd, root / "图片和附件"], base_dir=root)
    names = {s.name: s.depth for s in b.sources}
    assert names.get("PRD.md") == 0, names
    assert names.get("rag-service.md") == 1, names
    assert b.primary == "PRD.md", b.primary


def test_loader_common_base_computes_depth(tmp_path):
    """`loader.load_bundle` 自动求共同父目录，使磁盘路径也有层级。"""
    from prd2tc import loader
    root = tmp_path / "我的PRD"
    (root / "图片和附件").mkdir(parents=True)
    (root / "PRD.md").write_text("# 主需求\n", encoding="utf-8")
    (root / "图片和附件" / "rag-service.md").write_text(
        "# RAG\n\n" + "细节。" * 3000, encoding="utf-8")
    (root / "图片和附件" / "a.png").write_bytes(PNG)

    b = loader.load_bundle([root / "PRD.md", root / "图片和附件"])
    assert b.primary == "PRD.md", b.primary
    depths = {s.name: s.depth for s in b.sources}
    assert depths.get("PRD.md") == 0, depths
    assert depths.get("rag-service.md") == 1, depths
    assert len(b.images()) == 1


def test_ingest_prefers_markdown_then_docx_then_pdf():
    """无 prd 名线索时按格式优先级：md → docx → pdf。"""
    md, docx, pdf = ("a.md", b"# m\n", "u"), ("b.docx", make_docx(), "u"), ("c.pdf", make_pdf(), "u")
    assert ingest.ingest([pdf, docx, md]).primary == "a.md"
    assert ingest.ingest([pdf, docx]).primary == "b.docx"
    assert ingest.ingest([pdf]).primary == "c.pdf"


def test_ingest_accepts_two_and_three_tuples():
    """(name, bytes) 与 (name, bytes, origin) 都要支持 —— 上传层会带来源标记。"""
    b2 = ingest.ingest([("p.md", b"# a\n")])
    b3 = ingest.ingest([("p.md", b"# a\n", "upload:form")])
    assert b2.primary == b3.primary == "p.md"
    assert b3.sources[0].origin == "upload:form"


def test_ingest_directory_ignores_hidden_and_macos(tmp_path):
    (tmp_path / "prd.md").write_text("# 主\n", encoding="utf-8")
    (tmp_path / "._meta").write_bytes(b"junk")
    (tmp_path / ".hidden.md").write_text("# 隐藏\n", encoding="utf-8")
    mac = tmp_path / "__MACOSX"
    mac.mkdir()
    (mac / "x.md").write_text("# mac\n", encoding="utf-8")
    b = ingest.ingest([tmp_path])
    names = {s.name for s in b.sources}
    assert "prd.md" in names
    assert "._meta" not in names and ".hidden.md" not in names and "x.md" not in names


# ---------- ingest：压缩包 ----------

def test_ingest_expands_zip_with_images():
    z = make_zip({"prd.md": "# 主需求\n\n![图](https://x/A)\n",
                  "docs/智能体配置整体.png": PNG})
    b = ingest.ingest([("bundle.zip", z, "upload")])
    assert b.primary == "prd.md"
    assert len(b.images()) == 1


def test_ingest_expands_nested_zip():
    inner = make_zip({"inner.md": "# 内层\n"})
    outer = make_zip({"docs/prd.md": "# 主\n", "nested.zip": inner})
    b = ingest.ingest([("outer.zip", outer, "upload")])
    assert "inner.md" in {s.name for s in b.sources}


def test_ingest_dedupes_identical_members():
    """同名同内容（Wiki 导出常见）不应重复进正文。"""
    content = "# 主需求\n"
    z = make_zip({"prd.md": content, "docs/prd-copy.md": content})
    b = ingest.ingest([("b.zip", z, "upload")])
    dupes = [s for s in b.sources if s.note == "重复内容"]
    assert dupes, [(s.name, s.note) for s in b.sources]


def test_ingest_ignores_zip_depth_bomb():
    """嵌套超限必须停止，不得无限递归。"""
    payload = make_zip({"x.md": "# deep\n"})
    for _ in range(ingest.MAX_ZIP_DEPTH + 2):
        payload = make_zip({"nested.zip": payload, "leaf.md": "# leaf\n"})
    b = ingest.ingest([("bomb.zip", payload, "upload")])
    assert any("嵌套层级" in w for w in b.warnings), b.warnings


def test_ingest_reports_corrupt_zip():
    b = ingest.ingest([("ok.md", b"# ok\n", "u"), ("bad.zip", b"PK\x03\x04garbage", "u")])
    assert b.primary == "ok.md"
    assert any("格式不受支持" in w or "解压失败" in w for w in b.warnings)


# ---------- ingest：多模态与边界 ----------

def test_ingest_image_only_is_valid_multimodal_input():
    """纯图片 PRD：正文为空但资产就位，且必须明确告警。"""
    b = ingest.ingest([("需求截图.png", PNG, "upload")])
    assert b.text == ""
    assert b.primary == "需求截图.png"
    assert len(b.images()) == 1
    assert any("纯图片 PRD" in w for w in b.warnings), b.warnings


def test_ingest_empty_input_raises():
    with pytest.raises(ValueError, match="没有可用的输入"):
        ingest.ingest([])


def test_ingest_all_inputs_failed_reports_reason():
    """全部失败时错误信息必须带上原因，否则调用方无从排查。"""
    with pytest.raises(ValueError) as exc:
        ingest.ingest(["/no/such/file-xyz.md"])
    assert "没有可用的输入" in str(exc.value), str(exc.value)
    assert "不存在" in str(exc.value) or "跳过" in str(exc.value), str(exc.value)


def test_ingest_binary_only_input_reports_reason():
    """输入存在但无任何可用内容时，也必须说明「没有可解析的内容」。"""
    with pytest.raises(ValueError, match="没有可解析的文本或图片内容"):
        ingest.ingest([("binary.bin", b"\x00\x01\x02\x03" * 10, "u")])


def test_ingest_unsupported_format_warns_not_raises():
    b = ingest.ingest([("ok.md", b"# ok\n", "u"), ("x.xlsx", make_zip({"xl/workbook.xml": "<x/>"}), "u")])
    assert b.primary == "ok.md"
    assert any("格式不受支持" in w for w in b.warnings), b.warnings


def test_ingest_truncates_oversized_text():
    big = ("# 巨大\n\n" + "内容" * (ingest.MAX_TEXT_CHARS // 2)).encode()
    b = ingest.ingest([("big.md", big, "u")])
    assert len(b.text) <= ingest.MAX_TEXT_CHARS
    assert any("已截断" in w for w in b.warnings), b.warnings


def test_ingest_rejects_oversized_file(tmp_path):
    p = tmp_path / "huge.md"
    p.write_bytes(b"x" * (ingest.MAX_SOURCE_BYTES + 1))
    b = ingest.ingest([("ok.md", b"# ok\n", "u"), p])
    assert any("超过上限" in w for w in b.warnings), b.warnings
    assert b.primary == "ok.md"     # 单个超限文件不得影响其它输入


# ---------- 指纹与确定性 ----------

def test_fingerprint_covers_text_and_asset_content():
    """换了图片内容必须改变指纹 —— 否则会复用按旧图片产出的检查点。"""
    base = ingest.ingest([("prd.md", b"# A\n", "u")])
    same = ingest.ingest([("prd.md", b"# A\n", "u")])
    diff_text = ingest.ingest([("prd.md", b"# B\n", "u")])
    with_img = ingest.ingest([("prd.md", b"# A\n", "u"), ("x.png", PNG, "u")])
    with_other_img = ingest.ingest([("prd.md", b"# A\n", "u"), ("x.png", JPEG, "u")])
    assert base.fingerprint() == same.fingerprint()
    assert base.fingerprint() != diff_text.fingerprint()
    assert base.fingerprint() != with_img.fingerprint()
    assert with_img.fingerprint() != with_other_img.fingerprint()


def test_ingest_is_deterministic():
    args = [("prd.md", b"# A\n", "u"), ("b.png", PNG, "u")]
    assert ingest.ingest(args).fingerprint() == ingest.ingest(args).fingerprint()


def test_json_serialization_excludes_asset_bytes():
    b = ingest.ingest([("prd.md", b"# A\n", "u"), ("x.png", PNG, "u")])
    data = to_json(b)
    assert data["summary"]["imageCount"] == 1
    assert data["assets"][0]["mime"] == "image/png"
    assert "data" not in data["assets"][0]          # 不把图片字节塞进 manifest
    assert data["fingerprint"] == b.fingerprint()
    assert isinstance(data["sources"], list)


def test_bundle_summary_fields():
    b = ingest.ingest([("prd.md", b"# A\n", "u"), ("x.png", PNG, "u")])
    s = b.summary()
    assert s["primary"] == "prd.md"
    assert s["assetCount"] == 1 and s["imageCount"] == 1
    assert KIND_MARKDOWN in s["kinds"] and KIND_IMAGE in s["kinds"]


def test_asset_helpers():
    a = Asset(name="x.png", data=PNG, mime="image/png")
    assert a.size == len(PNG)
    assert a.sha256 == ingest.hashlib.sha256(PNG).hexdigest()
    assert not a.mime.startswith("image/") is False
