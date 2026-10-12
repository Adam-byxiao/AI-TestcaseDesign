"""PRD 图片发现、关联与视觉分析测试。

背景：PRD 下载为 md 后图片通常是**同目录/子目录的独立文件**，而非内联。
实测真实 PRD 含 8 处图片引用（全为需鉴权的远程链接），本地原无图片、
loader 也不解析图片 —— 这些图**从未参与生成**，直接导致模型臆造交互
（如凭 PRD 一句「弹窗提醒」编出三选项弹窗，而设计稿里根本没有该弹窗）。

本文件用自包含 fixture 固化：发现 → 关联（含用户实际提供的图片目录）→
分析（打桩视觉模型）→ 渲染 → 降级。
"""
import json

import pytest

from prd2tc import images
from prd2tc.config import Config
from prd2tc.images import (ImageRef, ImageSpec, ImageInsight, cache_key, discover_refs,
                           fetch_remote, longest_common_substring, prepare, read_image,
                           render_image_spec, resolve_refs, sniff_mime, to_json,
                           load_spec, analyze_images)

# 最小合法 PNG（含魔数，足以通过嗅探）
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                    "0000000a49444154789c6300010000050001")

PRD = """# 某后台改造 PRD

## 6.1 配置结构

![智能体配置整体：页签布局及默认收起的测试入口](https://feishu.cn/file/AAA)

正文说明。

## 6.2 上传窗口

![添加文档知识：名称填写、文件上传及保存窗口](https://feishu.cn/file/BBB)

## 6.3 RAG 参考

![RAG接口清单：业务标注参考](https://feishu.cn/file/CCC)
"""


def _write_images(dirpath, names):
    """写出图片文件。

    每个文件的**字节内容不同**（把文件名附加在 PNG 魔数之后）：
    `ingest` 会按内容哈希去重，若三张图字节相同只会有 1 张成为资产 ——
    那是正确行为，但会让「关联 3 条引用」的断言失去意义。
    """
    dirpath.mkdir(parents=True, exist_ok=True)
    out = []
    for n in names:
        p = dirpath / n
        p.write_bytes(PNG + n.encode("utf-8"))
        out.append(p)
    return out


@pytest.fixture
def prd_file(tmp_path):
    (tmp_path / "prd.md").write_text(PRD, encoding="utf-8")
    return tmp_path / "prd.md"


# ---------- 发现 ----------

def test_discovers_markdown_images_with_context():
    refs = discover_refs(PRD)
    assert len(refs) == 3
    assert refs[0].index == 1
    assert refs[0].alt == "智能体配置整体：页签布局及默认收起的测试入口"
    assert refs[0].target.endswith("AAA")
    assert refs[0].remote is True


def test_attaches_section_and_context():
    refs = discover_refs(PRD)
    assert refs[0].section == "6.1 配置结构"
    assert refs[1].section == "6.2 上传窗口"
    assert refs[2].section == "6.3 RAG 参考"
    assert "正文说明" in refs[1].context or "上传窗口" in refs[1].context


def test_discovers_html_img_and_local_paths():
    md = 'text <img src="pics/a.png"> more\n\n![](./b.jpg)\n'
    refs = discover_refs(md)
    targets = [r.target for r in refs]
    assert "pics/a.png" in targets and "./b.jpg" in targets
    assert all(r.remote is False for r in refs)


def test_no_images_returns_empty():
    assert discover_refs("# 文档\n\n没有图片。") == []


def test_deterministic_order():
    a = discover_refs(PRD)
    b = discover_refs(PRD)
    assert [(r.index, r.alt, r.target) for r in a] == [(r.index, r.alt, r.target) for r in b]


# ---------- 关联 ----------

def test_longest_common_substring():
    assert longest_common_substring("业务场景编辑", "业务场景新增删除") == 4
    assert longest_common_substring("abc", "xyz") == 0
    assert longest_common_substring("", "x") == 0


