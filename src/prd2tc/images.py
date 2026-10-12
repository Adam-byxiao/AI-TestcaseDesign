"""PRD 图片发现、关联与视觉分析。

## 为什么需要它

PRD 里的**关键 UI 事实常常只存在于图片中**：线框图、字段布局、弹窗选项、状态枚举、
流程图。而 Markdown 导出通常**不会内联图片**，而是把图片作为同目录/子目录下的独立文件
一起下载（例如 `prd.md` + `prd.assets/`）。

实测：真实 PRD 含 **8 处图片引用**（全部为需鉴权的远程链接），
本地无任何图片文件、loader 也不解析引用 —— 这些图**从未参与生成**。
后果是模型只能凭文字推断页面结构与交互，进而编造标识符与弹窗选项
（与 `prototype.py` 解决的是同一类问题：**设计真值不可达**）。

## 能力

1. **发现**：解析 Markdown `![](...)` 与 HTML `<img>`，记录 alt、顺序、所在小节、上下文；
2. **关联**：在 PRD 同级与约定子目录中按「URL 文件名 → alt 关键词 → 出现顺序」匹配本地图片，
   并给出匹配置信度（不猜测来源，低置信度可复核）；
3. **分析**：调用**视觉模型**把图片转成结构化 UI 事实（元素/字段/状态/文案/布局/流程）；
4. **缓存**：按 `sha256(图片) + 模型 + 提示词版本` 缓存，保证可复现且不重复计费；
5. **降级**：缺图、远程取不到、模型不支持视觉 → **只告警不中断**。

## 设计取舍

- **只读，不改 PRD**：图片是证据来源，不注入正文文本，避免污染原始输入指纹；
- **分级信任**：图片结论标记为 `inferred`（来自图片推断），与 PRD 明文事实区分，
  供门禁与评审识别（呼应「诚实标注」原则）；
- **零依赖**：仅用 stdlib（`urllib`/`base64`/`hashlib`），图片类型按魔数嗅探。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

# 提示词版本：参与缓存键。改动抽取要求时必须同步升版，否则会命中旧结论。
VISION_PROMPT_VERSION = "1.0"

# 单图上限：base64 后约放大 1.33 倍，8MB 已接近多数 Provider 的请求上限
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# 支持的图片类型（魔数 → MIME）
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)
_EXT_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}

# 约定：图片与 PRD 一同下载时常见的目录名
ASSET_DIR_NAMES = (
    "{stem}.assets", "{stem}_files", "{stem}", "assets", "images", "img",
    "{stem}.files", "attachments", "media",
)

_MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\s*\)")
_HTML_IMG_RE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"'][^>]*>", re.I)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class ImageRef:
    """文档中的一处图片引用。"""
    index: int                 # 在文档中出现的顺序（1 起）
    alt: str                   # alt 文本（常是图注）
    target: str                # 原始地址：URL 或相对路径
    section: str = ""          # 所在小节标题（最近的上文标题）
    context: str = ""          # 引用处上下文的纯文本摘录
    resolved: str = ""         # 解析到的本地文件路径
    match: str = ""            # 关联方式：filename | alt | order | explicit | ""
    remote: bool = False       # 是否为远程地址


@dataclass
class ImageInsight:
    """单张图片的视觉分析结论（全部为「图片推断」，非 PRD 明文）。"""
    index: int
    sha256: str = ""
    source: str = ""
    alt: str = ""
    section: str = ""
    kind: str = "other"        # ui-mockup | flow-diagram | table | architecture | other
    summary: str = ""
    annotated: list = field(default_factory=list)     # [{target, meaning}] 设计稿标注（改动点）
    ui_elements: list = field(default_factory=list)   # [{label,kind,state}]
    fields: list = field(default_factory=list)        # [{label,type,placeholder,maxLength}]
    states: list = field(default_factory=list)        # 状态/选项枚举
    texts: list = field(default_factory=list)         # 图中原文文案
    layout: str = ""
    flows: list = field(default_factory=list)
    notes: str = ""
    cached: bool = False


@dataclass
class ImageSpec:
    """整份文档的图片证据集合。"""
    refs: list = field(default_factory=list)          # [ImageRef]
    insights: list = field(default_factory=list)      # [ImageInsight]
    warnings: list = field(default_factory=list)

    def analyzed(self) -> list:
        return [i for i in self.insights if i.summary or i.ui_elements or i.fields
                or i.texts or i.states or i.annotated]

    def summary(self) -> dict:
        return {
            "refCount": len(self.refs),
            "resolvedCount": sum(1 for r in self.refs if r.resolved),
            "remoteCount": sum(1 for r in self.refs if r.remote),
            "analyzedCount": len(self.analyzed()),
            "kinds": sorted({i.kind for i in self.analyzed()}),
            "warnings": len(self.warnings),
        }


# ---------------------------------------------------------------------------
# 发现
# ---------------------------------------------------------------------------

def discover_refs(markdown: str) -> list[ImageRef]:
    """解析文档中的图片引用，附带小节与上下文（确定性顺序）。"""
    hits: list[tuple[int, str, str]] = []      # (pos, alt, target)
    for m in _MD_IMG_RE.finditer(markdown):
        hits.append((m.start(), m.group(1).strip(), m.group(2).strip()))
    for m in _HTML_IMG_RE.finditer(markdown):
        hits.append((m.start(), "", m.group(1).strip()))
    hits.sort(key=lambda x: x[0])

    headings = [(m.start(), m.group(2).strip()) for m in _HEADING_RE.finditer(markdown)]

    refs: list[ImageRef] = []
    for i, (pos, alt, target) in enumerate(hits, 1):
        section = ""
        for hpos, htext in headings:
            if hpos < pos:
                section = htext
            else:
                break
        # 上下文：引用前后各截一段，压平空白（给分析提示词做定位线索）
        raw = markdown[max(0, pos - 300):pos + 300]
        context = re.sub(r"\s+", " ", re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", raw)).strip()
        refs.append(ImageRef(
            index=i, alt=alt, target=target, section=section, context=context[:400],
            remote=bool(re.match(r"^https?://", target, re.I)),
        ))
    return refs


# ---------------------------------------------------------------------------
# 关联本地文件
# ---------------------------------------------------------------------------

def candidate_dirs(prd_path: Path) -> list[Path]:
    """图片可能与 PRD 一起落盘的目录（按优先级）。"""
    stem = prd_path.stem
    base = prd_path.parent
    out = [base]
    for name in ASSET_DIR_NAMES:
        out.append(base / name.format(stem=stem))
    # 去重且仅保留存在的目录
    seen, dirs = set(), []
    for d in out:
        key = str(d.resolve()) if d.exists() else str(d)
        if key in seen:
            continue
        seen.add(key)
        if d.is_dir():
            dirs.append(d)
    return dirs


def _image_files(directory: Path, *, limit: int = 2000) -> list[Path]:
    files: list[Path] = []
    for p in sorted(directory.iterdir()):
        if p.is_file() and p.suffix.lower() in _EXT_MIME:
            files.append(p)
            if len(files) >= limit:
                break
    return files


def _norm(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(text or "")).lower()


def longest_common_substring(a: str, b: str) -> int:
    """最长公共子串长度（滚动数组，O(min(n,m)) 空间）。

    为什么不用「包含」判断：PRD 图注通常很长（」业务场景：新增、删除与带行号的内容编辑」），
    而文件名很短（`业务场景编辑.png`）—— 互不包含，但共享「业务场景」「编辑」等长子串。
    实测「包含」判定只能关联 3/8，改用最长公共子串可全部命中。
    """
    if not a or not b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


# 图注与文件名的最小公共子串长度：CJK 下 3 字已具备区分度
# （「配置」2 字太泛，「知识库」3 字可区分「知识库配置」与「知识库命中测试」）
MIN_ALT_OVERLAP = 3


def _alt_score(alt: str, filename_stem: str) -> int:
    return longest_common_substring(_norm(alt), _norm(filename_stem))


def find_local(ref: ImageRef, prd_path: Path, *,
               pool: list[Path] | None = None) -> tuple[str, str]:
    """为**单条**引用寻找本地文件，返回 (路径, 关联方式)。

    这是 `resolve_refs` 的便捷包装：单条场景下「全局唯一分配」自然退化为
    「择优匹配」，因此不重复实现评分逻辑 —— 两处实现会各自漂移
    （开发中曾因此出现「改了一处、测试仍全绿」的假安全感）。
    """
    refs = resolve_refs([ref], prd_path, extra_dirs=pool)
    return refs[0].resolved, refs[0].match


def _collect(prd_path: Path, extra_dirs: list[Path] | None = None) -> list[Path]:
    dirs = list(extra_dirs or []) + candidate_dirs(prd_path)
    seen: set[str] = set()
    files: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for f in _image_files(d):
            key = str(f.resolve())
            if key not in seen:
                seen.add(key)
                files.append(f)
    return files


def resolve_refs(refs: list[ImageRef], prd_path: Path, *,
                 extra_dirs: list[Path] | None = None) -> list[ImageRef]:
    """为全部引用补齐本地文件（**唯一**的关联实现）。

    分三阶段做**全局唯一分配**：
      0. `explicit`：target 就是存在的相对路径；
      1. `filename`：与 target 的 basename 同名（或规范化后同形）；
      2. `alt`：按**最长公共子串**评分贪心分配，每个文件只用一次。

    最后才做顺序兜底（`order`），且**仅当剩余引用数与剩余文件数相等**；
    该方式置信度低（实测 PRD 图序与目录排序并不一致），故显式标记以便复核。
    """
    for ref in refs:
        ref.resolved, ref.match = "", ""

    # 阶段 0：显式相对路径。必须在候选池判空之前做 ——
    # 否则「图片就在 PRD 旁边但目录名不常规」的场景会被 pool 为空提前返回而漏掉。
    for ref in refs:
        if ref.remote:
            continue
        direct = (prd_path.parent / ref.target).resolve()
        if direct.is_file():
            ref.resolved, ref.match = str(direct), "explicit"

    files = _collect(prd_path, extra_dirs)
    if not files:
        return refs

    used: set[str] = {r.resolved for r in refs if r.resolved}
    # 阶段 1：同名匹配
    for ref in refs:
        if ref.resolved:
            continue
        basename = Path(ref.target).name
        if not basename:
            continue
        for f in files:
            if str(f.resolve()) in used:
                continue
            if f.name == basename or _norm(f.stem) == _norm(Path(basename).stem):
                ref.resolved, ref.match = str(f), "filename"
                used.add(str(f.resolve()))
                break

    # 阶段 2：图注评分，全局贪心（每个文件至多分配一次）
    pairs: list[tuple[int, int, Path]] = []
    for ref in refs:
        if ref.resolved:
            continue
        for f in files:
            if str(f.resolve()) in used:
                continue
            score = _alt_score(ref.alt, f.stem)
            if score >= MIN_ALT_OVERLAP:
                pairs.append((score, ref.index, f))
    pairs.sort(key=lambda x: (-x[0], x[1], x[2].name))
    by_index = {r.index: r for r in refs}
    for score, index, f in pairs:
        key = str(f.resolve())
        if key in used:
            continue
        ref = by_index.get(index)
        if ref is None or ref.resolved:
            continue
        ref.resolved, ref.match = str(f), "alt"
        used.add(key)

    # 阶段 3：顺序兜底（低置信）
    left_refs = [r for r in refs if not r.resolved]
    left_files = [f for f in files if str(f.resolve()) not in used]
    if left_refs and len(left_refs) == len(left_files):
        for ref, f in zip(left_refs, left_files):
            ref.resolved, ref.match = str(f), "order"
    return refs


# ---------------------------------------------------------------------------
# 读取与编码
# ---------------------------------------------------------------------------

def sniff_mime(data: bytes, fallback: str = "") -> str:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return fallback


def read_image(path: Path) -> tuple[bytes, str]:
    """读取图片并按**魔数**判定类型；非图片/空/超限抛 ValueError。

    注意：**不信任文件扩展名**。改名为 .png 的文本文件必须被拒绝 ——
    否则损坏文件会被当作有效图片送进视觉模型，浪费调用且污染证据。
    扩展名仅用于错误提示。
    """
    data = path.read_bytes()
    if not data:
        raise ValueError("文件为空")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"图片超过上限 {MAX_IMAGE_BYTES // 1024 // 1024}MB（{len(data)} 字节）")
    mime = sniff_mime(data)
    if not mime:
        raise ValueError(f"不是可识别的图片格式（扩展名 {path.suffix or '无'}，"
                         "支持 png/jpeg/gif/webp/bmp）")
    return data, mime


def fetch_remote(url: str, *, timeout: int = 20) -> tuple[bytes, str]:
    """下载远程图片。需显式开启（会发起网络请求）。"""
    req = urllib.request.Request(url, headers={"User-Agent": "prd2tc/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(MAX_IMAGE_BYTES + 1)
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"远程图片超过上限（>{MAX_IMAGE_BYTES} 字节）")
    mime = sniff_mime(data, ctype if ctype.startswith("image/") else "")
    if not mime:
        raise ValueError(f"远程内容不是图片（Content-Type={ctype or '未知'}）")
    return data, mime


# ---------------------------------------------------------------------------
# 视觉分析
# ---------------------------------------------------------------------------

VISION_INSTRUCTION = f"""你是资深测试架构师。请**只依据图中可见内容**提取结构化事实，用于设计测试用例。

