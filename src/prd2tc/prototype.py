"""交互式设计产物解析：把可运行原型（HTML/JS）转成「UI 契约」真值。

## 为什么需要它

真实 PRD 常引用一份可运行原型（如 `养老Agent后台-prototype-测试内嵌版.html`），
而**页面的真实标识符只存在于原型里**：PRD 正文既没有 `data-testid`，也不写真实路由。

实测运行 `c653dc92`：PRD 中 `data-testid` 出现 **0** 次，原型也未随 PRD 送入模型，
而提示词却硬性要求 `target` 用「元素中文名 (data-testid)」格式 ——
模型只能编造，产出 **143 个自拟标识符**、**4 种并存写法**、
以及原型中根本不存在的 URL 路由（`/knowledge/list` vs `/agent/knowledge-base`）。

本模块把原型解析为一份**可机读的 UI 契约**，从而：
1. 阶段 3 拿到真实标识符与真实文案（不再编造）；
2. 门禁可以按「契约」校验 target，而不是只判非空。

## 解析策略（零依赖，纯 stdlib）

原型是**单页 JS 应用**，真正的 DOM 在 `<script>` 的模版字符串里，
静态 HTML 只剩外壳（`nav`/`app`/`modal`/`toast`）。因此必须同时解析：

| 来源 | 抽取内容 |
|---|---|
| 静态 HTML 属性 | `id` / `class` / `placeholder` / `maxlength` / `data-*` |
| JS 模版字符串 | `id=` / `class=` / `<button>` 文案 / `<th>` 列头 |
| JS 状态声明 | `let page='config',tab='main',dirty=false,...` |
| JS 数组字面量 | `navs`（导航页）/ `tabs`（页签） |
| JS 函数调用 | `modal(...)`（弹窗标题与按钮）/ `toast(...)`（提示文案） |

输出**确定性**（排序固定），可参与可复现性对拍。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class UiAnchor:
    """一个可用于定位的元素锚点。"""
    kind: str          # id | class | data-attr
    value: str         # 锚点值（如 promptEditor / file-drop）
    label: str = ""    # 人类可读标签（如「文档名称输入框」）


@dataclass
class UiField:
    id: str = ""
    label: str = ""
    tag: str = ""
    placeholder: str = ""
    maxlength: str = ""


@dataclass
class UiModal:
    title: str
    buttons: list[str] = field(default_factory=list)


@dataclass
class UiSpec:
    """原型的 UI 契约（机读真值）。"""
    source: str = ""
    pages: list[dict] = field(default_factory=list)     # 导航页 [{key,label}]
    tabs: list[dict] = field(default_factory=list)      # 页签 [{key,label}]
    anchors: list[UiAnchor] = field(default_factory=list)
    fields: list[UiField] = field(default_factory=list)
    buttons: list[str] = field(default_factory=list)
    modals: list[UiModal] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)   # toast / 校验文案
    tables: list[list[str]] = field(default_factory=list)
    state: dict = field(default_factory=dict)
    interactions: list[str] = field(default_factory=list)  # onclick 函数名
    has_url_routing: bool = False                       # 是否存在真实 URL 路由

    # ---- 查询辅助（供门禁使用）----
    def anchor_values(self) -> set[str]:
        return {a.value for a in self.anchors}

    def known_identifier(self, ident: str) -> bool:
        return ident in self.anchor_values()

    def summary(self) -> dict:
        return {
            "source": self.source,
            "pages": [p["key"] for p in self.pages],
            "tabs": [t["key"] for t in self.tabs],
            "anchorCount": len(self.anchors),
            "buttonCount": len(self.buttons),
            "modalCount": len(self.modals),
            "messageCount": len(self.messages),
            "hasUrlRouting": self.has_url_routing,
        }


# ---------------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------------

_SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.S | re.I)
_STYLE_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.S | re.I)
# id="x" / id='x'（跳过模版表达式）；注意 `[^>]*` 不可用于标签匹配，否则会吃掉后续标签
_ID_RE = re.compile(r"""\bid\s*=\s*["']([A-Za-z_][\w-]*)["']""")
_CLASS_RE = re.compile(r"""\bclass\s*=\s*["']([^"']+)["']""")
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.I)
_TEXTAREA_RE = re.compile(r"<textarea\b[^>]*>", re.I)
# 关键：`<th(?:\s[^>]*)?>` —— 若写成 `<th[^>]*>`，re.I 下会连 `<thead>` 一起匹配，
# 再用非贪婪 (.*?)</th> 就会把整段表格吞成一项（实测踩到此坑）。
_TH_RE = re.compile(r"<th(?:\s[^>]*)?>(.*?)</th>", re.S | re.I)
_BUTTON_RE = re.compile(r"<button(?:\s[^>]*)?>(.*?)</button>", re.S | re.I)
_LABEL_RE = re.compile(r"<label(?:\s[^>]*)?>(.*?)</label>", re.S | re.I)
_TOAST_RE = re.compile(r"toast\(\s*(?:'((?:[^'\\]|\\.)*)'|\"([^\"]*)\"|`([^`]*)`)\s*\)")
_URL_ROUTING_RE = re.compile(r"location\.(?:hash|pathname|href)\s*=|history\.pushState|window\.location\s*=")
_ONCLICK_FN_RE = re.compile(
    r"""on(?:click|change|input|drop|dragover)\s*=\s*["'][^"']*?([A-Za-z_$][\w$]*)\s*\(""")

