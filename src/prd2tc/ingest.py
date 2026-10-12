"""PRD 摄取（ingestion）：把**多格式、多文件**的输入归一化为「文本 + 资产」。

## 为什么需要它

此前整条链路只认「单个 Markdown 文本文件」：

| 层 | 原约束 |
|---|---|
| 上传 | `ALLOWED_PRD = {.md,.markdown,.txt}`、单文件、强制 `.decode("utf-8-sig")` |
| 存储 | `prds/prd-xxx.md`（一个文件一份文本） |
| loader | `read_text(encoding="utf-8")` |
| 阶段 1 | 文本直接进 prompt |

于是**真实 PRD 的常见形态全都进不来**：

- `.docx` —— PRD 最常见的交付格式（且内含图片）；
- `.zip` —— 从 Wiki/飞书导出的「md + 图片 + 附件」包；
- 图片 —— 需求本身就是截图（多模态 PRD）；
- 多文件 —— `prd.md` + `图片和附件/`（实测现场就是这种）；
- `.pdf` —— 冻结评审版。

本模块把「格式」与「生成」解耦：**摄取层负责把任意输入变成统一证据，
生成层只面对 `Bundle(text, assets)`。** 生成逻辑因此不必关心来源格式。

## 支持矩阵

| 格式 | 处理方式 | 依赖 |
|---|---|---|
| `.md` / `.markdown` / `.txt` | 直接作正文 | stdlib |
| `.docx` / `.docm` | ZIP+XML → **Markdown**（保留标题层级、表格），并抽出 `word/media/*` 图片 | stdlib |
| `.html` / `.htm` | 静态取正文；交 `prototype.py` 抽 UI 契约 | stdlib |
| `.pdf` | 内容流解压 + `ToUnicode` CMap → 文本（best-effort，质量低会告警） | stdlib |
| `.png/.jpg/.gif/.webp/.bmp` | 作为**资产**，交 `images.py` 视觉识别 | stdlib + 视觉模型 |
| `.zip` | 递归展开（限深），逐成员按上表处理 | stdlib |
| 目录 | 递归遍历（忽略隐藏文件与 `__MACOSX`） | stdlib |

零第三方依赖。所有输出**确定性**（成员排序固定），保证可复现与 `configHash` 稳定。
"""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

MAX_SOURCE_BYTES = 64 * 1024 * 1024      # 单个输入上限（防超大文件吃内存）
MAX_ZIP_DEPTH = 3                        # 嵌套压缩包深度
MAX_MEMBERS = 500                        # 单个压缩包成员上限
MAX_TEXT_CHARS = 2_000_000               # 归一化后正文上限，防上下文爆炸

KIND_MARKDOWN = "markdown"
KIND_TEXT = "text"
KIND_HTML = "html"
KIND_DOCX = "docx"
KIND_PDF = "pdf"
KIND_IMAGE = "image"
KIND_ZIP = "zip"
KIND_OTHER = "other"

TEXT_KINDS = {KIND_MARKDOWN, KIND_TEXT, KIND_HTML, KIND_DOCX, KIND_PDF}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# 图片魔数（与 images.py 保持一致，但此处不导入以免循环依赖）
_MAGIC_IMAGE = (
    b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM",
)