要求：
1. 只描述图中**真实可见**的元素与文案，逐字抄录可见文字（不要改写、不要补全）；
2. **特别注意标注**：设计稿常有红框/箭头/高亮，用来圈出**本次改动点**。
   凡被标注的元素，在 `annotated` 中标明，并在 `summary` 里说明标注含义；
3. 注意**状态与计数器**：角标、计数（如「16 行 · 235 字」）、状态标签
   （如「与已发布内容一致」「全部草稿已保存」）、通知条文案 —— 这些是可断言的强证据；
4. 无法确认的内容放进 `notes`，**禁止猜测**；看不清就写「不确定」；
5. 输出**单个 JSON 对象**（不要 Markdown 围栏、不要解释）；
6. 字段：
   - `kind`：ui-mockup | flow-diagram | table | architecture | other
   - `summary`：一句话说明这张图展示什么（≤ 60 字）
   - `annotated`：被标注/高亮的元素与含义数组，元素为 {{"target": 被标注元素原文, "meaning": 标注想说明什么}}
   - `uiElements`：可见交互元素数组，元素为 {{"label": 图中原文, "kind": button|tab|link|icon|switch|menu|notice|status, "state": 可见状态如 active/disabled/收起/空}}
   - `fields`：表单字段数组，元素为 {{"label": 标签原文, "type": input|textarea|select|upload|checkbox, "placeholder": 占位原文, "maxLength": 图中标注的长度限制}}
   - `states`：状态 / 选项 / 枚举值数组（如「解析中」「解析成功」「未命中」）
   - `texts`：图中其它可见文案、通知条、校验提示、计数器（逐字抄录）
   - `layout`：布局描述（如「左右分栏：左列表右详情」「右侧收起窄栏」）
   - `flows`：若为流程图，按顺序列出步骤；否则为空数组
   - `notes`：不确定或不可辨识之处