# 模版/字符串拼接残留：`'+(cond?'a':'b')+'`、`${...}`、孤立引号
_TERNARY_RE = re.compile(r"\?\s*'([^']*)'\s*:\s*'([^']*)'")
_TEMPLATE_EXPR_RE = re.compile(r"\$\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}")
# 明显不是 UI 文案的标记（属性值/状态名/变量名）
_NON_LABEL = {
    "active", "wide", "primary", "link", "danger", "on", "off", "empty",
    "true", "false", "null", "undefined", "class", "style", "hidden",
}


def _balanced(text: str, start: int, open_ch: str = "[", close_ch: str = "]") -> str:
    """从 `start`（指向 open_ch）起做平衡扫描，返回含首尾括号的子串；失败返回 ""。

    用于提取 `navs` 这类**嵌套数组**：正则 `\\[[^\\]]*\\]` 会在第一个 `]` 处截断，
    实测导致导航页只解析出 1/3。字符串与转义需跳过，避免 `'a]b'` 造成误判。
    """
    if start >= len(text) or text[start] != open_ch:
        return ""
    depth = 0
    quote = ""
    i = start
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'`":
            quote = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return ""


def _split_top_args(call_args: str) -> list[str]:
    """按顶层逗号切分函数实参（跳过括号/引号内部）。"""
    out: list[str] = []
    depth = 0
    quote = ""
    cur: list[str] = []
    i = 0
    while i < len(call_args):
        ch = call_args[i]
        if quote:
            cur.append(ch)
            if ch == "\\":
                if i + 1 < len(call_args):
                    cur.append(call_args[i + 1])
                    i += 2
                    continue
            elif ch == quote:
                quote = ""
        elif ch in "\"'`":
            quote = ch
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    if cur:
        out.append("".join(cur).strip())
    return out


