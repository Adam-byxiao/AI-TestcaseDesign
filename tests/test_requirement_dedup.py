"""需求语义去重测试。

背景：文本指纹（`diff.fingerprint`）对**同义改写**无效 —— 标题措辞一变哈希就完全不同。
实测同一份资料被切成多个块后，模型为每块换了措辞与模块码，
于是产出 `DOCINGEST` 与 `INGEST` 两个模块、内容几乎逐条对应
（该次运行：319 条需求 / 43 个模块，其中 17 个模块纯由同一附件产生）。

同时必须防止**误并**（数据丢失比重复更危险）：实测曾把
`rerank_score > 0.2` 与 `> 0.39` 合并（数值冲突是两条不同规则）、
把不同接口的必填字段合并、把「场景筛选」这类极短标题并入无关长标题。
"""
import pytest

from prd2tc.stages import (DUP_CONTAINMENT, DUP_JACCARD, MIN_SHARED, MIN_TOKENS,
                           _dedupe_semantic, _numbers, _semantic_tokens, _similarity)


def _req(model_id, module, title, *, explicit=False):
    return {"model_id": model_id, "module": module, "title": title,
            "description": title, "acceptanceCriteria": [], "exceptionFlows": [],
            "priority": "P1", "sourceRefs": [], "explicit": explicit}


# ---------- token / 相似度 ----------

def test_semantic_tokens_strips_noise_but_keeps_words():
    """剥离代码标记与括号补充语，保留 CJK 2-gram 与英文词。"""
    a = _semantic_tokens("远程文件下载解析入库（异步）")
    b = _semantic_tokens("远程文件下载解析入库")
    assert a == b, (a, b)
    t = _semantic_tokens("`POST /v1/rag/context` 支持覆盖")
    assert "post" in t and "context" in t and "覆盖" in t


def test_containment_catches_one_sided_rewrite():
    """包含度对「一条是另一条的加长版」更敏感 —— Jaccard 会漏掉这类。"""
    a = _semantic_tokens("远程文件下载解析入库")
    b = _semantic_tokens("远程文件异步下载解析入库")
    jac, cont = _similarity(a, b)
    assert jac < DUP_JACCARD, f"Jaccard={jac} 本应不足以判定"
    assert cont >= DUP_CONTAINMENT, f"containment={cont} 应足够判定"


def test_numbers_extracted_for_conflict_detection():
    assert _numbers("`rerank_score` 默认过滤 > 0.2") == {"0.2"}
    assert _numbers("Filter rerank_score > 0.39") == {"0.39"}
    assert _numbers("无数字") == set()


# ---------- 合并（正向） ----------

def test_merges_identical_titles_across_modules():
    """跨模块的同一需求应合并，并把来源模块记入合并痕迹。"""
    reqs = [_req("R1", "TEST", "文字测试默认收起为右侧窄栏"),
            _req("R2", "AGENT", "文字测试默认收起为右侧窄栏")]
    warns: list[str] = []
    kept = _dedupe_semantic(reqs, warns=warns)
    assert len(kept) == 1, [r["title"] for r in kept]
    merged = kept[0].get("mergedFrom") or []
    assert merged and merged[0]["module"] == "AGENT", merged
    assert warns and "语义去重" in warns[0]


def test_merges_synonym_rewrites():
    """同义改写（换语序/加限定）应合并。"""
    reqs = [_req("R1", "INGEST", "远程文件异步下载解析入库"),
            _req("R2", "DOCINGEST", "远程文件下载解析入库（异步）"),
            _req("R3", "INGEST", "产品版本矩阵 xlsx 按 A1 模板识别"),
            _req("R4", "DOCINGEST", "产品版本矩阵 xlsx 仅按 A1 模板识别")]
    kept = _dedupe_semantic(reqs)
    assert len(kept) == 2, [r["title"] for r in kept]


def test_keeps_longer_title_on_merge():
    """合并时保留信息量更大（更长）的标题。"""
    reqs = [_req("R1", "A", "远程文件下载解析入库"),
            _req("R2", "B", "远程文件异步下载解析入库并回调")]
    kept = _dedupe_semantic(reqs)
    assert len(kept) == 1
    assert kept[0]["title"] == "远程文件异步下载解析入库并回调"


