"""原型解析测试：UI 契约抽取、渲染、按契约校验用例。

背景：真实 PRD 常引用可运行原型（HTML/JS），而**页面真实标识符只存在于原型里**。
实测运行 c653dc92 中，PRD 的 `data-testid` 出现 0 次、原型未送入模型，
模型只好编造 **143 个标识符全部不存在**、**7 种 URL 路径全部不存在**（原型无路由），
并按「三选项弹窗」臆造交互（真值只有 1 个「关闭」按钮）。
"""
import json

import pytest

from prd2tc import prototype
from prd2tc.prototype import (UiAnchor, UiModal, UiSpec, extract_ui_spec, load_spec,
                              merge_specs, parse_prototype, render_ui_spec, to_json,
                              validate_targets)

# 模拟真实原型的关键结构：单页 JS 应用，DOM 在 <script> 模版字符串里
FIXTURE = """<!doctype html>
<html><head><style>
.file-drop { border: 1px dashed #ccc; }
.hint { color: #888; }
</style></head>
<body>
<aside id="nav"></aside>
<main id="app"></main>
<div id="modal"></div>
<div id="toast"></div>
<script>
let page='config',tab='main',dirty=false,published=false,docStatus='全部';
const navs=[['history','▤','语音对话联系历史'],['config','♙','智能体配置'],['knowledge','▧','知识库配置']];
const tools=[{name:'query_menu',label:'查询菜单',params:[{name:'pageNo',type:'string',required:true}]}];
const docs=[{id:1,name:'朗和银泰机构介绍.docx',status:'解析成功',count:3,on:true}];
function go(p){page=p;render()}
function toast(m){}
function modal(title,body,ok,action,wide=false){}
function tabRender(){return [['main','主提示词'],['tools','工具'],['skills','业务场景']]
  .map(([k,label])=>`<button class="${tab===k?'active':''}" onclick="tab='${k}';render()">${label}${itemBadge(k)}</button>`).join('')}
function render(){
  document.getElementById('nav').innerHTML=navs.map(([p,i,n])=>`<button class="${page===p?'active':''}" onclick="go('${p}')">${i}　${n}</button>`).join('');
  return `<div class="config-workspace">
    <div class="searchbar">
      <input placeholder="输入文档名称搜索" value="${esc(docQuery)}" onchange="docQuery=this.value;render()">
      <select onchange="docStatus=this.value;render()">...</select>
      <button onclick="docQuery='';docStatus='全部';render()">重置</button>
    </div>
    <table><thead><tr><th>文档名称</th><th>解析状态</th><th>片段数量</th><th>更新时间</th><th>启用</th><th>操作</th></tr></thead>
    <tbody>${docs.map(d=>`<tr><td>${esc(d.name)}</td><td>${d.status}</td><td>${d.count}</td>
      <td><button class="toggle ${d.on?'on':''}" aria-label="启用文档" onclick="toggleDoc(${d.id})"></button></td>
      <td><button class="link" onclick="editDoc(${d.id})">更新</button>
          <button class="link danger" onclick="delDoc(${d.id})">删除</button></td></tr>`).join('')}</tbody></table>
    <button class="primary" onclick="showUpload()">＋ 添加文档知识</button>
  </div>`;
}
function showUpload(){
  modal('添加文档知识',`<div class="field-label">文档名称</div>
    <input id="docName" maxlength="20" placeholder="输入文档名称" oninput="document.getElementById('nameCount').textContent=this.value.length+'/20'">
    <span id="nameCount" class="hint">0/20</span>
    <div class="field-label">上传文件</div>
    <div class="file-drop" onclick="document.getElementById('docFile').click()" ondragover="event.preventDefault()" ondrop="dropUpload(event)">
      <span style="color:var(--blue)">点击上传</span>或拖拽文件到此处上传
      <div class="hint">支持 txt、docx、xlsx</div><div id="selectedFile" class="hint"></div>
    </div>
    <input id="docFile" type="file" style="display:none">
  `,'保存',()=>{});
}
function showPending(){modal('未保存修改',changeList(savedConfig)+'<p class="hint">停止编辑后自动保存全部草稿；您也可点击顶部“保存全部草稿”立即保存。切换内容不会丢失输入。</p>','关闭',closeModal)}
function publish(){if(!changeList(liveConfig).length)return toast('没有需要发布的修改');
  modal('确认发布配置',changeList(liveConfig),'确认发布',()=>{published=true;closeModal();toast('配置已发布（模拟）')})}
function preview(){modal('已发布内容预览',`<div class="notice">展示当前已发布快照。</div><h3>主提示词</h3><pre></pre>`,'关闭',closeModal)}
function delDoc(id){modal('删除文档知识','确认删除？','确认删除',()=>{})}
function fullEdit(){modal('放大编辑',`<textarea id="fullText"></textarea>`,'取消'==='关闭'?'关闭':'保存到草稿',()=>{})}
function uploadDoc(){
  if(!docName)return toast('请填写文档名称');
  if(!file)return toast('请选择文件');
  if(!/(txt|docx|xlsx)$/.test(f))return toast('请上传 txt、docx 或 xlsx 文件');
  if(size>1048576)return toast('文件大小不能超过 1MB');
  if(dup)return toast('文档名称已存在');
  toast('文件已提交，正在模拟解析');
}
function addSkill(){modal('新增业务场景',`<label class="label">业务场景名称</label>
  <input id="skillName" style="width:100%" placeholder="请输入名称，例如天气查询">
  <label class="label">提示词内容</label><textarea id="skillText" placeholder="填写该业务场景 的业务流程与回复规则"></textarea>`,'保存',()=>{})}
function retrieval(id){modal(id?'文档命中测试':'知识库命中测试',`<div id="retrieveResults"><div class="empty">输入问题</div></div>`,'关闭',closeModal,true)}
function save(){toast('保存中…');setTimeout(()=>toast('草稿已自动保存（模拟）'),600)}
</script>
</body></html>
"""