def test_resolve_by_alt_when_names_differ(prd_file, tmp_path):
    """**核心**：图注长、文件名短且互不包含时，仍须关联成功。

    实测「业务场景：新增、删除与带行号的内容编辑」↔ `业务场景编辑.png`：
    两者互不包含（→ 用「包含」判定会失败），但共享 4 字子串「业务场景」。
    本用例刻意使用**互不包含**的名字，以真正锁住「最长公共子串」这一实现。
    """
    md = ("## 6.3 业务场景\n\n"
          "![业务场景：新增、删除与带行号的内容编辑](https://feishu.cn/file/E98)\n\n"
          "## 6.4 文字测试\n\n"
          "![内嵌文字测试：配置与测试并排、长工具结果折叠](https://feishu.cn/file/Tsq)\n\n"
          "## 6.5 知识库\n\n"
          "![知识库配置：文档列表及添加入口](https://feishu.cn/file/Ghd)\n\n"
          "## 6.6 命中测试\n\n"
          "![知识命中测试：片段、文档来源及评分](https://feishu.cn/file/O49)\n")
    p = tmp_path / "prd.md"
    p.write_text(md, encoding="utf-8")
    d = tmp_path / "图片和附件"
    _write_images(d, ["业务场景编辑.png", "文字测试界面.png",
                      "知识库配置与添加.png", "知识库命中测试.png"])

    refs = resolve_refs(discover_refs(md), p, extra_dirs=[d])
    assert all(r.resolved for r in refs), [r.alt for r in refs if not r.resolved]
    assert all(r.match == "alt" for r in refs), [r.match for r in refs]
    # 按图注显式断言配对（比比较排序结果更能说明「关联正确」）
    pair = {r.alt: (r.resolved or "").rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for r in refs}
    assert pair["业务场景：新增、删除与带行号的内容编辑"] == "业务场景编辑.png", pair
    assert pair["内嵌文字测试：配置与测试并排、长工具结果折叠"] == "文字测试界面.png", pair
    assert pair["知识库配置：文档列表及添加入口"] == "知识库配置与添加.png", pair
    assert pair["知识命中测试：片段、文档来源及评分"] == "知识库命中测试.png", pair

    # 断言这确实是「互不包含」场景，否则测试退化为无意义的包含匹配
    for alt, name in [("业务场景新增删除与带行号的内容编辑", "业务场景编辑"),
                      ("内嵌文字测试配置与测试并排长工具结果折叠", "文字测试界面")]:
        assert alt not in name and name not in alt, (alt, name)
        assert longest_common_substring(alt, name) >= images.MIN_ALT_OVERLAP


def test_resolve_by_alt_when_names_differ_simple(prd_file, tmp_path):
    """同一行为的基础版：文件名是图注前缀（包含即可命中）。"""
    d = tmp_path / "图片和附件"
    _write_images(d, ["智能体配置整体.jpeg", "添加文档知识弹窗.png", "rag接口清单.jpeg"])
    refs = resolve_refs(discover_refs(PRD), prd_file, extra_dirs=[d])
    assert all(r.resolved for r in refs), [r.alt for r in refs if not r.resolved]
    assert all(r.match == "alt" for r in refs)
    names = {r.index: r.resolved.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for r in refs}
    assert names[1] == "智能体配置整体.jpeg"
    assert names[2] == "添加文档知识弹窗.png"
    assert names[3] == "rag接口清单.jpeg"


def test_find_local_single_ref_delegates_to_resolve(prd_file, tmp_path):
    """单条引用的便捷入口必须与批量关联同源（不得各写一套评分逻辑）。"""
    d = tmp_path / "图片和附件"
    _write_images(d, ["业务场景编辑.png"])
    ref = ImageRef(index=1, alt="业务场景：新增、删除与带行号的内容编辑",
                   target="https://feishu.cn/file/E98", remote=True)
    path, how = images.find_local(ref, prd_file, pool=[d])
    assert how == "alt" and path.endswith("业务场景编辑.png"), (path, how)

    refs = resolve_refs([ImageRef(index=1, alt=ref.alt, target=ref.target, remote=True)],
                        prd_file, extra_dirs=[d])
    assert (refs[0].resolved, refs[0].match) == (path, how)