def _literal(value: str) -> str:
    """从 JS 表达式取字样面量；三元表达式取全部候选。"""
    v = value.strip()
    if re.fullmatch(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", v):
        return v[1:-1]
    cands = [a for a, _ in _TERNARY_RE.findall(v)] + [b for _, b in _TERNARY_RE.findall(v)]
    if cands:
        return " / ".join(dict.fromkeys(c for c in cands if c))
    found = re.findall(r"'([^']{1,40})'", v)
    return " / ".join(dict.fromkeys(found)) if found else ""


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def _clean_label(text: str) -> str:
    """把模版字符串/拼接残留压成可读标签。

    原型里按钮文案常有三种形态：
      `${label}`、`'+(m.expanded?'收起':'展开全部')+'`、纯文本。
    这里统一展开三元候选、抹掉表达式与孤立引号。
    """
    t = _strip_tags(text)
    # 先处理字符串拼接形态：'+(cond?'a':'b')+'  → a / b
    t = re.sub(r"'\+|\+'", "", t)
    had_paren = t.lstrip().startswith("(")
    def _tern(m):
        a, b = m.group(1), m.group(2)
        return " / ".join(x for x in dict.fromkeys([a, b]) if x)
    t = _TERNARY_RE.sub(_tern, t)
    # 去掉三元展开后残留的条件前缀，如 `(m.expanded收起 / 展开全部)` → `收起 / 展开全部`
    # 注意必须用 ASCII 限定的标识符：[A-Za-z_$][A-Za-z0-9_$]* —— 若写成 `[\w$]+`，
    # Unicode 下 `\w` 会匹配中文，导致 `文档名称` 被吃掉前缀变成 `称`（实测踩到此坑）。
    t = re.sub(r"^[(\s]*[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z0-9_$]+)*"
               r"(?=[\u4e00-\u9fff«×⛶＋])", "", t)
    t = _TEMPLATE_EXPR_RE.sub("…", t)
    t = t.replace("\\n", " ")
    t = re.sub(r"\s+", " ", t).strip()
    t = t.strip("'\"` ")
    # 仅当因去掉前导 `(` 才配对去掉尾部 `)`；否则会误伤 `通话时长(s)` 这类真实列名
    if had_paren and t.endswith(")"):
        t = t[:-1]
    return t.strip()


def _is_ui_label(text: str) -> bool:
    t = text.strip()
    if not t or set(t) <= set("… .·-—'\""):
        return False
    if t.lower() in _NON_LABEL:
        return False
    if len(t) > 46:
        return False
    # 至少含一个中文，或为图标/短符号（× ⛶ ＋ »
    return bool(re.search(r"[\u4e00-\u9fff×⛶＋»⇧▤♙▧]", t))



def _parse_pairs(array_literal: str) -> list[dict]:
    """解析 [['key','label'],...] 或 [['key','icon','label'],...]。"""
    out: list[dict] = []
    for item in re.findall(r"\[([^\[\]]{0,240})\]", array_literal):
        vals = [a or b for a, b in re.findall(r"'([^']*)'|\"([^\"]*)\"", item)]
        if len(vals) < 2:
            continue
        key = vals[0].strip()
        label = vals[-1].strip()
        # 图标项（▤ ♙ ▧）不是标签
        if len(vals) >= 3 and len(vals[1]) <= 2:
            label = vals[-1].strip()
        if not re.fullmatch(r"[A-Za-z_][\w-]*", key):
            continue
        if not label:
            continue
        out.append({"key": key, "label": label})
    return out


def _find_array_assigned_to(js: str, name: str) -> str:
    """定位 `const navs = [...]` 的完整数组字面量（平衡扫描，支持嵌套）。"""
    m = re.search(rf"\b(?:const|let|var)\s+{re.escape(name)}\s*=\s*", js)
    if not m:
        return ""
    return _balanced(js, m.end(), "[", "]")


def _find_tab_array(js: str) -> list[dict]:
    """定位页签数组：紧跟 `.map(([k,label])` 的那个数组，且渲染时写 `tab='${k}'`。"""
    # 优先：从 tab='${k}' 渲染点反查最近的数组字面量
    for m in re.finditer(r"tab\s*=\s*'\$\{\s*(\w+)\s*\}", js):
        head = js[:m.start()]
        idx = head.rfind("[[")
        while idx != -1:
            lit = _balanced(js, idx, "[", "]")
            pairs = _parse_pairs(lit)
            if len(pairs) >= 2:
                return pairs
            idx = head.rfind("[[", 0, idx)
    # 回退：扫所有 `[[...]]` 数组，取标签最「像页签」的一组
    best: list[dict] = []
    for m in re.finditer(r"\[\[", js):
        lit = _balanced(js, m.start(), "[", "]")
        if not lit:
            continue
        pairs = _parse_pairs(lit)
        if len(pairs) < 2:
            continue
        labels = [p["label"] for p in pairs]
        score = sum(1 for x in labels if x in TABS_HINT)
        if score > sum(1 for x in [p["label"] for p in best] if x in TABS_HINT):
            best = pairs
    return best


TABS_HINT = ("主提示词", "工具", "业务场景", "提示词", "配置", "历史")


def _extract_fields(html: str, js: str) -> list[UiField]:
    """抽取表单字段：id + 邻近 label + placeholder + maxlength。"""
    blob = html + "\n" + js
    fields: dict[str, UiField] = {}
    for m in re.finditer(r"<(input|textarea)\b[^>]*>", blob, re.I):
        tag, tagtext = m.group(1).lower(), m.group(0)
        fid = re.search(r"""\bid\s*=\s*["']([A-Za-z_][\w-]*)["']""", tagtext)
        if not fid:
            continue
        # 向前 260 字符找最近的 <label> 或 class="field-label"
        before = blob[max(0, m.start() - 260):m.start()]
        labels = _LABEL_RE.findall(before) or \
            re.findall(r"""class\s*=\s*["']field-label["'][^>]*>([^<]{1,30})<""", before)
        ph = re.search(r"""placeholder\s*=\s*["']([^"']{0,60})["']""", tagtext)
        ml = re.search(r"""maxlength\s*=\s*["']?(\d+)""", tagtext)
        fields[fid.group(1)] = UiField(
            id=fid.group(1),
            label=_clean_label(labels[-1]) if labels else "",
            tag=tag,
            placeholder=ph.group(1) if ph else "",
            maxlength=ml.group(1) if ml else "",
        )
    return [fields[k] for k in sorted(fields)]


def _extract_state(js: str) -> dict:
    """抽取 `let a=1,b='x',c=false;` 形式的初始状态（仅标量/空容器）。"""
    state: dict = {}
    m = re.search(r"\b(?:let|const|var)\s+([\w$]+\s*=\s*[^;]{0,400};)", js)
    if not m:
        return state
    for part in _split_top_args(m.group(1).rstrip(";")):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        k, v = k.strip(), v.strip()
        if not re.fullmatch(r"[\w$]+", k or ""):
            continue
        if re.fullmatch(r"'(?:[^']*)'|\"(?:[^\"]*)\"", v):
            state[k] = v[1:-1]
        elif re.fullmatch(r"-?\d+(?:\.\d+)?", v):
            state[k] = float(v) if "." in v else int(v)
        elif v in ("true", "false"):
            state[k] = v == "true"
        elif v in ("[]", "{}"):
            state[k] = [] if v == "[]" else {}
    return {k: state[k] for k in sorted(state)}


def _extract_modals(js: str) -> list[UiModal]:
    """提取 `modal(title, body, ok, action, wide)` 的标题与按钮。

    原型里弹窗按钮由渲染函数统一生成：`ok === '关闭'` 时只有 1 个按钮，
    否则为「取消 + ok」两个 —— **从不超过 2 个**。
    因此按钮取自第 3 个实参，而不是扫描函数体（后者会抓到无关按钮）。
    """
    modals: dict[str, UiModal] = {}
    for m in re.finditer(r"\bmodal\s*\(", js):
        lit = _balanced(js, m.end() - 1, "(", ")")
        if not lit:
            continue
        args = _split_top_args(lit[1:-1])
        if len(args) < 3:
            continue
        # 标题必须含字样面量：`modal(title,body,ok,...)` 是**函数定义**自身，
        # `modal(d.name,...)` 是动态标题 —— 二者都不是真实弹窗清单，须剔除。
        title = _literal(args[0])
        if not title or len(title) > 50:
            continue
        ok = _literal(args[2]) or args[2].strip()
        buttons = ["关闭"] if ok == "关闭" else ["取消", ok]
        modals.setdefault(title, UiModal(title=title, buttons=buttons))
    return [modals[k] for k in sorted(modals)]


def _extract_tables(js: str) -> list[list[str]]:
    """按 <thead> 分组抽取表格列头。

    不可直接对全文跑 `<th>` 正则再 `for c in cols` —— 那会把单个列名字符串
    按字符拆开（实测产出 `["会","话","","I","D"]` 这类结果）。
    """
    tables: list[list[str]] = []
    for m in re.finditer(r"<thead(?:\s[^>]*)?>(.*?)</thead>", js, re.S | re.I):
        cols = [_clean_label(c) for c in _TH_RE.findall(m.group(1))]
        cols = [c for c in cols if _is_ui_label(c)]
        if len(cols) >= 2:
            tables.append(cols)
    return [list(t) for t in sorted({tuple(t) for t in tables})]


def extract_ui_spec(html: str, *, source: str = "") -> UiSpec:
    """从原型 HTML 抽取 UI 契约（确定性输出：所有列表均已排序）。"""
    scripts = _SCRIPT_RE.findall(html)
    js = "\n".join(scripts)
    blob = _STYLE_RE.sub("", html) + "\n" + js

    # --- 锚点：id / class / data-* ---
    # 说明：`class="x"` 属性匹配到的每一项都是**真实使用过的类**，故不再按样式表过滤
    #（样式表中出现恰恰是该类真实存在的旁证）。
    anchors: dict[tuple[str, str], UiAnchor] = {}
    for ident in _ID_RE.findall(blob):
        anchors[("id", ident)] = UiAnchor("id", ident)
    for cls_attr in _CLASS_RE.findall(blob):
        for c in cls_attr.split():
            if "${" in c or len(c) < 3:
                continue
            anchors.setdefault(("class", c), UiAnchor("class", c))
    for attr in re.findall(r"\b(data-[\w-]+)\s*=", blob):
        anchors.setdefault(("data-attr", attr), UiAnchor("data-attr", attr))

    # --- 导航页 / 页签（平衡扫描，避免嵌套数组被截断）---
    pages = _parse_pairs(_find_array_assigned_to(js, "navs"))
    tabs = _find_tab_array(js)

    # --- 按钮 / 表格列头 ---
    buttons = sorted({_clean_label(b) for b in _BUTTON_RE.findall(js)
                      if _is_ui_label(_clean_label(b))})

    # --- 提示文案 ---
    messages: list[str] = []
    for m in _TOAST_RE.finditer(js):
        val = next((g for g in m.groups() if g), "")
        val = _TEMPLATE_EXPR_RE.sub("…", val)
        if val:
            messages.append(val)

    return UiSpec(
        source=source,
        pages=pages,
        tabs=tabs,
        anchors=[anchors[k] for k in sorted(anchors)],
        fields=_extract_fields(html, js),
        buttons=buttons,
        modals=_extract_modals(js),
        messages=sorted(set(messages)),
        tables=_extract_tables(js),
        state=_extract_state(js),
        interactions=sorted(set(_ONCLICK_FN_RE.findall(blob))),
        has_url_routing=bool(_URL_ROUTING_RE.search(blob)),
    )


def parse_prototype(path: str | Path) -> UiSpec:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"原型文件不存在: {p}")
    raw = p.read_text(encoding="utf-8", errors="replace")
    if not re.search(r"<(?:html|body|div|script)", raw, re.I):
        raise ValueError(f"不是可识别的 HTML 原型: {p}")
    return extract_ui_spec(raw, source=p.name)