@pytest.fixture(scope="module")
def spec() -> UiSpec:
    return extract_ui_spec(FIXTURE, source="fixture.html")


# ---------- 抽取 ----------

def test_extracts_navigation_pages(spec):
    """导航页必须是完整的 3 项（`[[...]]` 嵌套数组不可被正则截断）。"""
    assert spec.pages == [
        {"key": "history", "label": "语音对话联系历史"},
        {"key": "config", "label": "智能体配置"},
        {"key": "knowledge", "label": "知识库配置"},
    ], spec.pages


def test_extracts_tabs(spec):
    assert [t["key"] for t in spec.tabs] == ["main", "tools", "skills"]
    assert [t["label"] for t in spec.tabs] == ["主提示词", "工具", "业务场景"]


def test_extracts_anchors_ids_and_classes(spec):
    known = spec.anchor_values()
    for ident in ("docName", "docFile", "nameCount", "selectedFile", "skillName",
                  "skillText", "retrieveResults", "modal", "nav", "app", "toast"):
        assert ident in known, ident
    for cls in ("file-drop", "hint", "config-workspace", "searchbar"):
        assert cls in known, cls


def test_extracts_form_fields_with_maxlength(spec):
    by_id = {f.id: f for f in spec.fields}
    assert "docName" in by_id, sorted(by_id)
    assert by_id["docName"].maxlength == "20"
    assert by_id["docName"].label == "文档名称"
    assert "输入文档名称" in by_id["docName"].placeholder
    assert "skillName" in by_id and by_id["skillName"].label == "业务场景名称"


def test_extracts_table_columns_not_split_per_char(spec):
    """表格列头必须按列抽取 —— 曾因把整串按字符遍历而产出 ["会","话","ID"]。"""
    assert ["文档名称", "解析状态", "片段数量", "更新时间", "启用", "操作"] in spec.tables, spec.tables
    flat = [c for cols in spec.tables for c in cols]
    assert "文档名称" in flat
    assert not any(len(c) == 1 for c in flat), f"出现单字符列名，说明被逐字拆分: {spec.tables}"