def test_min_overlap_threshold_rejects_vague_match(tmp_path):
    """公共子串不足阈值（如仅「配置」2 字）时不得**按图注**强行关联。

    注意 `order` 兜底是设计行为（数量相等时按序分配，且已显式标记低置信），
    因此这里断言的是「未走 alt 路径」，而不是「完全未匹配」。
    """
    md = "![完全无关的图注呀](https://x/a)\n![另一段无关说明](https://x/b)\n"
    p = tmp_path / "prd.md"
    p.write_text(md, encoding="utf-8")
    d = tmp_path / "assets"
    _write_images(d, ["配置.png"])
    refs = resolve_refs(discover_refs(md), p, extra_dirs=[d])
    assert all(r.match != "alt" for r in refs), [(r.alt, r.match) for r in refs]
    # 数量不等（2 引用 vs 1 文件）→ 也不应触发 order 兜底
    assert all(not r.resolved for r in refs), [r.resolved for r in refs]


def test_resolve_is_unique_globally(prd_file, tmp_path):
    """同一文件不得被两条引用占用（否则证据张冠李戴）。

    构造「两处图注都指向同一张图」的场景：目录里只有 1 张可用图，
    但另有一张次优候选。若无全局唯一分配，两处引用会双双落到同一文件。
    """
    md = ("## 6.1 业务场景\n\n![业务场景：新增与删除](https://x/1)\n\n"
          "## 6.2 业务场景配置\n\n![业务场景：编辑与保存](https://x/2)\n")
    p = tmp_path / "prd.md"
    p.write_text(md, encoding="utf-8")
    d = tmp_path / "assets"
    _write_images(d, ["业务场景编辑.png", "业务场景新增.png"])
    refs = resolve_refs(discover_refs(md), p, extra_dirs=[d])
    used = [r.resolved for r in refs if r.resolved]
    assert len(used) == len(set(used)), f"同一文件被重复占用: {used}"
    assert len(used) == 2, f"应两张都分配出去，实际 {used}"
    names = sorted((r.resolved or "").rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for r in refs)
    assert names == ["业务场景新增.png", "业务场景编辑.png"], names


def test_resolve_is_unique_when_only_one_candidate(tmp_path):
    """候选只有 1 张、引用有 2 条时：只能分配 1 条，另一条保持未匹配（不得复用）。"""
    md = "![业务场景：新增](https://x/1)\n![业务场景：删除](https://x/2)\n"
    p = tmp_path / "prd.md"
    p.write_text(md, encoding="utf-8")
    d = tmp_path / "assets"
    _write_images(d, ["业务场景.png"])
    refs = resolve_refs(discover_refs(md), p, extra_dirs=[d])
    assert sum(1 for r in refs if r.resolved) == 1, [r.resolved for r in refs]


def test_resolve_prefers_filename_over_alt(prd_file, tmp_path):
    md = "![无关图注](https://x/exact.png)\n"
    p = tmp_path / "prd.md"
    p.write_text(md, encoding="utf-8")
    d = tmp_path / "images"
    _write_images(d, ["other.png", "exact.png"])
    refs = resolve_refs(discover_refs(md), p, extra_dirs=[d])
    assert refs[0].match == "filename"
    assert refs[0].resolved.endswith("exact.png")


def test_resolve_explicit_relative_path(tmp_path):
    (tmp_path / "pics").mkdir()
    (tmp_path / "pics" / "a.png").write_bytes(PNG)
    md = "![说明](./pics/a.png)\n"
    p = tmp_path / "prd.md"
    p.write_text(md, encoding="utf-8")
    refs = resolve_refs(discover_refs(md), p)
    assert refs[0].match == "explicit"
    assert refs[0].resolved.endswith("a.png")