def merge_specs(specs: list[UiSpec]) -> UiSpec:
    """合并多份原型（同产品多个页面原型）。"""
    if not specs:
        return UiSpec()
    if len(specs) == 1:
        return specs[0]
    out = UiSpec(source=", ".join(s.source for s in specs if s.source))
    seen_page: dict[str, dict] = {}
    seen_tab: dict[str, dict] = {}
    seen_anchor: dict[tuple[str, str], UiAnchor] = {}
    seen_modal: dict[str, UiModal] = {}
    for s in specs:
        for p in s.pages:
            seen_page.setdefault(p["key"], p)
        for t in s.tabs:
            seen_tab.setdefault(t["key"], t)
        for a in s.anchors:
            seen_anchor.setdefault((a.kind, a.value), a)
        for m in s.modals:
            seen_modal.setdefault(m.title, m)
    out.pages = [seen_page[k] for k in sorted(seen_page)]
    out.tabs = [seen_tab[k] for k in sorted(seen_tab)]
    out.anchors = [seen_anchor[k] for k in sorted(seen_anchor)]
    out.fields = [f for s in specs for f in s.fields]
    out.buttons = sorted({b for s in specs for b in s.buttons})
    out.modals = [seen_modal[k] for k in sorted(seen_modal)]
    out.messages = sorted({m for s in specs for m in s.messages})
    out.tables = [t for s in specs for t in s.tables]
    out.state = {k: v for s in specs for k, v in s.state.items()}
    out.interactions = sorted({i for s in specs for i in s.interactions})
    out.has_url_routing = any(s.has_url_routing for s in specs)
    return out