def test_merges_source_refs():
    """合并后应累积 sourceRefs，避免丢失可追溯性。"""
    a = _req("R1", "A", "文字测试默认收起为右侧窄栏")
    a["sourceRefs"] = [{"chunkId": "c1", "start": 0, "end": 10}]
    b = _req("R2", "B", "文字测试默认收起为右侧窄栏")
    b["sourceRefs"] = [{"chunkId": "c2", "start": 20, "end": 30}]
    kept = _dedupe_semantic([a, b])
    refs = {r["chunkId"] for r in kept[0]["sourceRefs"]}
    assert refs == {"c1", "c2"}, kept[0]["sourceRefs"]


# ---------- 误并防护（负向，最重要） ----------

def test_never_merges_numeric_conflicts():
    """**核心回归**：数值不同绝不合并 —— 阈值/上限是事实，不是措辞。

    实测误并：`rerank_score default filter > 0.2` 与 `Filter rerank_score > 0.39`
    被当成重复，从而丢掉了其中一条真实规则。

    用例刻意构造成**除数值外完全相同**，使相似度远高于阈值 ——
    否则会被相似度护栏先拦下，压根测不到数值检查这一层
    （变异测试曾因此漏报）。
    """
    base = "`rerank_score` 相似度阈值过滤条件"
    reqs = [_req("R1", "RAG", f"{base} > 0.2"),
            _req("R2", "RETRIEVE", f"{base} > 0.39")]
    # 前置条件：除数值外应当高度相似（否则本用例没测到目标逻辑）
    a, b = (_semantic_tokens(reqs[0]["title"]), _semantic_tokens(reqs[1]["title"]))
    jac, cont = _similarity(a, b)
    assert jac >= DUP_JACCARD or cont >= DUP_CONTAINMENT, (jac, cont)
    kept = _dedupe_semantic(reqs)
    assert len(kept) == 2, [r["title"] for r in kept]


def test_numeric_conflict_with_identical_text_differs_only_by_number():
    """数值不同但其余逐字相同 —— 这是数值检查唯一能生效的场景。"""
    reqs = [_req("R1", "A", "默认过滤阈值 rerank_score 大于 0.2"),
            _req("R2", "B", "默认过滤阈值 rerank_score 大于 0.39")]
    assert len(_dedupe_semantic(reqs)) == 2


def test_same_number_still_merges():
    """数值相同则不构成冲突，仍应按相似度合并（避免护栏过严）。"""
    reqs = [_req("R1", "A", "默认过滤阈值 rerank_score 大于 0.2"),
            _req("R2", "B", "默认过滤阈值 rerank_score 超过 0.2")]
    kept = _dedupe_semantic(reqs)
    assert len(kept) == 1, [r["title"] for r in kept]


def test_never_merges_tiny_generic_title_into_longer_one():
    """**核心回归**：极短标题不得因「被包含」而并入语义无关的长标题。

    实测误并：「场景筛选」（3 token）被并入「联系历史页支持日期/手机号/场景筛选」；
    「Toast 提示」（2 token）被并入「全局 #toast 顶部居中提示，z-index=99」。
    护栏是 MIN_TOKENS / MIN_SHARED。
    """
    cases = [
        ("场景筛选", "联系历史页支持日期/手机号/场景筛选"),
        ("Toast 提示", "全局 #toast 顶部居中提示，z-index=99"),
        ("`envName` 必填", "`envName`/`dialogFlowId`/`contents`/`callbackUrl` 必填"),
    ]
    for short, long in cases:
        kept = _dedupe_semantic([_req("R1", "A", long), _req("R2", "B", short)])
        assert len(kept) == 2, f"{short!r} 不应并入 {long!r}"


def test_field_list_requirements_stay_separate():
    """不同接口的必填字段要求不得合并（字段集合不同 = 不同契约）。"""
    reqs = [_req("R1", "KBWRITE", "`envName`/`dialogFlowId`/`contents`/`callbackUrl` 必填"),
            _req("R2", "BASERET", "`envName` 必填")]
    assert len(_dedupe_semantic(reqs)) == 2