_EXT_KIND = {
    ".md": KIND_MARKDOWN, ".markdown": KIND_MARKDOWN, ".mdown": KIND_MARKDOWN,
    ".txt": KIND_TEXT, ".text": KIND_TEXT, ".log": KIND_TEXT,
    ".html": KIND_HTML, ".htm": KIND_HTML,
    ".docx": KIND_DOCX, ".docm": KIND_DOCX,
    ".pdf": KIND_PDF,
    ".zip": KIND_ZIP,
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Source:
    """一个输入来源（文件 / 压缩包成员 / 上传项）。"""
    name: str
    kind: str
    size: int
    sha256: str
    origin: str = ""          # 磁盘路径或 "upload" / "zip:a.zip"
    role: str = "primary"     # primary | attachment | asset | skipped
    textChars: int = 0
    note: str = ""

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class Asset:
    """二进制资产（图片等），供视觉识别使用。"""
    name: str
    data: bytes = field(repr=False, default=b"")
    mime: str = ""
    origin: str = ""
    role: str = "prd-image"   # prd-image | embedded | attachment

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass
class Bundle:
    """归一化后的摄取结果：生成层只面对这一结构。"""
    text: str = ""                       # PRD 正文（附件已按节内联）
    sources: list = field(default_factory=list)     # [Source]
    assets: list = field(default_factory=list)      # [Asset]
    warnings: list = field(default_factory=list)
    primary: str = ""                    # 主文件（正文来源）名称

    def images(self) -> list:
        return [a for a in self.assets if a.mime.startswith("image/")]

    def summary(self) -> dict:
        return {
            "primary": self.primary,
            "textChars": len(self.text),
            "sourceCount": len(self.sources),
            "assetCount": len(self.assets),
            "imageCount": len(self.images()),
            "kinds": sorted({s.kind for s in self.sources}),
            "warnings": len(self.warnings),
        }

    def fingerprint(self) -> str:
        """内容指纹：覆盖正文 + 全部资产的**内容**（而非文件名）。

        用于 `configHash`：换了图片或改了附件都必须使旧检查点失效。
        """
        parts = [self.text]
        for s in sorted(self.sources, key=lambda x: (x.name, x.sha256)):
            parts.append(f"{s.name}:{s.kind}:{s.sha256}")
        for a in sorted(self.assets, key=lambda x: (x.name, x.sha256)):
            parts.append(f"{a.name}:{a.sha256}")
        return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 格式嗅探
# ---------------------------------------------------------------------------

def sniff_kind(name: str, data: bytes) -> str:
    """按**内容**优先、扩展名兜底判定类型。

    内容优先很重要：上传的文件扩展名常常不可靠（改名、无扩展名、
    或 Wiki 导出时被统一改成 `.txt`），而魔数不会说谎。
    """
    if data[:4] == b"PK\x05\x06":
        # 空归档只有 EOCD、没有本地文件头；若不单独处理会被判为 other
        return KIND_ZIP
    if data[:4] == b"PK\x03\x04":
        # OOXML 与 zip 同为 PK；用内部条目区分
        # 注意切片必须取 4 字节：写成 data[:2] 与 4 字节常量比较会**永远为假**，
        # 导致 docx/zip 全部落空（实测被测试抓出）。
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = set(z.namelist())
            if "word/document.xml" in names:
                return KIND_DOCX
            if "xl/workbook.xml" in names or "ppt/presentation.xml" in names:
                return KIND_OTHER      # excel/ppt 暂不支持，明确归类
            return KIND_ZIP
        except zipfile.BadZipFile:
            return KIND_OTHER
    if data[:5] == b"%PDF-":
        return KIND_PDF
    if any(data.startswith(m) for m in _MAGIC_IMAGE):
        return KIND_IMAGE
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return KIND_IMAGE
    suffix = Path(name).suffix.lower()
    if suffix in _EXT_KIND:
        return _EXT_KIND[suffix]
    # 无扩展名或未知扩展名：看内容是否像文本
    head = data[:4096]
    if b"\x00" in head:
        return KIND_OTHER
    try:
        head.decode("utf-8")
        return KIND_MARKDOWN if re.search(rb"^#{1,6}\s", head, re.M) else KIND_TEXT
    except UnicodeDecodeError:
        return KIND_OTHER


def decode_text(data: bytes) -> str:
    """稳健解码：优先 UTF-8（含 BOM），失败则尝试常见中文编码。

    上传的 PRD 可能来自 Word/记事本，是 GBK 而非 UTF-8；
    直接 `decode("utf-8")` 会抛 UnicodeDecodeError 把整个上传拒掉。
    """
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    for enc in ("gb18030", "gbk", "big5", "utf-16"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# DOCX → Markdown
# ---------------------------------------------------------------------------

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W_NS = {"w": _W}


def _docx_heading_styles(zf: zipfile.ZipFile) -> dict:
    """styleId → 标题级别（来自 styles.xml 的 w:name，如 `heading 1`）。"""
    out: dict[str, int] = {}
    try:
        root = ET.fromstring(zf.read("word/styles.xml"))
    except (KeyError, ET.ParseError):
        return out
    for style in root.findall("w:style", _W_NS):
        sid = style.get(f"{{{_W}}}styleId") or ""
        name_el = style.find("w:name", _W_NS)
        name = (name_el.get(f"{{{_W}}}val") if name_el is not None else "") or ""
        m = re.search(r"(?:heading|标题)\s*(\d)", name, re.I)
        if m:
            out[sid] = int(m.group(1))
    return out


def _para_text(para: ET.Element) -> str:
    """段落文本：`w:t` 取字，`w:tab` → 制表，`w:br`/`w:cr` → 换行。"""
    parts: list[str] = []
    for node in para.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "t":
            parts.append(node.text or "")
        elif tag == "tab":
            parts.append("\t")
        elif tag in ("br", "cr"):
            parts.append("\n")
    return "".join(parts)


def _cell_text(cell: ET.Element) -> str:
    """单元格文本：多段合一，竖线转全角以免破坏 Markdown 表格。"""
    lines = [_para_text(p).strip() for p in cell.findall("w:p", _W_NS)]
    joined = " ".join(x for x in lines if x)
    return joined.replace("|", "｜").replace("\n", " ").strip()


def _table_md(table: ET.Element) -> str:
    rows: list[list[str]] = []
    for tr in table.findall("w:tr", _W_NS):
        rows.append([_cell_text(tc) for tc in tr.findall("w:tc", _W_NS)])
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |",
           "| " + " | ".join("---" for _ in range(width)) + " |"]
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def _docx_heading_level(para: ET.Element, styles: dict) -> int:
    """判定标题级别：`w:outlineLvl` 最可靠，其次样式名，最后 styleId 形态。"""
    ppr = para.find("w:pPr", _W_NS)
    if ppr is not None:
        lvl = ppr.find("w:outlineLvl", _W_NS)
        if lvl is not None:
            raw = lvl.get(f"{{{_W}}}val")
            if raw is not None and raw.isdigit():
                return min(6, int(raw) + 1)
        pstyle = ppr.find("w:pStyle", _W_NS)
        if pstyle is not None:
            sid = pstyle.get(f"{{{_W}}}val") or ""
            if sid in styles:
                return min(6, styles[sid])
            m = re.search(r"(?:heading|标题)\s*(\d)", sid, re.I)
            if m:
                return min(6, int(m.group(1)))
    return 0


def extract_docx(data: bytes, *, name: str = "") -> tuple[str, list[Asset], list[str]]:
    """DOCX → (Markdown 文本, 内嵌图片资产, 警告)。

    输出 Markdown 而非纯文本，是为了**保留标题层级** ——
    下游 `split_document()` 依赖 `#` 分节，丢层级会让长 PRD 的分段失效。
    """
    warnings: list[str] = []
    assets: list[Asset] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"不是有效的 DOCX（{exc}）") from exc
    with zf:
        try:
            doc = ET.fromstring(zf.read("word/document.xml"))
        except KeyError as exc:
            raise ValueError("DOCX 缺少 word/document.xml") from exc
        except ET.ParseError as exc:
            raise ValueError(f"DOCX 主体 XML 损坏: {exc}") from exc

        styles = _docx_heading_styles(zf)
        body = doc.find("w:body", _W_NS)
        if body is None:
            return "", [], ["DOCX 无正文节点"]

        blocks: list[str] = []
        for child in body:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                text = _para_text(child).rstrip()
                level = _docx_heading_level(child, styles)
                if not text.strip():
                    continue
                if level:
                    blocks.append(f"{'#' * level} {text.strip()}")
                else:
                    blocks.append(text)
            elif tag == "tbl":
                md = _table_md(child)
                if md:
                    blocks.append(md)

        # 内嵌图片：按名称排序保证确定性
        media = sorted(n for n in zf.namelist()
                       if n.startswith("word/media/") and not n.endswith("/"))
        for member in media:
            blob = zf.read(member)
            mime = _image_mime(blob)
            if not mime:
                continue
            assets.append(Asset(name=Path(member).name, data=blob, mime=mime,
                                origin=f"docx:{member}", role="embedded"))
        if len(media) > len(assets):
            warnings.append(f"DOCX 中有 {len(media) - len(assets)} 个嵌入对象非图片，已忽略")

    md = "\n\n".join(blocks)
    # Word 常用全角空格与不间断空格；归一化以免污染分节与匹配
    md = md.replace("\u00a0", " ").replace("\u3000", " ")
    return md, assets, warnings