def to_json(spec: UiSpec) -> dict:
    return {
        "schemaVersion": "1.0",
        "source": spec.source,
        "pages": spec.pages,
        "tabs": spec.tabs,
        "anchors": [asdict(a) for a in spec.anchors],
        "fields": [asdict(f) for f in spec.fields],
        "buttons": spec.buttons,
        "modals": [asdict(m) for m in spec.modals],
        "messages": spec.messages,
        "tables": spec.tables,
        "state": spec.state,
        "interactions": spec.interactions,
        "hasUrlRouting": spec.has_url_routing,
    }


def load_spec(data: dict) -> UiSpec:
    return UiSpec(
        source=data.get("source", ""),
        pages=list(data.get("pages") or []),
        tabs=list(data.get("tabs") or []),
        anchors=[UiAnchor(**a) for a in data.get("anchors") or []],
        fields=[UiField(**f) for f in data.get("fields") or []],
        buttons=list(data.get("buttons") or []),
        modals=[UiModal(**m) for m in data.get("modals") or []],
        messages=list(data.get("messages") or []),
        tables=[list(t) for t in data.get("tables") or []],
        state=dict(data.get("state") or {}),
        interactions=list(data.get("interactions") or []),
        has_url_routing=bool(data.get("hasUrlRouting")),
    )