def test_order_fallback_only_when_counts_match(tmp_path):
    """顺序兜底仅在数量相等时启用（低置信，避免乱配）。"""
    p = tmp_path / "prd.md"
    p.write_text("![甲](https://x/1)\n![乙](https://x/2)\n", encoding="utf-8")
    d = tmp_path / "assets"
    _write_images(d, ["zzz.png", "yyy.png"])
    refs = resolve_refs(discover_refs(p.read_text(encoding="utf-8")), p, extra_dirs=[d])
    assert all(r.match == "order" for r in refs), [r.match for r in refs]

    d2 = tmp_path / "assets2"
    _write_images(d2, ["a.png", "b.png", "c.png"])
    refs2 = resolve_refs(discover_refs(p.read_text(encoding="utf-8")), p, extra_dirs=[d2])
    assert all(not r.resolved for r in refs2), "数量不等时不应顺序兜底"


def test_autodetect_conventional_dirs(prd_file):
    """默认探测 `<stem>.assets/` 等约定目录（图片与 PRD 一起下载的常见形态）。"""
    d = prd_file.parent / "prd.assets"
    _write_images(d, ["智能体配置整体.jpeg", "添加文档知识弹窗.png", "rag接口清单.jpeg"])
    refs = resolve_refs(discover_refs(PRD), prd_file)
    assert all(r.resolved for r in refs)
    assert all(r.match == "alt" for r in refs)


def test_resolve_no_candidates_keeps_unresolved(prd_file):
    refs = resolve_refs(discover_refs(PRD), prd_file)
    assert all(not r.resolved for r in refs)


# ---------- 读取与嗅探 ----------

def test_sniff_mime_variants():
    assert sniff_mime(PNG) == "image/png"
    assert sniff_mime(b"\xff\xd8\xff\xe0" + b"x" * 8) == "image/jpeg"
    assert sniff_mime(b"GIF89a" + b"x" * 8) == "image/gif"
    assert sniff_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_mime(b"not an image") == ""


def test_read_image_ok(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(PNG)
    data, mime = read_image(p)
    assert data == PNG and mime == "image/png"


def test_read_image_rejects_non_image(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError, match="不是可识别的图片格式"):
        read_image(p)


def test_read_image_rejects_empty_and_oversize(tmp_path):
    empty = tmp_path / "e.png"
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="文件为空"):
        read_image(empty)
    big = tmp_path / "big.png"
    big.write_bytes(PNG + b"\x00" * (images.MAX_IMAGE_BYTES + 1))
    with pytest.raises(ValueError, match="超过上限"):
        read_image(big)


def test_fetch_remote_validates(monkeypatch):
    class Resp:
        headers = {"Content-Type": "text/html"}
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=None): return b"<html>not an image</html>"
    monkeypatch.setattr(images.urllib.request, "urlopen", lambda *a, **k: Resp())
    with pytest.raises(ValueError, match="不是图片"):
        fetch_remote("https://x/a.png")


# ---------- 视觉分析（打桩） ----------

def _fake_vision(payload: dict, calls: list | None = None):
    from prd2tc.llm import LLMResult
    def call(cfg, prompt, imgs, **kw):
        if calls is not None:
            calls.append({"prompt": prompt, "n": len(imgs), **kw})
        return LLMResult(json.dumps(payload, ensure_ascii=False), "fake", 10, 5)
    return call


CFG = Config("k", "https://x", "m", 0.4, 10, 1)