提示词版本：{VISION_PROMPT_VERSION}
"""


def cache_key(sha256: str, model: str) -> str:
    raw = f"{sha256}\x00{model}\x00{VISION_PROMPT_VERSION}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _parse_insight_json(text: str) -> dict:
    """从模型输出中提取 JSON 对象（容忍围栏与前后噪声）。"""
    t = str(text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", t)
    if m:
        t = m.group(1)
    else:
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            t = t[start:end + 1]
    data = json.loads(t)
    if not isinstance(data, dict):
        raise ValueError("视觉模型未返回 JSON 对象")
    return data


def _coerce_list(value, keys: tuple[str, ...]) -> list:
    """把模型输出规整为 list[dict]，只保留白名单键（防御脏数据）。"""
    out = []
    for item in value or []:
        if isinstance(item, dict):
            out.append({k: str(item.get(k) or "") for k in keys})
        elif isinstance(item, str) and item.strip():
            out.append({keys[0]: item.strip()} if len(keys) == 1 else item.strip())
    return out


def _coerce_str_list(value) -> list:
    """规整为字符串数组：丢弃 None/空串，保留数字（转字符串）。"""
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for x in value:
        if x is None or isinstance(x, (dict, list, bool)):
            continue
        text = str(x).strip()
        if text:
            out.append(text)
    return out


def analyze_images(
    cfg, items: list[tuple[ImageRef, bytes, str]], *, vision_llm=None,
    cache_dir: Path | None = None, reporter=None,
) -> list[ImageInsight]:
    """对 (引用, 字节, MIME) 列表做视觉分析，返回结论（带缓存）。

    `vision_llm(cfg, prompt, images, system=...)` 需支持多图输入；
    默认为 `prd2tc.llm.vision_completion`。
    """
    from prd2tc.llm import vision_completion
    call = vision_llm or vision_completion
    insights: list[ImageInsight] = []
    for ref, data, mime in items:
        digest = hashlib.sha256(data).hexdigest()
        key = cache_key(digest, cfg.model)
        cached = None
        if cache_dir is not None:
            cp = cache_dir / f"{key}.json"
            if cp.is_file():
                try:
                    cached = json.loads(cp.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    cached = None
        if cached is not None:
            # index/source/alt/section 取**当前**引用：缓存按图片内容寻址，
            # 同一张图在不同文档里可能位于不同序号（缓存里存旧序号会错位）。
            insights.append(ImageInsight(
                index=ref.index, sha256=digest, source=ref.resolved or ref.target,
                alt=ref.alt, section=ref.section, cached=True, **cached))
            continue
        prompt = (
            f"图注（alt）：{ref.alt or '(无)'}\n"
            f"所在小节：{ref.section or '(未知)'}\n"
            f"引用处上下文：{ref.context or '(无)'}\n\n"
            "请按指令提取结构化事实。"
        )
        try:
            result = call(cfg, prompt, [(data, mime)], system=VISION_INSTRUCTION)
            payload = _parse_insight_json(getattr(result, "content", result))
        except Exception as exc:
            if reporter:
                try:
                    reporter(f"⚠ 图片 {ref.index} 分析失败，已跳过：{type(exc).__name__}: {exc}")
                except Exception:
                    pass
            continue
        insight = ImageInsight(
            index=ref.index, sha256=digest, source=ref.resolved or ref.target,
            alt=ref.alt, section=ref.section,
            kind=str(payload.get("kind") or "other")[:24],
            summary=str(payload.get("summary") or "")[:200],
            annotated=_coerce_list(payload.get("annotated"), ("target", "meaning")),
            ui_elements=_coerce_list(payload.get("uiElements"), ("label", "kind", "state")),
            fields=_coerce_list(payload.get("fields"), ("label", "type", "placeholder", "maxLength")),
            states=_coerce_str_list(payload.get("states")),
            texts=_coerce_str_list(payload.get("texts")),
            layout=str(payload.get("layout") or "")[:200],
            flows=_coerce_str_list(payload.get("flows")),
            notes=str(payload.get("notes") or "")[:300],
        )
        insights.append(insight)
        if cache_dir is not None:
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                body = {k: v for k, v in asdict(insight).items()
                        if k not in {"cached", "sha256", "source", "alt", "section", "index"}}
                (cache_dir / f"{key}.json").write_text(
                    json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except OSError:
                pass
    return insights


# ---------------------------------------------------------------------------
# 编排：从 PRD 到 ImageSpec
# ---------------------------------------------------------------------------

def prepare(
    prd_text: str, prd_path: Path, *, allow_remote: bool = False,
    reporter=None, cache_dir: Path | None = None, cfg=None, vision_llm=None,
    extra_dirs: list[Path] | None = None, analyze: bool = True,
) -> ImageSpec:
    """发现 → 关联 → 分析，返回完整图片证据集。任何单点失败都只告警。

    `analyze=False`（如 stub 模式）时只做发现与关联，不调用视觉模型 ——
    这样仍能产出可复核的「图片清单」产物，且不会发起无谓的网络请求。
    """
    spec = ImageSpec()
    refs = discover_refs(prd_text)
    if not refs:
        return spec
    resolve_refs(refs, prd_path, extra_dirs=extra_dirs)
    spec.refs = refs

    items: list[tuple[ImageRef, bytes, str]] = []
    for ref in refs:
        try:
            if ref.resolved:
                data, mime = read_image(Path(ref.resolved))
            elif ref.remote and allow_remote:
                data, mime = fetch_remote(ref.target)
            else:
                hint = ("远程图片默认不下载（用 --fetch-images 显式开启）"
                        if ref.remote else "未找到本地文件")
                spec.warnings.append(f"图片 {ref.index} 跳过：{hint}（{ref.target}）")
                continue
        except (ValueError, OSError, urllib.error.URLError) as exc:
            spec.warnings.append(f"图片 {ref.index} 读取失败：{type(exc).__name__}: {exc}")
            continue
        items.append((ref, data, mime))

    if not items:
        return spec
    if not analyze or cfg is None:
        spec.warnings.append(
            f"已定位 {len(items)} 张图片，但未做视觉识别"
            + ("（stub 模式）" if not analyze else "（未提供视觉模型配置）")
            + "；请配置支持图片输入的模型后重跑以获取图片证据")
        return spec

    spec.insights = analyze_images(cfg, items, vision_llm=vision_llm,
                                   cache_dir=cache_dir, reporter=reporter)
    if not spec.insights:
        spec.warnings.append("视觉分析未产出任何结论（模型可能不支持图片输入）")
    return spec


# ---------------------------------------------------------------------------
# 渲染 / 序列化
# ---------------------------------------------------------------------------

def render_image_spec(spec: ImageSpec, *, max_texts: int = 60) -> str:
    """渲染为可注入 prompt 的证据块（标注为「图片推断」以区别于 PRD 明文）。"""
    analyzed = spec.analyzed()
    if not analyzed:
        return ""
    out = ["## PRD 图片证据（**来自图片的视觉识别，属推断结论**；与 PRD 明文冲突时以明文为准，"
           "并在用例中标注 `[推断]`）"]
    if spec.summary()["refCount"] > len(analyzed):
        out.append(f"- 共 {spec.summary()['refCount']} 处图片引用，成功识别 {len(analyzed)} 张；"
                   "未识别部分不得凭空补全。")
    for ins in analyzed:
        head = f"- 图{ins.index}"
        if ins.section:
            head += f"（小节：{ins.section}）"
        if ins.alt:
            head += f" 图注：{ins.alt}"
        out.append(head)
        if ins.summary:
            out.append(f"  - 内容：{ins.summary}")
        if ins.kind and ins.kind != "other":
            out.append(f"  - 类型：{ins.kind}")
        if ins.layout:
            out.append(f"  - 布局：{ins.layout}")
        if ins.annotated:
            rows = []
            for a in ins.annotated:
                tgt = a.get("target") or "?"
                mean = a.get("meaning") or ""
                rows.append(f"{tgt}（{mean}）" if mean else tgt)
            out.append("  - **图中标注（本次改动点）**：" + "；".join(rows))
        if ins.fields:
            rows = []
            for f in ins.fields:
                bits = [f.get("label") or "?"]
                if f.get("type"):
                    bits.append(f["type"])
                if f.get("placeholder"):
                    bits.append(f"占位「{f['placeholder']}」")
                if f.get("maxLength"):
                    bits.append(f"上限 {f['maxLength']}")
                rows.append("；".join(bits))
            out.append("  - 表单字段：" + " | ".join(rows))
        if ins.ui_elements:
            rows = []
            for e in ins.ui_elements:
                bits = [e.get("label") or "?"]
                if e.get("kind"):
                    bits.append(e["kind"])
                if e.get("state"):
                    bits.append(f"状态 {e['state']}")
                rows.append("/".join(bits))
            out.append("  - 可见元素：" + "、".join(rows))
        if ins.states:
            out.append("  - 状态/选项：" + "、".join(ins.states))
        if ins.texts:
            shown = ins.texts[:max_texts]
            more = "" if len(ins.texts) <= max_texts else f"（另有 {len(ins.texts) - max_texts} 条）"
            out.append("  - 图中原文（`expected` 应优先引用原文）："
                       + "、".join(f"「{t}」" for t in shown) + more)
        if ins.flows:
            out.append("  - 流程：" + " → ".join(ins.flows))
        if ins.notes:
            out.append(f"  - 不确定：{ins.notes}")
    return "\n".join(out)


def to_json(spec: ImageSpec) -> dict:
    return {
        "schemaVersion": "1.0",
        "promptVersion": VISION_PROMPT_VERSION,
        "refs": [asdict(r) for r in spec.refs],
        "insights": [asdict(i) for i in spec.insights],
        "warnings": spec.warnings,
        "summary": spec.summary(),
    }


def load_spec(data: dict) -> ImageSpec:
    return ImageSpec(
        refs=[ImageRef(**r) for r in data.get("refs") or []],
        insights=[ImageInsight(**i) for i in data.get("insights") or []],
        warnings=list(data.get("warnings") or []),
    )