# ---------------------------------------------------------------------------
# 渲染为 prompt 片段
# ---------------------------------------------------------------------------

def render_ui_spec(spec: UiSpec, *, max_anchors: int = 80) -> str:
    """把 UI 契约渲染为可注入 prompt 的紧凑清单（确定性）。"""
    if not any((spec.pages, spec.tabs, spec.anchors, spec.buttons, spec.messages)):
        return ""
    out: list[str] = ["## 原型 UI 契约（**以下标识符与文案为真实值，必须原样使用；不得编造新的标识符或路由**）"]
    if spec.source:
        out.append(f"来源原型：{spec.source}")
    if spec.has_url_routing:
        out.append("- 该原型**存在** URL 路由，可直接使用其地址。")
    else:
        out.append("- ⚠ 该原型是**单页应用，不存在 URL 路由**：`goto` 请使用下列页面键"
                   "（如 `page=config`）或页面名，**不得编造 `/xxx` 路径**。")
    if spec.pages:
        out.append("- 导航页：" + "、".join(f"{p['key']}（{p['label']}）" for p in spec.pages))
    if spec.tabs:
        out.append("- 页签：" + "、".join(f"{t['key']}（{t['label']}）" for t in spec.tabs))
    ids = [a.value for a in spec.anchors if a.kind == "id"]
    classes = [a.value for a in spec.anchors if a.kind == "class"]
    if ids:
        out.append(f"- 可用 id 锚点（{len(ids)} 个）：" + "、".join(ids[:max_anchors]))
    if classes:
        shown = classes[:max_anchors]
        more = "" if len(classes) <= max_anchors else f"（另有 {len(classes) - max_anchors} 个）"
        out.append(f"- 可用 class 锚点：{ '、'.join(shown) }{more}")
    if spec.fields:
        rows = []
        for f in spec.fields:
            bits = [f.id]
            if f.label:
                bits.append(f"标签「{f.label}」")
            if f.placeholder:
                bits.append(f"占位「{f.placeholder}」")
            if f.maxlength:
                bits.append(f"上限 {f.maxlength} 字")
            rows.append("；".join(bits))
        out.append("- 表单字段：" + " | ".join(rows))
    if spec.buttons:
        out.append("- 按钮文案：" + "、".join(spec.buttons))
    if spec.tables:
        for cols in spec.tables:
            out.append("- 表格列：" + " | ".join(cols))
    if spec.modals:
        out.append("- 弹窗（**括号内是它的全部按钮，不得增加选项**）："
                   + "、".join(f"{m.title}[{'/'.join(m.buttons)}]" for m in spec.modals))
    if spec.messages:
        more = "" if len(spec.messages) <= 40 else f"（另有 {len(spec.messages) - 40} 条）"
        out.append("- 提示/校验文案（`expected` 应优先引用原文）："
                   + "、".join(f"「{x}」" for x in spec.messages[:40]) + more)
    out.append("- target 写法：`页面元素中文名 (id=真实id)` 或 `页面元素中文名 (class=真实class)`；"
               "**不要输出 data-testid** —— 原型中没有该属性。")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 按契约校验用例（供门禁使用）