# ---------------------------------------------------------------------------
# PDF → 文本（best-effort）
# ---------------------------------------------------------------------------

_UNICODE_ESC = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f",
                "(": "(", ")": ")", "\\": "\\"}


def _pdf_unescape(raw: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(raw):
            break
        nxt = raw[i]
        if nxt in _UNICODE_ESC:
            out.append(_UNICODE_ESC[nxt])
            i += 1
        elif nxt.isdigit():                      # 八进制 \ddd
            j = i
            while j < len(raw) and j < i + 3 and raw[j].isdigit():
                j += 1
            out.append(chr(int(raw[i:j], 8)))
            i = j
        elif nxt == "\n":                        # 续行
            i += 1
        else:
            out.append(nxt)
            i += 1
    return "".join(out)


def _pdf_tounicode(stream: bytes) -> dict[int, str]:
    """解析 ToUnicode CMap 的 bfchar / bfrange，返回 码点→字符。"""
    text = stream.decode("latin-1", errors="replace")
    table: dict[int, str] = {}
    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            try:
                table[int(src, 16)] = _hex_to_str(dst)
            except ValueError:
                continue
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for lo, hi, dst in re.findall(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            try:
                start, end, base = int(lo, 16), int(hi, 16), int(dst, 16)
            except ValueError:
                continue
            for k in range(min(end - start + 1, 65536)):
                table[start + k] = chr(base + k & 0x10FFFF) if base + k <= 0x10FFFF else ""
    return table


def _hex_to_str(hexstr: str) -> str:
    raw = bytes.fromhex(hexstr if len(hexstr) % 2 == 0 else hexstr + "0")
    if len(raw) >= 2 and raw[0] == 0xFE and raw[1] == 0xFF:
        raw = raw[2:]
    try:
        return raw.decode("utf-16-be")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def extract_pdf(data: bytes) -> tuple[str, list[str]]:
    """PDF → 文本（best-effort）。

    不追求完整 PDF 支持：目标是覆盖**文本型 PDF**（由 Word/Markdown 导出）。
    实现要点：
      1. 扫描所有流对象并尝试 Flate 解压；
      2. 从含 `beginbfchar/beginbfrange` 的流构建 ToUnicode 映射（CJK 必需）；
      3. 在内容流中取 `Tj` / `TJ` / `'` / `"` 的字样，按文本算子切分。

    **扫描件 / 纯图 PDF 无法抽取**（需先栅格化再走视觉，本项目不含渲染器）：
    此时返回空文本并给出明确告警，交由调用方决定是否让用户提供图片或源文档。
    """
    warnings: list[str] = []
    if not data.startswith(b"%PDF-"):
        raise ValueError("不是 PDF 文件")

    cmap: dict[int, str] = {}
    for m in re.finditer(rb"stream\r?\n", data):
        start = m.end()
        end = data.find(b"endstream", start)
        if end == -1:
            continue
        chunk = data[start:end].rstrip(b"\r\n")
        blob = None
        try:
            blob = zlib.decompress(chunk)
        except zlib.error:
            if b"beginbfchar" in chunk or b"BT" in chunk:
                blob = chunk
        if not blob:
            continue
        if b"beginbfchar" in blob or b"beginbfrange" in blob:
            cmap.update(_pdf_tounicode(blob))

    # 抽取内容流文本
    pieces: list[str] = []
    for m in re.finditer(rb"stream\r?\n", data):
        start = m.end()
        end = data.find(b"endstream", start)
        if end == -1:
            continue
        chunk = data[start:end].rstrip(b"\r\n")
        blob = None
        try:
            blob = zlib.decompress(chunk)
        except zlib.error:
            blob = chunk
        if not blob or (b"BT" not in blob and b"Tj" not in blob and b"TJ" not in blob):
            continue
        pieces.append(_pdf_stream_text(blob, cmap))

    text = "\n".join(p for p in pieces if p.strip())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        warnings.append("PDF 未抽取到文本（可能为扫描件/纯图 PDF）。"
                        "请提供可复制文本的 PDF、源文档，或该页的图片以走视觉识别")
    elif _looks_garbled(text):
        warnings.append("PDF 文本抽取质量低（可能缺少 ToUnicode 映射或为扫描件）；"
                        "建议提供源文档或图片以确保需求抽取准确")
    return text, warnings


def _pdf_stream_text(stream: bytes, cmap: dict[int, str]) -> str:
    """从一个内容流里取文本：处理 Tj/TJ/'/\"，并按定位算子换行。"""
    src = stream.decode("latin-1", errors="replace")
    out: list[str] = []

    def render_literal(raw: str) -> str:
        s = _pdf_unescape(raw)
        if cmap:
            # 单字节或双字节码：优先查表（CJK 字体的 Identity-H 编码）
            mapped = "".join(cmap.get(ord(c), c) for c in s)
            if mapped != s:
                return mapped
        return s

    def render_hex(hexstr: str) -> str:
        hexstr = re.sub(r"[^0-9A-Fa-f]", "", hexstr)
        if not hexstr:
            return ""
        raw = bytes.fromhex(hexstr if len(hexstr) % 2 == 0 else hexstr + "0")
        if cmap:
            # Identity-H：两字节一码
            if len(raw) % 2 == 0 and all(
                    int.from_bytes(raw[i:i + 2], "big") in cmap
                    for i in range(0, len(raw), 2)):
                return "".join(cmap[int.from_bytes(raw[i:i + 2], "big")]
                               for i in range(0, len(raw), 2))
            if all(b in cmap for b in raw):
                return "".join(cmap[b] for b in raw)
        try:
            return raw.decode("utf-16-be") if len(raw) % 2 == 0 else raw.decode("latin-1")
        except UnicodeDecodeError:
            return raw.decode("latin-1", errors="replace")

    token_re = re.compile(
        r"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>|\[|\]|"
        r"\bT[JjdD*]\b|\bTD\b|\bET\b|\bBT\b|'|\"", re.S)
    pending: list[str] = []
    for m in token_re.finditer(src):
        tok = m.group(0)
        if tok.startswith("("):
            pending.append(render_literal(tok[1:-1]))
        elif tok.startswith("<"):
            pending.append(render_hex(tok[1:-1]))
        elif tok in ("Tj", "TJ", "'", '"'):
            if tok in ("'", '"'):
                out.append("\n")
            out.append("".join(pending))
            pending = []
        elif tok in ("Td", "TD", "T*", "ET"):
            if pending:
                out.append("".join(pending))
                pending = []
            out.append("\n")
    if pending:
        out.append("".join(pending))
    text = "".join(out)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _looks_garbled(text: str) -> bool:
    """抽取质量启发式：可读字符（CJK/字母/数字/常见符号）占比过低即判为乱码。

    用「可打印 + CJK」而非硬编码标点表：后者容易出现引号转义疏漏，
    且不必随语料新增标点而维护。
    """
    if len(text) < 40:
        return False
    good = sum(1 for ch in text if ch.isprintable())
    return good / len(text) < 0.85


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return ""


# ---------------------------------------------------------------------------
# 摄取
# ---------------------------------------------------------------------------

def _norm_input(item) -> tuple[str, bytes, str]:
    """把输入项归一化为 (name, data, origin)。

    支持：
      - `(name, bytes)` 二元组 —— 最简上传形态；
      - `(name, bytes, origin)` 三元组 —— 需要保留来源标记时（如 "upload:field"）；
      - 文件/目录路径。
    """
    if isinstance(item, tuple):
        if len(item) == 2 and isinstance(item[1], (bytes, bytearray)):
            return str(item[0]), bytes(item[1]), "upload"
        if len(item) == 3 and isinstance(item[1], (bytes, bytearray)):
            return str(item[0]), bytes(item[1]), str(item[2])
        raise TypeError("元组输入应为 (name, bytes) 或 (name, bytes, origin)")
    p = Path(item)
    if p.is_dir():
        raise IsADirectoryError(f"目录请通过 ingest_directory 处理: {p}")
    if not p.is_file():
        raise FileNotFoundError(f"输入不存在: {p}")
    size = p.stat().st_size
    if size > MAX_SOURCE_BYTES:
        raise ValueError(f"文件超过上限 {MAX_SOURCE_BYTES // 1024 // 1024}MB: {p.name}")
    return p.name, p.read_bytes(), str(p)


def _iter_dir(path: Path):
    """递归遍历目录，忽略隐藏文件与 macOS 元数据（确定性排序）。"""
    for p in sorted(path.rglob("*")):
        if not p.is_file():
            continue
        if any(part.startswith(".") for part in p.relative_to(path).parts):
            continue
        if "__MACOSX" in p.parts or p.name.startswith("._"):
            continue
        yield p.name, p.read_bytes(), str(p)


def ingest(inputs, *, warnings: list | None = None) -> Bundle:
    """摄取多个输入（文件路径 / 目录 / (name, bytes)），返回归一化 Bundle。

    主文件判定（决定哪个是 PRD 正文）：
    有 `.md/.markdown` 则取**字符数最多**者为主文件；否则取 `.docx`，再否则取 `.pdf`；
    都没有则取第一个文本类来源。其余文本类来源作为**附件**按节内联，
    图片全部作为资产交视觉识别。
    """
    bundle = Bundle()
    warns = warnings if warnings is not None else bundle.warnings
    pending: list[tuple[str, bytes, str, int]] = []      # name, data, origin, depth
    seen_hash: set[str] = set()

    raw_items = list(inputs)
    for item in raw_items:
        try:
            if isinstance(item, (str, Path)) and Path(item).is_dir():
                for name, data, origin in _iter_dir(Path(item)):
                    pending.append((name, data, origin, 0))
                continue
            name, data, origin = _norm_input(item)
        except (FileNotFoundError, IsADirectoryError, OSError, ValueError, TypeError) as exc:
            warns.append(f"输入跳过：{exc}")
            continue
        pending.append((name, data, origin, 0))

    if not pending:
        # 把已收集的告警带进异常 —— 否则「为什么全都失败了」对调用方不可见
        detail = ("；".join(warns[:5]) + ("…" if len(warns) > 5 else "")) if warns else "未提供任何输入"
        raise ValueError(f"没有可用的输入（{detail}）")

    # 展开压缩包（含嵌套），并对成员去重
    expanded: list[tuple[str, bytes, str, int]] = []
    while pending:
        name, data, origin, depth = pending.pop(0)
        if len(data) > MAX_SOURCE_BYTES:
            bundle.sources.append(Source(name=name, kind=KIND_OTHER, size=len(data),
                                         sha256=hashlib.sha256(data).hexdigest(),
                                         origin=origin, role="skipped",
                                         note=f"超过上限 {MAX_SOURCE_BYTES // 1024 // 1024}MB"))
            warns.append(f"{name} 超过单文件上限，已跳过")
            continue
        kind = sniff_kind(name, data)
        digest = hashlib.sha256(data).hexdigest()
        if kind != KIND_ZIP and digest in seen_hash:
            bundle.sources.append(Source(name=name, kind=kind, size=len(data), sha256=digest,
                                         origin=origin, role="skipped", note="重复内容"))
            continue
        seen_hash.add(digest)
        if kind == KIND_ZIP:
            if depth >= MAX_ZIP_DEPTH:
                warns.append(f"{name} 嵌套层级超过 {MAX_ZIP_DEPTH}，已跳过")
                continue
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    members = [n for n in sorted(zf.namelist())
                               if not n.endswith("/") and not n.startswith("__MACOSX")]
                    if len(members) > MAX_MEMBERS:
                        warns.append(f"{name} 成员数 {len(members)} 超过上限 {MAX_MEMBERS}，"
                                     "仅取前若干项")
                        members = members[:MAX_MEMBERS]
                    for member in members:
                        base = Path(member).name
                        if base.startswith("."):
                            continue
                        pending.append((base, zf.read(member), f"zip:{name}/{member}", depth + 1))
            except zipfile.BadZipFile as exc:
                warns.append(f"{name} 解压失败：{exc}")
            continue
        expanded.append((name, data, origin, depth))

    return _assemble(expanded, warns, bundle=bundle)


def _assemble(items: list[tuple[str, bytes, str, int]], warns: list,
              bundle: Bundle | None = None) -> Bundle:
    """把展开后的成员归类为「正文 / 附件 / 资产」。

    传入 `bundle` 以复用**同一个**告警列表：若在此处新建 Bundle，
    前面（展开、校验阶段）收集的告警会全部丢失 —— 实测导致「全部失败却没有任何原因」。
    """
    bundle = bundle if bundle is not None else Bundle()
    bundle.warnings = warns
    texts: list[tuple[str, str, str, int]] = []          # name, text, origin, order
    assets: list[Asset] = []

    for idx, (name, data, origin, _depth) in enumerate(items):
        kind = sniff_kind(name, data)
        digest = hashlib.sha256(data).hexdigest()
        src = Source(name=name, kind=kind, size=len(data), sha256=digest, origin=origin)
        try:
            if kind in (KIND_MARKDOWN, KIND_TEXT, KIND_HTML):
                text = decode_text(data)
                src.textChars = len(text)
                texts.append((name, text, origin, idx))
            elif kind == KIND_DOCX:
                text, embedded, w = extract_docx(data, name=name)
                src.textChars = len(text)
                warns.extend(w)
                if not text.strip():
                    src.role = "skipped"
                    src.note = "DOCX 正文为空"
                    warns.append(f"{name} 正文为空，已跳过")
                else:
                    texts.append((name, text, origin, idx))
                    assets.extend(embedded)
                    if embedded:
                        src.note = f"含 {len(embedded)} 张内嵌图片"
            elif kind == KIND_PDF:
                text, w = extract_pdf(data)
                src.textChars = len(text)
                warns.extend(f"{name}: {x}" for x in w)
                if text.strip():
                    texts.append((name, text, origin, idx))
                else:
                    src.role = "skipped"
                    src.note = "未能抽取文本"
            elif kind == KIND_IMAGE:
                mime = _image_mime(data)
                if not mime:
                    src.role = "skipped"
                    src.note = "图片格式不可识别"
                else:
                    assets.append(Asset(name=name, data=data, mime=mime, origin=origin,
                                        role="prd-image"))
                    src.role = "asset"
                    src.note = "图片（交视觉识别）"
            else:
                src.role = "skipped"
                src.note = "不支持的格式"
                warns.append(f"{name} 格式不受支持（{kind}），已跳过")
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            src.role = "skipped"
            src.note = f"{type(exc).__name__}: {exc}"
            warns.append(f"{name} 解析失败：{exc}")
        bundle.sources.append(src)

    if not texts:
        if assets:
            # 纯图片 PRD：正文留空，全部靠视觉证据 —— 这是合法的多模态输入
            bundle.primary = assets[0].name
            warns.append("未找到文本正文；按「纯图片 PRD」处理，将完全依赖视觉识别结果")
        else:
            raise ValueError("输入中没有可解析的文本或图片内容")
        bundle.assets = assets
        bundle.text = ""
        return bundle

    # 先按输入顺序稳定排序，再选主文件 ——
    # 反过来的话 primary_idx 会指向排序前的下标，内容与名字错位（开发中踩到过）。
    texts.sort(key=lambda x: x[3])
    primary_idx = _pick_primary(texts)
    primary = texts[primary_idx]
    bundle.primary = primary[0]

    parts: list[str] = [primary[1]]
    for i, (name, text, origin, _order) in enumerate(texts):
        if i == primary_idx:
            continue
        parts.append(f"\n\n---\n\n## 附件：{name}\n\n{text}")
        for s in bundle.sources:
            if s.name == name and s.origin == origin:
                s.role = "attachment"
                break
    body = "".join(parts)
    if len(body) > MAX_TEXT_CHARS:
        warns.append(f"正文超过 {MAX_TEXT_CHARS} 字符，已截断（请拆分 PRD 后分段生成）")
        body = body[:MAX_TEXT_CHARS]
    bundle.text = body
    bundle.assets = assets
    # 主文件角色修正
    for s in bundle.sources:
        if s.name == primary[0]:
            s.role = "primary"
            break
    return bundle


def _pick_primary(texts: list[tuple[str, str, str, int]]) -> int:
    """选主文件：`.md` 中最长者 → `.docx` → `.pdf` → 第一个。"""
    def score(item):
        name, text, _origin, order = item
        suffix = Path(name).suffix.lower()
        rank = 0 if suffix in (".md", ".markdown", ".mdown") else \
               1 if suffix in (".docx", ".docm") else 2 if suffix == ".pdf" else 3
        return (rank, -len(text), order)
    return min(range(len(texts)), key=lambda i: score(texts[i]))


def to_json(bundle: Bundle) -> dict:
    """序列化（不含资产字节，只留元信息）—— 用于 manifest 与留痕。"""
    return {
        "schemaVersion": "1.0",
        "primary": bundle.primary,
        "textChars": len(bundle.text),
        "sources": [s.to_json() for s in bundle.sources],
        "assets": [{"name": a.name, "mime": a.mime, "size": a.size,
                    "sha256": a.sha256, "origin": a.origin, "role": a.role}
                   for a in bundle.assets],
        "warnings": bundle.warnings,
        "fingerprint": bundle.fingerprint(),
        "summary": bundle.summary(),
    }