def test_analyze_images_produces_structured_insight(tmp_path):
    ref = ImageRef(index=1, alt="智能体配置整体", target="https://x/a.png",
                   resolved=str(tmp_path / "a.png"), match="alt")
    calls = []
    out = analyze_images(CFG, [(ref, PNG, "image/png")],
                         vision_llm=_fake_vision({
                             "kind": "ui-mockup", "summary": "配置页含三页签",
                             "annotated": [{"target": "保存全部草稿", "meaning": "本次新增按钮"}],
                             "uiElements": [{"label": "主提示词", "kind": "tab", "state": "active"}],
                             "fields": [{"label": "文档名称", "type": "input",
                                         "placeholder": "输入文档名称", "maxLength": 20}],
                             "states": ["已发布", "草稿"],
                             "texts": ["16 行 · 235 字"],
                             "layout": "顶部页签 + 右侧收起测试栏",
                         }, calls))
    assert len(out) == 1
    ins = out[0]
    assert ins.kind == "ui-mockup" and ins.summary == "配置页含三页签"
    assert ins.annotated[0]["target"] == "保存全部草稿"
    assert ins.ui_elements[0]["state"] == "active"
    assert ins.fields[0]["maxLength"] == "20"
    assert ins.states == ["已发布", "草稿"]
    assert ins.texts == ["16 行 · 235 字"]
    assert ins.sha256 and len(ins.sha256) == 64
    assert calls and calls[0]["n"] == 1


def test_analyze_uses_cache(tmp_path):
    """同图 + 同模型 + 同提示词版本 → 命中缓存，不重复调用。"""
    ref = ImageRef(index=1, alt="a", target="x", resolved="x")
    calls = []
    fake = _fake_vision({"kind": "ui-mockup", "summary": "s"}, calls)
    first = analyze_images(CFG, [(ref, PNG, "image/png")], vision_llm=fake, cache_dir=tmp_path)
    second = analyze_images(CFG, [(ref, PNG, "image/png")], vision_llm=fake, cache_dir=tmp_path)
    assert len(calls) == 1, f"第二次应命中缓存，实际调用 {len(calls)} 次"
    assert second[0].cached is True
    assert second[0].summary == first[0].summary


def test_cache_key_includes_prompt_version(monkeypatch):
    a = cache_key("sha", "m")
    monkeypatch.setattr(images, "VISION_PROMPT_VERSION", "9.9")
    assert cache_key("sha", "m") != a, "提示词版本必须参与缓存键"


def test_analyze_tolerates_fenced_json(tmp_path):
    from prd2tc.llm import LLMResult
    ref = ImageRef(index=1, alt="a", target="x")
    fake = lambda cfg, p, i, **kw: LLMResult(
        '```json\n{"kind":"flow-diagram","summary":"流程"}\n```', "m", 1, 1)
    out = analyze_images(CFG, [(ref, PNG, "image/png")], vision_llm=fake)
    assert out[0].kind == "flow-diagram"


def test_analyze_skips_broken_model_output_with_warning(tmp_path):
    """模型返回非法 JSON → 跳过该图并告警，不影响其它图。"""
    from prd2tc.llm import LLMResult
    ref = ImageRef(index=1, alt="a", target="x")
    notes = []
    fake = lambda cfg, p, i, **kw: LLMResult("这不是 JSON", "m", 1, 1)
    out = analyze_images(CFG, [(ref, PNG, "image/png")], vision_llm=fake,
                         reporter=notes.append)
    assert out == []
    assert notes and "分析失败" in notes[0]


def test_analyze_coerces_dirty_payload(tmp_path):
    """模型返回多余键/错误类型时必须被规整，不得污染产物。"""
    ref = ImageRef(index=1, alt="a", target="x")
    out = analyze_images(CFG, [(ref, PNG, "image/png")], vision_llm=_fake_vision({
        "kind": "ui-mockup", "summary": "s",
        "uiElements": [{"label": "按钮", "kind": "button", "state": "on", "extra": "丢弃"}],
        "texts": ["a", 1, None, "  ", "b"],
        "states": "不是数组",
    }))
    ins = out[0]
    assert set(ins.ui_elements[0]) == {"label", "kind", "state"}
    assert ins.texts == ["a", "1", "b"]
    assert ins.states == []