def test_never_merges_explicit_requirements():
    """PRD 已写明编号的显式需求是权威事实，永不被合并。"""
    reqs = [_req("REQ-A-001", "A", "支持登录", explicit=True),
            _req("REQ-A-002", "A", "支持登录", explicit=True)]
    kept = _dedupe_semantic(reqs)
    assert len(kept) == 2, [r["model_id"] for r in kept]


def test_explicit_requirement_not_merged_into_generated():
    """生成需求也不得并入显式需求（否则显式项会被改写）。"""
    reqs = [_req("REQ-A-001", "A", "支持登录", explicit=True),
            _req("R2", "B", "支持登录")]
    kept = _dedupe_semantic(reqs)
    assert len(kept) == 2


def test_distinct_requirements_survive():
    """语义无关的需求必须全部保留（不得因阈值过松被误并）。"""
    reqs = [_req("R1", "A", "首次进入默认展示主提示词页签"),
            _req("R2", "A", "新增\"保存全部草稿\"按钮"),
            _req("R3", "B", "文档名称必填，最多20字并展示计数"),
            _req("R4", "B", "支持txt/docx/xlsx，单文件≤1MB"),
            _req("R5", "C", "无召回结果时明确展示\"未命中\"")]
    assert len(_dedupe_semantic(reqs)) == 5


def test_dedup_is_deterministic():
    reqs = [_req("R1", "A", "支持登录"), _req("R2", "B", "支持登录")]
    a = [r["model_id"] for r in _dedupe_semantic([dict(r) for r in reqs])]
    b = [r["model_id"] for r in _dedupe_semantic([dict(r) for r in reqs])]
    assert a == b


def test_dedup_no_warning_when_nothing_merged():
    warns: list[str] = []
    _dedupe_semantic([_req("R1", "A", "支持登录"), _req("R2", "B", "支持注册")], warns=warns)
    assert warns == []


# ---------- 接线：requirements_from_analysis 必须真的调用去重 ----------

def test_requirements_from_analysis_applies_semantic_dedup():
    """**接线回归**：`requirements_from_analysis` 必须调用语义去重。

    为什么需要这条：`_dedupe_semantic` 已有充分的单元测试，
    但「它是否被调用」是独立的一行 —— 把那行删掉后单元测试仍全绿
    （变异测试因此漏报过一次）。这里从**报告文本**端到端验证：
    同一需求以不同措辞出现两次，产出应只剩一条。
    """
    from prd2tc.stages import requirements_from_analysis

    report = (
        "# 需求分析\n\n"
        "| 编号 | 优先级 | 标题 | 验收标准 | 异常流 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| REQ-DOC-001 | P1 | 远程文件下载解析入库（异步） | 异步处理 | 回调失败 |\n"
        "| REQ-ING-001 | P1 | 远程文件异步下载解析入库 | 异步处理 | 回调失败 |\n"
        "| REQ-DOC-002 | P1 | 支持扩展名 .txt/.md/.csv | 全部可解析 | 不支持报错 |\n"
    )
    prd = "# PRD\n\nDOC-001 远程文件下载解析入库（异步）\n"
    warns: list[str] = []
    reqs = requirements_from_analysis(report, prd, warnings=warns)
    titles = [r["title"] for r in reqs]
    assert len(reqs) == 2, titles
    assert any("语义去重" in w for w in warns), warns


def test_requirements_from_analysis_keeps_explicit_distinct():
    """显式编号（PRD 中存在）的需求不得被去重合并。"""
    from prd2tc.stages import requirements_from_analysis

    report = (
        "# 分析\n\n"
        "| 编号 | 优先级 | 标题 | 验收标准 | 异常流 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| REQ-A-001 | P1 | 支持登录 | 成功 | 失败 |\n"
        "| REQ-A-002 | P1 | 支持登录 | 成功 | 失败 |\n"
    )
    prd = "# PRD\n\nREQ-A-001 支持登录\nREQ-A-002 支持登录\n"
    reqs = requirements_from_analysis(report, prd)
    assert len(reqs) == 2, [r["id"] for r in reqs]