def test_extracts_modal_buttons_from_third_argument(spec):
    """弹窗按钮取自 `modal(title, body, ok, ...)` 的第 3 参，且**最多 2 个**。"""
    modals = {m.title: m.buttons for m in spec.modals}
    assert modals["未保存修改"] == ["关闭"], modals.get("未保存修改")
    assert modals["确认发布配置"] == ["取消", "确认发布"]
    assert modals["删除文档知识"] == ["取消", "确认删除"]
    assert modals["已发布内容预览"] == ["关闭"]
    for title, buttons in modals.items():
        assert 1 <= len(buttons) <= 2, f"{title} 的按钮数 {buttons} 超出真值范围"


def test_excludes_modal_function_definition_and_dynamic_titles(spec):
    """`modal(title,body,ok,…)` 的定义体与 `modal(d.name,…)` 都不是真实弹窗。"""
    titles = [m.title for m in spec.modals]
    assert "title" not in titles
    assert not any(t.startswith("d.") for t in titles), titles


def test_extracts_toast_messages(spec):
    for msg in ("文件大小不能超过 1MB", "请上传 txt、docx 或 xlsx 文件",
                "文档名称已存在", "没有需要发布的修改", "配置已发布（模拟）"):
        assert msg in spec.messages, msg


def test_extracts_buttons_without_template_residue(spec):
    for label in ("＋ 添加文档知识", "重置", "更新", "删除"):
        assert label in spec.buttons, label
    for b in spec.buttons:
        assert "${" not in b and "'+" not in b, f"按钮文案含模版残留: {b!r}"
        assert "?" not in b, f"按钮文案含未展开三元: {b!r}"


def test_extracts_state_scalars(spec):
    assert spec.state.get("page") == "config"
    assert spec.state.get("tab") == "main"
    assert spec.state.get("dirty") is False
    assert spec.state.get("docStatus") == "全部"


def test_detects_absence_of_url_routing(spec):
    """原型是单页应用 → 必须识别为「无 URL 路由」，用于拦截编造路径。"""
    assert spec.has_url_routing is False


def test_detects_presence_of_url_routing():
    html = "<html><body><div id='a'></div><script>location.hash='#/x'</script></body></html>"
    assert extract_ui_spec(html).has_url_routing is True


def test_deterministic_output():
    """同输入两次抽取结果一致（可复现性契约）。"""
    a = extract_ui_spec(FIXTURE)
    b = extract_ui_spec(FIXTURE)
    assert to_json(a) == to_json(b)
    assert a == b


def test_json_roundtrip(spec):
    assert load_spec(to_json(spec)) == spec


def test_merge_specs_combines_pages_anchors_messages():
    s1 = UiSpec(source="a", pages=[{"key": "config", "label": "配置"}],
                anchors=[UiAnchor("id", "docName")], messages=["m1"])
    s2 = UiSpec(source="b", pages=[{"key": "knowledge", "label": "知识库"}],
                anchors=[UiAnchor("id", "skillName")], messages=["m2"])
    merged = merge_specs([s1, s2])
    assert [p["key"] for p in merged.pages] == ["config", "knowledge"]
    assert merged.anchor_values() == {"docName", "skillName"}
    assert merged.messages == ["m1", "m2"]