# ---------- 编排与降级 ----------

def test_prepare_end_to_end(prd_file, tmp_path):
    d = tmp_path / "图片和附件"
    _write_images(d, ["智能体配置整体.jpeg", "添加文档知识弹窗.png", "rag接口清单.jpeg"])
    spec = prepare(PRD, prd_file, extra_dirs=[d], cfg=CFG,
                   vision_llm=_fake_vision({"kind": "ui-mockup", "summary": "界面"}))
    assert len(spec.refs) == 3
    assert all(r.resolved for r in spec.refs)
    assert len(spec.analyzed()) == 3
    s = spec.summary()
    assert s["refCount"] == 3 and s["resolvedCount"] == 3 and s["analyzedCount"] == 3


def test_prepare_without_analysis_records_refs_and_warns(prd_file, tmp_path):
    """stub / 无视觉模型：仍产出图片清单，并明确告警未识别。"""
    d = tmp_path / "assets"
    _write_images(d, ["智能体配置整体.jpeg", "添加文档知识弹窗.png", "rag接口清单.jpeg"])
    spec = prepare(PRD, prd_file, extra_dirs=[d], analyze=False)
    assert len(spec.refs) == 3 and spec.insights == []
    assert any("未做视觉识别" in w for w in spec.warnings)
    assert any("stub 模式" in w for w in spec.warnings)


def test_prepare_remote_not_fetched_by_default(prd_file, tmp_path):
    spec = prepare(PRD, prd_file, cfg=CFG, vision_llm=_fake_vision({}))
    assert spec.insights == []
    assert sum(1 for w in spec.warnings if "--fetch-images" in w) == 3


def test_prepare_no_images_returns_empty_spec(tmp_path):
    p = tmp_path / "prd.md"
    p.write_text("# 无图文档", encoding="utf-8")
    spec = prepare(p.read_text(encoding="utf-8"), p)
    assert spec.refs == [] and spec.insights == [] and spec.warnings == []
    assert render_image_spec(spec) == ""


def test_prepare_survives_unreadable_image(prd_file, tmp_path):
    """图片损坏时只告警，不中断（其余图片照常分析）。"""
    d = tmp_path / "assets"
    _write_images(d, ["智能体配置整体.jpeg"])
    (d / "添加文档知识弹窗.png").write_text("not an image", encoding="utf-8")
    _write_images(d, ["rag接口清单.jpeg"])
    spec = prepare(PRD, prd_file, extra_dirs=[d], cfg=CFG,
                   vision_llm=_fake_vision({"kind": "ui-mockup", "summary": "ok"}))
    assert len(spec.analyzed()) == 2
    assert any("不是可识别的图片格式" in w for w in spec.warnings)


# ---------- 渲染 ----------

def test_render_marks_inference_not_plaintext():
    """渲染必须显式声明「属推断、冲突以明文为准」，避免图片结论被当成 PRD 事实。"""
    spec = ImageSpec(insights=[ImageInsight(
        index=1, section="6.1 配置", alt="配置整体", kind="ui-mockup",
        summary="三页签配置页", annotated=[{"target": "保存全部草稿", "meaning": "本次新增"}],
        ui_elements=[{"label": "工具", "kind": "tab", "state": ""}],
        fields=[{"label": "文档名称", "type": "input", "placeholder": "输入文档名称",
                 "maxLength": "20"}],
        states=["已发布", "草稿"], texts=["16 行 · 235 字"],
        layout="右侧收起测试栏", flows=["打开页面", "切换页签"])])
    out = render_image_spec(spec)
    assert "属推断结论" in out and "[推断]" in out
    assert "图1" in out and "6.1 配置" in out and "配置整体" in out
    assert "保存全部草稿（本次新增）" in out
    assert "文档名称；input；占位「输入文档名称」；上限 20" in out
    assert "「16 行 · 235 字」" in out
    assert "打开页面 → 切换页签" in out