# ---------------------------------------------------------------------------

_IDENT_IN_TARGET_RE = re.compile(r"\((?:data-testid=)?([A-Za-z_][\w.\-]*)\)\s*$")
_PATH_IN_TARGET_RE = re.compile(r"(?:^|[\s(])(/[A-Za-z0-9_\-/{}.]+)")


def validate_targets(cases: list[dict], spec: UiSpec, *,
                     at_prefix: str = "") -> list[str]:
    """按原型契约校验用例的 target 与 goto 地址，返回错误列表。

    检查项：
      1. target 中括号内的标识符必须是契约中的真实锚点（否则是编造）；
      2. 原型无 URL 路由时，`goto` 不得使用 `/xxx` 路径；
      3. 原型无 URL 路由时，`goto` 应落在已知页面键或页面名上。
    """
    if not spec.anchors and not spec.pages:
        return []
    known = spec.anchor_values()
    page_keys = {p["key"] for p in spec.pages}
    page_labels = {p["label"] for p in spec.pages}
    errors: list[str] = []
    for case in cases:
        at = f"{at_prefix}[用例 {case.get('id', '(无id)')}]"
        for step in case.get("steps") or []:
            if not isinstance(step, dict):
                continue
            target = str(step.get("target") or "")
            action = str(step.get("action") or "")
            ident = _IDENT_IN_TARGET_RE.search(target)
            if ident:
                value = ident.group(1)
                # 形如 `panel-text-test.collapsed` 的状态路径不是锚点，单独提示
                base = value.split(".")[0]
                if value not in known and base not in known:
                    errors.append(
                        f"{at} target 引用了契约中不存在的标识符 `{value}`（原型仅有 "
                        f"{len(known)} 个真实锚点）；请改用真实 id/class 或去掉该标识符")
            if not spec.has_url_routing:
                path = _PATH_IN_TARGET_RE.search(target)
                data = str(step.get("data") or "")
                if path and not (path.group(1) in page_keys):
                    errors.append(
                        f"{at} target 出现 URL 路径 `{path.group(1)}`，但原型是单页应用、"
                        f"不存在 URL 路由；`goto` 应使用页面键 {sorted(page_keys)}")
                if action == "goto" and data.startswith("/"):
                    errors.append(
                        f"{at} goto 的 data 为 URL `{data}`，但原型无 URL 路由；"
                        f"请改用页面键 {sorted(page_keys)}")
                if action == "goto" and not data.startswith("/") and data:
                    if data not in page_keys and not any(lbl in data for lbl in page_labels) \
                            and data not in known:
                        errors.append(
                            f"{at} goto 的目标 `{data}` 不在原型页面中；"
                            f"应为 {sorted(page_keys)} 或页面名之一")
    return errors