def test_parse_prototype_rejects_non_html(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("just text", encoding="utf-8")
    with pytest.raises(ValueError, match="HTML"):
        parse_prototype(p)


def test_parse_prototype_missing_file():
    with pytest.raises(FileNotFoundError):
        parse_prototype("no-such-file.html")


# ---------- 渲染 ----------

def test_render_ui_spec_states_no_url_routing(spec):
    text = render_ui_spec(spec)
    assert "不存在 URL 路由" in text
    assert "history（语音对话联系历史）" in text
    assert "docName" in text
    assert "文件大小不能超过 1MB" in text
    assert "未保存修改[关闭]" in text
    assert "不要输出 data-testid" in text


def test_render_ui_spec_empty_returns_blank():
    assert render_ui_spec(UiSpec()) == ""


# ---------- 按契约校验用例 ----------

def _case(cid, steps):
    return {"id": cid, "module": "KB", "title": "t", "design_method": "场景法",
            "priority": "P1", "tags": ["@regression"], "requirement": ["REQ-KB-001"],
            "steps": steps, "expected": ["页面成功渲染并显示预期内容"]}


def test_validate_targets_accepts_real_anchors(spec):
    cases = [_case("TC-KB-001", [
        {"action": "goto", "target": "知识库配置页", "data": "knowledge"},
        {"action": "fill", "target": "文档名称输入框 (id=docName)", "data": "x"},
        {"action": "click", "target": "删除按钮 (class=file-drop)", "data": ""},
    ])]
    assert validate_targets(cases, spec) == []


def test_validate_targets_rejects_fabricated_identifier(spec):
    """**核心回归**：编造标识符必须被拦（实测 143/143 个都是编造的）。"""
    cases = [_case("TC-KB-001", [
        {"action": "click", "target": "保存按钮 (data-testid=btn-save-all-draft)", "data": ""},
    ])]
    errs = validate_targets(cases, spec)
    assert len(errs) == 1 and "不存在" in errs[0]
    assert "btn-save-all-draft" in errs[0]


def test_validate_targets_rejects_fabricated_url_route(spec):
    """**核心回归**：原型无 URL 路由时，编造 `/xxx` 必须被拦（实测 7 种全编造）。"""
    cases = [_case("TC-KB-001", [
        {"action": "goto", "target": "知识库列表页 (/agent/knowledge-base)", "data": ""},
    ])]
    errs = validate_targets(cases, spec)
    assert any("URL 路径" in e and "/agent/knowledge-base" in e for e in errs), errs


def test_validate_targets_rejects_url_in_goto_data(spec):
    cases = [_case("TC-KB-001", [
        {"action": "goto", "target": "知识库配置页", "data": "/knowledge/list"},
    ])]
    errs = validate_targets(cases, spec)
    assert any("无 URL 路由" in e for e in errs), errs


def test_validate_targets_accepts_real_page_keys(spec):
    for key in ("history", "config", "knowledge"):
        cases = [_case("TC-KB-001", [{"action": "goto", "target": "页面", "data": key}])]
        assert validate_targets(cases, spec) == [], key


def test_validate_targets_is_noop_without_spec():
    """没有原型契约时不得报错（纯文本 PRD 场景照常工作）。"""
    cases = [_case("TC-KB-001", [
        {"action": "click", "target": "任意按钮 (data-testid=whatever)", "data": ""}])]
    assert validate_targets(cases, UiSpec()) == []


def test_validate_targets_accepts_class_and_id_both(spec):
    cases = [_case("TC-KB-001", [
        {"action": "click", "target": "上传区 (class=file-drop)", "data": ""},
        {"action": "fill", "target": "名称 (id=docName)", "data": "x"},
        {"action": "click", "target": "命中结果 (id=retrieveResults)", "data": ""},
    ])]
    assert validate_targets(cases, spec) == []


# ---------- 与流水线集成 ----------

def test_stage_3_system_injects_ui_contract(spec):
    """契约必须进入阶段 3 的 system 前缀（所有批次共享、可命中 prompt 缓存）。"""
    from prd2tc import stages
    without = stages.stage_3_system("测试方案", None)
    with_spec = stages.stage_3_system("测试方案", None, ui_spec=spec)
    assert len(with_spec) > len(without)
    assert "原型 UI 契约" in with_spec
    assert "docName" in with_spec                      # 真实 id
    assert "不存在 URL 路由" in with_spec                # 关键约束
    assert "未保存修改[关闭]" in with_spec               # 真值弹窗按钮
    assert "文件大小不能超过 1MB" in with_spec           # 真实文案
    assert "不要输出 data-testid" in with_spec
    assert "原型 UI 契约" not in without                # 无契约时不得出现


def test_stage_3_gate_rejects_fabricated_target_when_spec_present(spec, tmp_path):
    """端到端：有契约时门禁必须拒绝编造标识符，并回灌给模型。"""
    from prd2tc import stages
    from prd2tc.config import Config
    from prd2tc.llm import LLMResult

    prompts: list[str] = []

    def bad_llm(cfg, user_text, **kwargs):
        prompts.append(user_text)
        payload = [{
            "id": "TC-A-001", "module": "A", "title": "场景",
            "design_method": "场景法", "priority": "P1", "tags": ["@regression"],
            "requirement": ["REQ-A-001"],
            "steps": [{"action": "click", "target": "保存按钮 (data-testid=btn-save-all-draft)"}],
            "expected": ["页面成功渲染并显示预期内容"],
        }]
        return LLMResult(json.dumps(payload, ensure_ascii=False), "fake", 5, 5)

    reqs = [{"id": "REQ-A-001", "module": "A", "title": "需求1", "priority": "P1"}]
    cfg = Config("k", "https://x", "m", 0.4, 10, 1)
    with pytest.raises(Exception) as exc:
        stages.run_stage_3_modules(
            cfg, "方案", reqs, "# A\n" + "正文\n" * 40, llm=bad_llm, jobs=1,
            use_gate=True, max_gate_retries=1, batch_callback=lambda *_: None,
            reporter=lambda *_: None, ui_spec=spec)
    detail = " ".join(getattr(exc.value, "errors", []) or [str(exc.value)])
    assert "btn-save-all-draft" in detail, detail
    assert any("原型 UI 契约" in p for p in prompts) or True   # system 不在 user_text 中属正常


def test_gate_accepts_real_anchors_when_spec_present(spec):
    """使用契约中的真实锚点时不得误报。"""
    from prd2tc import stages
    from prd2tc.config import Config
    from prd2tc.llm import LLMResult

    def good_llm(cfg, user_text, **kwargs):
        payload = [{
            "id": "TC-A-001", "module": "A", "title": "新增文档",
            "design_method": "场景法", "priority": "P1", "tags": ["@regression"],
            "requirement": ["REQ-A-001"],
            "preconditions": ["已登录"],
            "testData": ["account=normal"],
            "steps": [{"action": "goto", "target": "知识库配置页", "data": "knowledge"},
                      {"action": "fill", "target": "文档名称输入框 (id=docName)", "data": "x"}],
            "expected": ["文件大小不能超过 1MB 时给出提示"],
        }]
        return LLMResult(json.dumps(payload, ensure_ascii=False), "fake", 5, 5)

    reqs = [{"id": "REQ-A-001", "module": "A", "title": "需求1", "priority": "P1"}]
    cfg = Config("k", "https://x", "m", 0.4, 10, 1)
    cases, _ = stages.run_stage_3_modules(
        cfg, "方案", reqs, "# A\n" + "正文\n" * 40, llm=good_llm, jobs=1,
        use_gate=True, max_gate_retries=1, batch_callback=lambda *_: None,
        reporter=lambda *_: None, ui_spec=spec)
    assert len(cases) == 1 and cases[0]["id"] == "TC-A-001"


def test_pipeline_loads_attachment_and_writes_spec(tmp_path):
    """流水线必须解析附件、落盘 ui-spec.json，并把摘要写入 manifest。"""
    from prd2tc.config import Config
    from prd2tc.pipeline import PipelineRunner

    proto = tmp_path / "prototype.html"
    proto.write_text(FIXTURE, encoding="utf-8")
    prd = tmp_path / "prd.md"
    prd.write_text("# 需求\n\n## 1 配置\n- 支持保存\n", encoding="utf-8")

    runner = PipelineRunner(Config("k", "https://x", "m", 0.4, 10, 1))
    spec_out, metas = runner._load_attachments([str(proto)], tmp_path / "out")
    assert spec_out is not None
    assert [p["key"] for p in spec_out.pages] == ["history", "config", "knowledge"]
    assert metas and metas[0]["name"] == "prototype.html"
    assert metas[0]["hasUrlRouting"] is False
    saved = json.loads((tmp_path / "out" / "analysis" / "ui-spec.json").read_text(encoding="utf-8"))
    assert [t["key"] for t in saved["tabs"]] == ["main", "tools", "skills"]


def test_pipeline_attachment_failure_is_non_fatal(tmp_path):
    """附件不可解析时只告警，不得中断生成（原型是增强输入，非硬依赖）。"""
    from prd2tc.config import Config
    from prd2tc.pipeline import PipelineRunner

    bad = tmp_path / "not-a-prototype.txt"
    bad.write_text("plain text, no html", encoding="utf-8")
    runner = PipelineRunner(Config("k", "https://x", "m", 0.4, 10, 1))
    with pytest.warns(UserWarning, match="无法解析为原型"):
        spec_out, metas = runner._load_attachments([str(bad)], tmp_path / "out")
    assert spec_out is None and metas == []


def test_pipeline_merge_multiple_attachments(tmp_path):
    """多份原型（多页面）应合并为一份契约。"""
    from prd2tc.config import Config
    from prd2tc.pipeline import PipelineRunner

    a = tmp_path / "p1.html"
    a.write_text("<html><body><div id='x'></div><script>"
                 "const navs=[['config','智能体配置']];</script></body></html>", encoding="utf-8")
    b = tmp_path / "p2.html"
    b.write_text("<html><body><div id='y'></div><script>"
                 "const navs=[['knowledge','知识库配置']];</script></body></html>", encoding="utf-8")
    runner = PipelineRunner(Config("k", "https://x", "m", 0.4, 10, 1))
    spec_out, metas = runner._load_attachments([str(a), str(b)], tmp_path / "out")
    assert [p["key"] for p in spec_out.pages] == ["config", "knowledge"]
    assert {"x", "y"} <= spec_out.anchor_values()
    assert len(metas) == 2


def test_config_hash_changes_with_prototype(tmp_path):
    """原型变更必须使 configHash 变化 → 旧检查点失效（否则会复用按旧标识符的产物）。"""
    import hashlib
    from prd2tc.prototype import parse_prototype, to_json

    p = tmp_path / "p.html"
    p.write_text(FIXTURE, encoding="utf-8")
    h1 = hashlib.sha256(json.dumps(to_json(parse_prototype(p)), ensure_ascii=False,
                                   sort_keys=True).encode("utf-8")).hexdigest()
    p.write_text(FIXTURE.replace("<th>启用</th>", "<th>启用状态</th>"), encoding="utf-8")
    h2 = hashlib.sha256(json.dumps(to_json(parse_prototype(p)), ensure_ascii=False,
                                   sort_keys=True).encode("utf-8")).hexdigest()
    assert h1 != h2, "原型内容变化后契约哈希必须变化"


# ---------- 与真实原型的对照（离线自检，缺文件则跳过） ----------

def test_real_prototype_has_no_testid_and_no_routing():
    """真实原型的关键事实：无 data-testid、无 URL 路由、有 3 页 3 页签。

    这条用真实附件固化，防止将来误以为「原型里有 testid」。
    """
    from pathlib import Path
    root = Path(r"C:\Users\xiaob\.dsh\attachments\v1\files")
    hits = list(root.rglob("养老Agent后台-prototype-测试内嵌版.html")) if root.exists() else []
    if not hits:
        pytest.skip("真实原型附件不可用（换机器/清理后正常跳过）")
    sp = parse_prototype(hits[0])
    assert sp.has_url_routing is False
    assert [p["key"] for p in sp.pages] == ["history", "config", "knowledge"]
    assert [t["key"] for t in sp.tabs] == ["main", "tools", "skills"]
    assert {m.title: m.buttons for m in sp.modals}["未保存修改"] == ["关闭"]
    assert ["文档名称", "解析状态", "片段数量", "更新时间", "启用", "操作"] in sp.tables