def test_render_warns_about_unrecognized():
    spec = ImageSpec(
        refs=[ImageRef(index=1, alt="a", target="u"), ImageRef(index=2, alt="b", target="v")],
        insights=[ImageInsight(index=1, summary="只有这张识别了")])
    out = render_image_spec(spec)
    assert "成功识别 1 张" in out


def test_render_skips_insights_without_content():
    spec = ImageSpec(insights=[ImageInsight(index=1)])
    assert render_image_spec(spec) == ""


# ---------- 序列化 ----------

def test_json_roundtrip(tmp_path):
    spec = ImageSpec(
        refs=[ImageRef(index=1, alt="a", target="https://x", section="s", match="alt")],
        insights=[ImageInsight(index=1, kind="ui-mockup", summary="s", texts=["t"])],
        warnings=["w"])
    assert load_spec(to_json(spec)) == spec


def test_json_is_deterministic():
    spec = ImageSpec(refs=[ImageRef(index=1, alt="a", target="u")])
    assert to_json(spec) == to_json(spec)


# ---------- 集成 ----------

def test_stage_1_user_includes_image_context():
    from prd2tc import stages
    with_ctx = stages.build_stage_1_user("PRD 正文", image_context="## PRD 图片证据\n- 图1：三页签")
    without = stages.build_stage_1_user("PRD 正文")
    assert "PRD 图片证据" in with_ctx and "三页签" in with_ctx
    assert "PRD 图片证据" not in without
    assert "属推断" in with_ctx


def test_stage_3_system_includes_image_context():
    from prd2tc import stages
    ctx = "## PRD 图片证据（**来自图片的视觉识别**）\n- 图1：三页签"
    assert "图1：三页签" in stages.stage_3_system("方案", None, image_context=ctx)
    assert "图1：三页签" not in stages.stage_3_system("方案", None)


def test_pipeline_loads_images_and_writes_spec(tmp_path):
    """流水线从 bundle.assets 取图（不再依赖 --images-dir 扫盘）。"""
    from prd2tc import loader
    from prd2tc.pipeline import PipelineRunner

    prd = tmp_path / "prd.md"
    prd.write_text(PRD, encoding="utf-8")
    d = tmp_path / "assets"
    _write_images(d, ["智能体配置整体.jpeg", "添加文档知识弹窗.png", "rag接口清单.jpeg"])
    bundle = loader.load_bundle([prd, d])

    class Req:
        fetch_images = False
        stub = True
    runner = PipelineRunner(Config("k", "https://x", "m", 0.4, 10, 1))
    out = tmp_path / "out"
    out.mkdir()
    spec = runner._load_images(bundle, out, request=Req(), llm=None, reporter=None)
    assert spec is not None and len(spec.refs) == 3
    # 三条引用都应按文件名/图注关联到资产（match=alt/filename，均来自内存资产）
    assert all(r.resolved for r in spec.refs), [r.resolved for r in spec.refs]
    assert all(r.match in {"alt", "filename"} for r in spec.refs), [r.match for r in spec.refs]
    saved = json.loads((out / "analysis" / "image-spec.json").read_text(encoding="utf-8"))
    assert saved["summary"]["resolvedCount"] == 3
    assert saved["summary"]["analyzedCount"] == 0


def test_pipeline_image_failure_is_non_fatal(tmp_path):
    """bundle 里没有图片时直接返回 None，不得报错。"""
    from prd2tc import loader
    from prd2tc.pipeline import PipelineRunner

    prd = tmp_path / "prd.md"
    prd.write_text("# 无图\n", encoding="utf-8")
    bundle = loader.load_bundle([prd])

    class Req:
        fetch_images = False
        stub = True
    runner = PipelineRunner(Config("k", "https://x", "m", 0.4, 10, 1))
    out = tmp_path / "out"
    out.mkdir()
    assert runner._load_images(bundle, out, request=Req(), llm=None, reporter=None) is None
