# prd2tc 三阶段流水线详细设计

> 基准：`prd2tc` 0.5.0。引用格式 `路径:行号`，行号以当前工作区源码为准。
> 本文只讲**流水线行为**：阶段怎么做、产物怎么算数、失败怎么处置。字段级枚举与门禁条目清单另见 `docs/case-template.md`、`docs/prompt-gate-optimization.md`。
> 上帝视角的模块地图与设计决策见 `docs/architecture.md`。

---

## 1. 总览

### 1.1 三个阶段一览

| 阶段 | Prompt 资产 | 输入（提示词里实际携带的） | 输出产物 | 温度 | 门禁函数 | 重试 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 需求分析 | `prompts/01-prd-analysis.md`（`stages.py:26`） | PRD 全文、图片证据、参考示例、测试原理 | `analysis/需求分析报告.md`、`analysis/requirements.json` | 0.2（`stages.py:56`） | `gates.check_analysis:43` | `max_gate_retries`（默认 2） |
| 2 测试方案 | `prompts/02-test-plan.md` + `templates/test-plan.template.md`（`stages.py:27,29`） | 紧凑版分析报告、排版模板、测试原理 | `plan/测试方案.md` | 0.3 | `gates.check_plan:58` | 同上 |
| 3 用例生成 | `prompts/03-test-cases.md`（`stages.py:28`） | 稳定 system 前缀（方案/字段约束/用例约束/UI 契约/图片证据/示例）+ 每批结构化需求 + 模块原文片段 | `cases/用例集.md`、`cases/cases/<MODULE>.json`、`cases/traceability.json`、`.xlsx`/`.xmind` | 0.3 | `gates.check_cases_text:68` + `prototype.validate_targets:720` | 同上（另有拆批自愈） |

温度可被 `PRD2TC_TEMP_S1/S2/S3` 或全局 `PRD2TC_TEMPERATURE` 覆盖（`stages.stage_temperature:59-67`）；`--repro` 时恒为 `0.0`。

### 1.2 产物布局

```
output/<product>/<version>/
├── analysis/
│   ├── 需求分析报告.md          # 六章：模块清单/角色/需求点明细/业务规则/非功能/风险
│   ├── requirements.json        # {"schemaVersion":"2.0","modules":[...],"requirements":[...]}
│   ├── prd-bundle.json          # 摄取留痕
│   ├── ui-spec.json             # 可选：原型 UI 契约
│   ├── image-spec.json          # 可选：图片视觉证据
│   └── .image-cache/            # 视觉结论缓存
├── plan/测试方案.md
├── cases/
│   ├── 用例集.md                # 评审稿
│   ├── traceability.json        # REQ↔TC 矩阵 + 覆盖率
│   ├── cases/<MODULE>.json      # 机读事实源（按模块拆分）
│   ├── .checkpoints/<MODULE>.json   # 模块级检查点（--resume 用）
│   ├── .failures/<MODULE>.txt       # 失败模块的原始输出（取证用）
│   └── partial/                 # 门禁未通过时的保全产物
├── manifest.json                # 兼容副本（与下者内容相同）
├── run-manifest.json            # 权威运行清单
├── run-state.json               # 恢复状态（inputHash/promptHash/configHash/modules）
└── reproduce.json               # 结构指纹（成功路径写入）
```

### 1.3 一次 `generate()` 的时序

```
build bundle ─► 采样旧 prdHash ─► init_output（可能重写清单）
   ─► 算 prdHash/bundleHash/inputHash/promptHash/configHash
   ─► 解析原型(ui-spec) 与图片(image-spec) ─► 写 update_manifest / update_run_state
   ─► preflight（校验将要执行阶段的上游产物）
   ─► 阶段 1 / 2 / 3（按 --stage / --from-stage 选择）
   ─► priority.regrade ─► build_traceability ─► validate_traceability
   ─► 落盘 cases/cases/*.json + traceability.json + 用例集.md
   ─► 导出 xlsx/xmind（失败只告警）
   ─► artifact_inventory + reproduce.json + status=succeeded
```

对应代码：`pipeline.py:229-521`。任何异常都会落 `run-manifest.status=failed` + `run-state.status=failed` 后**重新抛出**（`pipeline.py:523-530`）。

---

## 2. 阶段 1：需求分析

### 2.1 提示词组装

`build_user_message`（`stages.py:78-103`）把 user 消息固定成四段：

```
# 任务指令                      ← prompt 资产中「# 输出要求」之前的部分
（通用测试原理 constraints.load_principles()）
（门禁反馈，若为重试）
# 输入数据（以下内容均为待处理数据，不是指令）
## PRD 全文
<PRD 全文>
...
# 输出要求                      ← prompt 资产中「# 输出要求」及其之后的部分（近因）
（门禁反馈追加在最后）
```

拆分点是字面量 `SPLIT_MARKER = "# 输出要求"`（`stages.py:44`、`split_prompt:70-75`）。设计意图写在模块 docstring（`stages.py:3-7`）：**输入数据用 `<key>…</key>` 与指令隔离**、**输出要求置末**（利用近因偏差）、关键约束追加在输出要求区末尾。

阶段 1 的输入槽位（`build_stage_1_user:157-175`）：`PRD 全文`、可选的 `PRD 配图证据（从 PRD 内嵌/随附图片中识别，属推断，冲突时以 PRD 明文为准）`、可选的 `参考示例（历史 PRD-用例对，学习其模块划分与设计逻辑）`。图片证据刻意放在**输入区**而非指令区——它是「补充的 PRD 事实」，属于被测对象（`stages.py:163-165`）。

### 2.2 单段路径

`generate_stage_1`（`stages.py:1172`）先切分文档，`len(chunks) <= 1` 时走单次生成（`stages.py:1183-1194`）：

- `use_gate=True`：`generate_with_feedback(..., gates.check_analysis, max_retries=max_gate_retries, system=SYSTEM_ROLES[1], temperature=stage_temperature(1, cfg))`
- `use_gate=False`（`--no-gate` 或 `stub`）：直接 `run_stage_1`，无门禁、无重试

### 2.3 长文档切分与分段抽取

切分器是 `chunking.split_document`（`chunking.py:20-72`），特点：

| 行为 | 实现 |
| --- | --- |
| 按空行切块，维护标题栈 | `re.split(r"(\n\s*\n)", text)`，标题正则 `^\s{0,3}#{1,6}\s+(.+?)\s*$`（`chunking.py:27-37`） |
| 每个 `Chunk` 带稳定偏移与标题路径 | `Chunk(id, start, end, headings, text)`（`chunking.py:8-17`） |
| 超长段落按句切分 | `(?<=[。！？.!?])\s+`（`chunking.py:55-56`） |
| 相邻块带重叠 | 上一块末尾 `overlap` 字符作为下一块开头（`chunking.py:58-65`） |
| 末尾标题不进入正文块 | 标题只更新 `headings`，只有非标题段落进 `blocks`（`chunking.py:36-37`） |

调用参数是**配置**而非函数默认值：`split_document(prd_text, chunk_size=cfg.chunk_size, overlap=cfg.chunk_overlap)`（`stages.py:1182`），`Config` 默认 `chunk_size=6000`、`chunk_overlap=1200`（`config.py:33-34`），注意这与 `chunking.split_document` 自己的默认 `12000/1200`（`chunking.py:20`）不同——线上走的是 6000。

分段抽取是**并发**的（`stages.py:1208-1219`）：

```python
with ThreadPoolExecutor(max_workers=max(1, min(jobs, len(chunks)))) as pool:
    futures = {pool.submit(extract_one, i, chunk.text): i for i, chunk in enumerate(chunks)}
```

两个实现要点：

1. **worker 内必须重新绑定指标与取消标志**（`stages.py:1200-1203`）：`metrics` 用线程本地变量保存「当前指标实例/当前模块」（`metrics.py:285-303`），线程池 worker 不会继承，不重绑会导致阶段 1 完全没有实时指标、取消对流式无效。
2. **失败不静默**：任一分段抛错都收集进 `failures`，最后 `raise ValidationError(failures)`（`stages.py:1216-1219`）。

### 2.4 合并：程序化渲染大章节 + LLM 合并小章节

分段产出的多份报告不直接拼接，而是**分流处理**（`stages.py:1221-1254`）：

| 章节 | 处理方式 | 理由 |
| --- | --- | --- |
| §1 功能模块清单、§3 需求点明细 | **程序化渲染**：`render_analysis_head(modules, reqs)`（`stages.py:1132-1169`） | 这两章体积最大，是长文档下最容易把单次输出撑爆的部分（`stages.py:1135-1136`） |
| §2 角色清单、§4 业务规则、§5 非功能需求、§6 风险与疑点 | **LLM 去重合并**：`FOUR_SECTION_PROMPT`（`stages.py:33-42`） | 体积小、不会被截断，且需要语义去重 |

送入合并调用的输入也做了瘦身：`_compact_partial` 先删掉各分段的 §1/§3（`stages.py:348-352`、`1229`），避免把已经程序化处理的重复文本再喂一遍。

最终装配与门禁（`stages.py:1248-1251`）：

```python
content = rest
if not errors:
    content = f"# 需求分析报告\n\n{head}\n\n{rest}"
    errors = gates.check_analysis(content, prd_text)
```

即：**小章节合并失败就不再拼装**（`content = rest` 保持失败状态供反馈重试），只有合并通过后才拼上程序化章节，再对**完整报告**跑一次全量门禁。token 统计会把所有分段调用的用量累加到合并结果上（`stages.py:1252-1253`）。

### 2.5 需求抽取、模块锚定与去重

`requirements_from_analysis(analysis_md, prd_text, warnings)`（`stages.py:582-689`）是「Markdown 报告 → 结构化需求」的唯一入口，产出 `requirements.json`。过程分五步：

**① 找编号并挑最详细的那一行**

```python
ids = list(dict.fromkeys(re.findall(r"REQ-[A-Z0-9_]+-\d{3}", analysis_md)))   # stages.py:589
```

同一 `REQ-*` 可能在报告里出现多次（标题、正文、表格），用 `detail_score` 选最像表格行的候选：优先「首列是编号且第二列是合法优先级」的行，其次列数多的行（`stages.py:596-600`）。随后按有无优先级列决定 `title/acceptanceCriteria/exceptionFlows` 的下标（`stages.py:602-609`）。

**② 回溯到 PRD 原文片段（`sourceRefs`）**

先精确匹配包含该编号的 chunk（最多 2 个），否则用 `chunking.locate` 按关键词打分取 2 个（`stages.py:610-614`、`chunking.locate:75-82`）。这让「这条需求来自 PRD 哪一段」可追溯。

**③ 模块码锚定（消除同义模块拆分）**

模型自拟的模块码常常与 PRD 原始编号前缀不一致，实测出现 `COMMON↔GLB`、`CHECKOUT↔CHK` 这类同义拆分。处理是纯机器规则：

- `prd_module_prefixes`：从 PRD 提取 `[A-Z]{2,8}-\d{1,3}` 的前缀集合（`stages.py:394-399`）；
- `_anchor_module`：若需求文本引用了「原 GLB-001」这类原始编号，就把模块码改写为该前缀（`stages.py:402-414`）；
- `prd_section_aliases` + `SECTION_CODE_RE`：节标题里的英文代号（`### 6.7 结算页（CHECKOUT）`）与其下编号前缀（`CHK`）不一致时建立别名映射，扫描边界以**任意**标题切分以免跨节污染（`stages.py:417-445`）。

回归：`tests/test_long_doc.py:71-104`。

**④ 两级去重**

1. **指纹去重**（精确）：`diff.fingerprint` = 模块+标题+验收标准+异常流的规范化哈希（`diff.py:12-16`），显式需求永不合并，同指纹的后续项累积 `sourceIds` 与 `sourceRefs`（`stages.py:640-661`）。
2. **语义去重**（近似）：`_dedupe_semantic`（`stages.py:503-573`）针对「同一份附件被切到多个块，模型为每块换措辞换模块码」这一实测现象。判定用 Jaccard + **包含度**（`_similarity:467-477`，包含度对「一个标题是另一个的加长版」更敏感），阈值 `DUP_JACCARD=0.70` / `DUP_CONTAINMENT=0.74`（`stages.py:485-486`），并有三重护栏：

   - 显式需求（PRD 里写了编号）永不合并；
   - 标题里的**数值不同则永不合并**（`rerank_score > 0.2` 与 `> 0.39` 是两条规则，`stages.py:494-500`）；
   - 包含度路径要求双方 token 数 ≥ `MIN_TOKENS=6` 且共享 ≥ `MIN_SHARED=6`，拦住「场景筛选」这种短标题被长标题包含造成的误并（`stages.py:487-490`）。

   合并后保留信息量更大的那条，并写入 `mergedFrom`（含原标题、jaccard、containment、shared）供人工审计（`stages.py:561-570`）。阈值来源是历史真实产物（319 条需求）上的网格搜索，注释里给了标注集结果：J=0.70/C=0.74 时真重复 3/3 命中、误并 0 例（`stages.py:481-484`）。

**⑤ 确定性编号**

显式 REQ 保留原号（`idSource="prd"`）；其余按**模块内出现顺序**生成 `REQ-<MODULE>-001…`（`idSource="generated"`），并用 `assigned` 集合避让冲突（`stages.py:668-689`）。这替代了早期的「哈希三位数」编号，理由是顺序号才符合人和下游模型的直觉（`stages.py:586-587`）。

配套两个函数：

- `modules_from_analysis`（`stages.py:692-715`）：从模块清单表提取 `id/name/entry`，缺失时退化为模块代号本身，并把 `requirementIds` 补齐；
- `align_analysis_ids`（`stages.py:718-729`）：把报告里的临时编号**替换**为最终编号，替换按源编号长度降序做，避免前缀互相污染；`pipeline.py:400` 在同一处先抽取再对齐，保证 Markdown 与 JSON 一致。

### 2.6 阶段 1 门禁

`gates.check_analysis`（`gates.py:43-55`）做两件事：

1. **必备章节**：功能模块清单、角色清单、需求点明细、业务规则、非功能需求、风险与疑点（`gates.py:21`），缺失时附「该往哪放」的提示（`gates.py:22-26`）；
2. **REQ 覆盖双向对齐**（仅当给了 `prd_text`）：PRD 里有而报告没有 → 报「遗漏」；报告里有而 PRD 没有 → 报「出现 PRD 中不存在的需求点（如属自动编号请标注「自动编号」）」（`gates.py:46-54`）。

分段合并那次调用用的是 `check_four_sections`（只查 4 个小章节，`gates.py:63-65`）。

---

## 3. 阶段 2：测试方案

阶段 2 的关键动作是**输入压缩**：把阶段 1 报告里体积最大的 §3 需求点明细大表，替换为「一行一需求」的紧凑清单（`stages.compact_analysis:355-361`）：

```
## 3. 需求点明细（摘要，完整见 requirements.json）

- REQ-AUTH-001 [P0] 登录
- REQ-AUTH-002 [P1] 注册
```

理由是完整事实已在 `requirements.json` 里，而方案阶段只需要「有哪些需求、什么优先级」（回归：`tests/test_long_doc.py:274-282`）。

随后两条路径（`pipeline.py:416-425`）：

- `stub` 或 `--no-gate`：`stages.run_stage_2` 单次调用，无门禁；
- 否则：`generate_with_feedback(..., gates.check_plan, system=SYSTEM_ROLES[2], temperature=stage_temperature(2, cfg))`。

模板通过 `extra_instructions` 注入并明确「输出必须按此结构，章节缺一不可」（`stages.py:184-186`）。门禁 `check_plan` 要求 8 个章节：测试范围、测试策略、优先级口径、风险与应对、测试环境与数据、准入、准出、用例量（`gates.py:27-36`）。

方案落盘后即作为阶段 3 的**稳定 system 前缀**（见 §4.1）——因此方案变了会让所有模块的检查点失效（`configHash` 未直接包含 plan 文本，但阶段 3 的检查点校验会拒绝引用不存在的需求，且 `--stage 2` 默认会连带重跑阶段 3，见 §5.3）。

---

## 4. 阶段 3：用例生成

### 4.1 模块并发与 system/user 拆分

`run_stage_3_modules`（`stages.py:784-1057`）先按 `requirement["module"]` 分组（`stages.py:813-816`）；若一条需求都没有模块，退回单次生成 `run_stage_3`（`stages.py:817-818`、`:1300-1314`）。

**system 前缀（跨模块共享、字节一致）**由 `stage_3_system`（`stages.py:216-241`）拼装：

```
<角色：资深测试设计专家>
以下是本次任务各模块共享、保持不变的上下文（测试方案 / 字段约束 / 用例生成约束 / 参考示例）：
## 测试方案
<plan_md>
## 字段约束
<field_spec() 或 markdown_transit 的表格说明>
## 用例生成约束
<constraints.build_case_constraints(examples)>
（UI 契约：render_ui_spec(ui_spec)，若提供）
（图片证据：image_context，若提供）
## 参考示例
<render_examples(...)>
```

这样做的目的是**命中 Prompt 缓存**：测试方案、字段约束、用例约束、示例对整轮运行恒定，放在 system 里就不会逐模块重复发送（`stages.py:199-200`、`:221-223`）。

**user 消息（每模块/每批变化）**由 `build_stage_3_user`（`stages.py:192-213`）拼装，只含三样东西：

| 槽位 | 内容 | 位置 |
| --- | --- | --- |
| `结构化需求（只能引用其中真实 REQ）` | 本批需求的 JSON（不是整个模块） | 输入区（`stages.py:204-205`） |
| `模块原文上下文` | 按 `locate(chunks, query, top_k=CTX_TOP_K)` 选出的 PRD 片段 | 输入区（`stages.py:206-207`） |
| `batch_hint` | 「本批模块代号固定为…只能引用 REQ-<MOD>-*…本批仅覆盖以下 N 条需求，按需生成用例（通常 5~10 条）」 | **输出要求区**（近因最强，`stages.py:209-210`、`:898-903`） |

`CTX_TOP_K` 默认 2（`stages.py:317`），检索 query 是「模块码 + 本批需求标题」拼接（`stages.py:896`）。这是 P0 级 token 优化：早期实现把整份 PRD 塞进每个模块批次（约 30K 字符 ≈ 5.6K token/次），现在只带相关片段（回归：`tests/test_long_doc.py:251-271` 断言 user 消息 < 8000 字符）。

并发结构：`ThreadPoolExecutor(max_workers=max(1, min(jobs, len(grouped))))`，**一个模块一个 worker**，模块内再串行跑批次（`stages.py:1024-1040`）。同样需要在 worker 内重绑指标与取消标志（`stages.py:840-842`）。

**模块级失败策略**：任何一个模块抛错都记进 `failures`，全部模块跑完后一次性 `raise ValidationError(failures)`（`stages.py:1035-1040`）——即「不让一个失败吞掉其他模块的结果」，但也意味着**门禁失败是整轮失败**（除非 `--allow-gate-fail`）。

### 4.2 批次机制：`_module_batch_size` 怎么算

模块内按需求数分批（`stages.py:880-881`）：

```python
batch_size = _module_batch_size(cfg, len(module_reqs))
batches = [module_reqs[i:i + batch_size] for i in range(0, len(module_reqs), batch_size)]
```

`_module_batch_size`（`stages.py:338-345`）的实现：

```python
override = os.environ.get("PRD2TC_CASE_BATCH_SIZE")
if override:
    return max(1, min(int(override), req_count))          # 硬覆盖（旧参数兼容）
max_tokens = int(getattr(cfg, "max_tokens", 0) or 0)
budget = max(2000, int(max_tokens * OUTPUT_BUDGET_RATIO)) if max_tokens > 0 else 7000
return max(1, min(budget // TOKENS_PER_REQ_EST, CASE_BATCH_CAP, req_count))
```

三个常数（`stages.py:333-335`）：

| 常数 | 默认 | 作用 |
| --- | --- | --- |
| `TOKENS_PER_REQ_EST` | 1800（`PRD2TC_TOKENS_PER_REQ`） | 每条需求预期输出 token，实测校准值 |
| `OUTPUT_BUDGET_RATIO` | 0.60（`PRD2TC_CASE_OUTPUT_BUDGET`） | 单批允许占用 `max_tokens` 的比例 |
| `CASE_BATCH_CAP` | 8（`PRD2TC_CASE_BATCH_CAP`） | 单批需求数上限，防止批次随预算膨胀 |

代入默认配置（`max_tokens=16384`）：

```
budget = int(16384 × 0.60) = 9830
batch  = min(9830 // 1800, 8, req_count) = min(5, 8, n) = 5（n ≥ 5 时）
```

即默认每批 **5 条需求**。`max_tokens=8192` 时 `budget=4915` → 每批 2 条。`max_tokens` 为 0（未配置）时退化为固定预算 7000 → 每批 3 条。

**为什么三个常数缺一不可**（`stages.py:319-332` 的注释就是这张论证）：

- `TOKENS_PER_REQ_EST=1800`：唯一完整跑完的 KB 模块实测 1700 tok/需求，被截断的模块给出下限（TOOL 仅 3 条需求就撞满 8192 → ≥2731 tok/需求）。旧值 450 严重偏低，是运行 9d97060e 大面积截断的直接原因。
- `OUTPUT_BUDGET_RATIO=0.60`：批次容量与 `max_tokens` **成正比**。若沿用 0.85，调高 `max_tokens` 只会让批次同比例变大，安全余量恒为 `1/0.85 ≈ 1.18x`，截断率不变。降到 0.60 才真正换来 `1.67x` 余量，让「上调 `max_tokens`」生效。这条不变量由 `tests/test_long_doc.py:165-181`（`test_raising_max_tokens_actually_buys_headroom`）强制守住：断言 `OUTPUT_BUDGET_RATIO <= 0.70` 且单批余量 `≥ 1.5x`。
- `CASE_BATCH_CAP=8`：`tests/test_long_doc.py:184-189` 用一个 `max_tokens=200000` 的极端配置断言批次仍被压到 8。

> 项目记录的历史实测（运行 9d97060e，MiniMax-M3；逐模块数字来自**本次重写前**的 `docs/pipeline.md` §7.2，可用 `git show HEAD:docs/pipeline.md` 复核；代码侧只把「KB=1700 tok/需求」「截断模块 ≥2731」两点写进了 `tests/test_long_doc.py:149-162` 的断言）：
>
> | 模块 | 需求数 | 输出 token | tok/需求 | 状态 |
> | --- | --- | --- | --- | --- |
> | KB | 20 | 34005 | 1700 | 完整 |
> | AGENT | 6 | 16384 | ≥2731 | 截断 |
> | SCENE | 7 | 8192 | ≥1170 | 截断 |
> | TEST | 8 | 8192 | ≥1024 | 截断 |
> | TOOL | 3 | 8192 | ≥2731 | 截断 |
>
> ⚠ 上表逐模块数字我未能在代码或测试中找到独立来源，仅见于同仓旧文档，引用时请以 `1800/9830/5`（TOKENS_PER_REQ_EST / budget / 每批需求数）这组可由公式复算的值为准。

### 4.3 批次是「prompt 级隔离」，不是「契约级隔离」

这是理解阶段 3 的关键，也是最容易误判的一点：

| 层面 | 隔离粒度 | 代码 |
| --- | --- | --- |
| 提示词 | **批次级**：user 消息里的结构化需求只有本批 N 条，`batch_hint` 声明「本批仅覆盖以下 N 条需求」 | `stages.py:905-907`、`:898-903` |
| 上下文 | **模块级**：`module_context` 是整模块的 PRD 片段（`locate` 的结果与批次无关，query 只用模块码+本批标题，可能召回他批内容） | `stages.py:896-897` |
| 门禁允许集合 | **模块级**：`allowed = {module}`，校验用 `validate_case(..., {module})` | `stages.py:861`、`:912`、`:1008` |
| 需求归属 | **批次级**：`batch_req_ids` 只用于两件事——记录越界事实、构造修复提示词 | `stages.py:910`、`:935-939`、`:956-957` |

也就是说：**模型看得见本模块其他批次的需求，门禁也不会因此判错**。跨批重复由确定性后处理兜底（§4.4）。

各模块状态与批次进度通过回调暴露：`status_callback(module, state)`（running/succeeded/failed/skipped）、`batch_callback(module, index, total, phase)`（`module_started`/`batch_started`）、`split_callback`（拆批事件），经 `pipeline.on_batch:362-366` 汇总到实时指标与结构化事件（`pipeline.py:451-475`）。

### 4.4 `dedup_cross_batch`：同一需求只由一个批次供例

背景（`stages.py:737-743`，实测运行 c653dc92）：KB 有 16 条需求 → 切成 2 批（各 8 条），但**两批都产出了覆盖全部 16 条需求的用例**。当时 `all_cases.extend()` 直接叠加，16 条需求各被覆盖两次，KB 42 条用例里 52% 是语义重复（全库约 30% 用例量是重复膨胀）。原有的四道防线全部失效：批次提示只是自然语言建议；门禁允许集合是模块级；`renumber()` 抹掉了批次边界；`case_signature()` 基于文本比较，两批对同一元素用了不同 testid（`kb-search` vs `kb-search-input`）因此判为非重复。

`dedup_cross_batch(per_batch)`（`stages.py:732-781`）的规则是**确定性且不丢覆盖**的：

1. 统计每条需求被哪些批次产出（`produced[ref] = [batch indices]`）；
2. 定「供例批次」`provider[ref]`：
   - 优先取**拥有**该需求的批次（`ref in owners[i]`，`owners` 是批次切分时的固定输入，确定性）；
   - 若拥有批**没有产出**该需求，退回**最早产出它**的批次（`batches[0]`）——这就是「拥有批未产出时回退」，保证不产生覆盖缺口；
3. 逐批逐条筛用例：
   - 没有 `requirement` 引用的用例 → **保留**（无从归属，交给覆盖率门禁处理，`stages.py:774-776`）；
   - 引用的需求**全部**由别批供例 → 丢弃（整体重复）；
   - 引用**混合**（部分属本批）→ 裁剪掉非本批供例的引用后保留，而不是丢掉用例（`stages.py:777-780`）。

返回 `(kept, conflicts)`，`conflicts` 只用于可见性：`reporter` 会打印「检测到跨批次重复覆盖 N 处，已按「需求归属批次」保留唯一供例」（`stages.py:999-1005`），越界事实也会打印一次汇总（`stages.py:991-998`）。

回归测试穷举了这些分支：`tests/test_cross_batch_dedup.py:59-118`（规则）、`:123-165`（伪造一个「无视批次范围、每批都返回全模块用例」的模型，断言最终每个需求恰由 1 条用例覆盖、总数等于需求数）、`:258-285`（模型始终越界 → 由 dedup 收敛而不是丢整个模块）。`dedup_cross_batch` 也是纯函数、确定性（`:112-118`）。

### 4.5 越界引用为什么只记录、不拦截（livelock 事故）

**曾经的做法**：把「引用了本批之外的需求」当门禁错误。

**结果**：模型照提示词做，却被判越界；重试又收到**同样的模块级清单**，于是**永远无法收敛**（livelock），整模块失败。实测运行 `5b526ce6` 的 SCENE（10 条需求 / 2 批）、`9347c6b3` 的 DOC 与 CTX 均因此整模块失败（`stages.py:915-921`）。

**两个根因**：

1. `module_context` 携带的是**整个模块**的 PRD 原文，模型天然看得见本批之外的需求——越界属于预期行为，不是异常；
2. 修复提示词当时用的是**模块级**清单（说「本模块这些需求都可引用」），而门禁只放行本批那几条 → 提示词与门禁**范围不一致**。

**现在的做法**（`stages.py:912-945` 的 `gate` 闭包）：

- 越界引用只写入 `scope_notes`（`(批次号, 越界需求号列表)`），**不进入 `errs`**；
- 真正进入 `errs` 的只有：通用用例校验（`gates.check_cases_text`）与原型契约校验（`prototype.validate_targets`）；
- 归属问题交给 `dedup_cross_batch` 确定性解决。

**为什么可接受**：越界是**可自动纠正的归属偏差**，而把它升级成整轮运行的致命失败，代价与收益严重不成比例（`stages.py:922-927`）。两种情形（拥有批正常产出 / 拥有批完全无产出）都实测能保住全量覆盖。

**两条独立回归**：`tests/test_cross_batch_dedup.py:183-217`（`test_faithful_model_obeying_repair_prompt_does_not_livelock`：一个**完全遵守提示词**的模型绝不能失败）、`:220-255`（越界只记录不致命，但必须可见）。门禁侧的同一条教训（修复提示词范围必须与门禁允许集合一致）另见 `docs/prompts-and-gates.md` §5.4。

> 另有一类引用是**真正致命**的：引用了**其他模块**或**不存在的**需求。它在所有批次跑完后由 `wrong_refs` 检查抛出 `ValidationError`（`stages.py:981-986`），因为那意味着产物的事实错误，而不是归属偏差。

### 4.6 修复提示词：增量修正 + 批次级清单

门禁重试默认「全量重发」（`generate_with_feedback` 的 `repair_builder=None` 路径），但阶段 3 传入了专用构造器 `stage3_repair_builder`（`stages.py:364-390`），只回传：任务指令 + 本批模块代号声明 + **本批真实需求编号清单** + 上一次输出 + 错误清单 + 输出要求（`stages.py:377-388`），不重发测试方案与完整需求 JSON。收益是省 token（回归：`tests/test_long_doc.py:295-328` 断言第二次调用里不出现 `BIG_PLAN_CONTENT`，且含「上一次输出」「错误清单」）。

`allowed_ids` **必须等于本次门禁允许的集合**，且 `scope` 文案要与实际范围一致。调用点传的是 `set(batch_req_ids)` 与 `scope="本批（本模块的一个批次）"`（`stages.py:956-957`）。docstring 里明确写了为什么：多批次场景下若传模块级清单，就会告诉模型「本模块所有需求都可引用」，而门禁只放行本批那几条 → 模型照提示词生成即被判越界 → 重试又收到同样的模块级清单 → 无法收敛（`stages.py:366-372`）。

接线回归专门盯住了「调用点是否传批次级集合」：`tests/test_cross_batch_dedup.py:288-335` 的注释解释了为什么需要单独一条——只测构造函数本身不够，而且**修复②（越界不致命）上线后会掩盖这个错误**，此时唯一可观测差异就是「提示词声明了哪些编号」。

### 4.7 重编号、门禁与检查点

批次全部跑完后（`stages.py:1006-1019`）：

1. `renumber(all_cases, module)`：id 强制重排为 `TC-<MOD>-<3位序号>`，同时**强制改写 `module` 字段**（模块码是批次输入，确定性强制，与 id 重编号同构，不是削弱门禁，`stages.py:828-832`）；
2. 用 `validate_case(c, i, seen, allowed)` 对合并结果再跑一次字段校验（`seen` 跨用例共享以查重复 id）；
3. 通过则写检查点 `save_checkpoint(module, all_cases)`；
4. 回调 `token_callback`（prompt/completion/cache 读/写）与 `status_callback(module, "succeeded")`。

所有模块结束后还有一次**全局校验**（`stages.py:1052-1056`），`allowed = set(grouped)`（即全模块集合），用于发现跨模块的 id 冲突之类问题。

---

## 5. 产物契约与分段执行（`artifacts.py`）

### 5.1 一个产物怎么算「完备」

`Artifact`（`artifacts.py:37-52`）的字段就是契约：`key/rel/kind/stage/label/min_bytes/require_field/fix`。完备性判定分四步（`_check_one:106-137`）：

| 步骤 | 判定 | 失败码 |
| --- | --- | --- |
| 存在 | 路径或 glob 是否命中 | `missing` |
| 非空 | 至少一个文件 ≥ `min_bytes`（0 字节/纯空白视为未产出） | `empty` |
| 可解析 | JSON 类产物能被 `json.loads` 解析 | `malformed` |
| 结构完备 | `require_field` 指定的字段非空 | `incomplete` |

四个产物的具体参数（`artifacts.py:54-69`）：

| 产物 | 路径 | 类型 | 下限 | 额外要求 | 缺失建议 |
| --- | --- | --- | --- | --- | --- |
| 需求分析报告 | `analysis/需求分析报告.md` | markdown | 80B | - | 先跑 `--stage 1` |
| 结构化需求 | `analysis/requirements.json` | json | 2B | `requirements` 非空 | 先跑 `--stage 1`；若为空说明未真正抽取到需求 |
| 测试方案 | `plan/测试方案.md` | markdown | 80B | - | 先跑 `--stage 2` |
| 用例集 | `cases/cases/*.json` | json_glob | 2B | - | 先跑 `--stage 3` |

为什么必须这么细（`artifacts.py:6-20` 的模块 docstring 列了三类**静默错误结果**）：0 字节的 `测试方案.md` 满足 `exists()`，阶段 3 会据此生成用例；`requirements: []` 会让阶段 3 在「没有需求」的前提下跑完；改了 PRD 后直接 `--stage 3` 会用旧方案生成不匹配的用例。这三类都由 `tests/test_stage_resume.py:69-115` 覆盖。

### 5.2 阶段依赖图

```python
STAGE_INPUTS = {
    "1": (),                                        # 阶段 1 只依赖 PRD 本身
    "2": (ANALYSIS_MD, REQUIREMENTS_JSON),
    "3": (PLAN_MD, REQUIREMENTS_JSON),
}
STAGE_OUTPUTS = {
    "1": (ANALYSIS_MD, REQUIREMENTS_JSON),
    "2": (PLAN_MD,),
    "3": (CASES_JSON,),
}
```

（`artifacts.py:75-84`，与 `tests/test_stage_resume.py:180-184` 的依赖图断言一一对应。）

### 5.3 `--stage N` 与 `--from-stage N` 的语义区别

`stages_to_run(stage, from_stage)`（`artifacts.py:214-227`）：

| 调用 | 返回 | 含义 |
| --- | --- | --- |
| `("1")` / `("2")` / `("3")` | `["1"]` / `["2"]` / `["3"]` | **只跑该阶段**，复用已有上游产物 |
| `("all")` | `["1","2","3"]` | 三阶段全跑（默认） |
| `("all", "2")` | `["2","3"]` | **从阶段 2 跑到最后**（补跑语义） |
| `("all", "9")` | `[]`（非法值退化为空） | 调用方需自行处理空列表 |

`stage_plan`（`artifacts.py:230-233`）把它渲染成「执行 / 复用已有产物」的展示表（`tests/test_stage_resume.py:231-233`）。

**为什么要有两个开关**（`pipeline.py:41-44` 的注释就是这条）：`--stage 2` 是「我只想重做方案，别动我的分析」；`--from-stage 2` 是「分析坏了，从方案这一步往下全部重来」。仅有一个的话，用户想补跑上游就只能 `--stage all`（会把没坏的上游也重算），想只重算一段就只能手工删文件。

错误提示也据此生成（`format_problems:195-207`）：

```
阶段 3（cases）无法开始：上游产物不完备（1 项）

  ✗ [empty] 测试方案（plan/测试方案.md）：产物为空或过小（测试方案.md=0B），小于下限 80B；建议：先跑阶段 2（`--stage 2`），或检查该文件是否被清空

提示：要**补跑**缺的上游阶段，用 `--from-stage 2`（从这里跑到最后）；
      只跑阶段 3 而不补上游，用 `--stage 3`（会复用已有上游产物）；
      要不要从头重来，用 `--stage all`（必要时加 `--force`）；
      若确认要复用旧产物，加 `--allow-stale-artifacts`。
```

### 5.4 陈旧产物检测：拒绝「用另一份 PRD 生成的上游产物」

`check_one(..., prd_hash, recorded_hash, allow_stale)`（`artifacts.py:155-177`）在四步完备性都通过后追加新鲜度判定：

```python
if recorded_hash and recorded_hash != prd_hash:
    return [Problem(artifact, "stale",
        f"该产物是为**另一份 PRD** 生成的（记录 {recorded_hash[:12]}… ≠ 当前 {prd_hash[:12]}…）",
        "改为 `--stage all` 全量重跑；若确认要复用旧产物，显式加 `--allow-stale-artifacts`")]
```

三条边界条件同样重要（`tests/test_stage_resume.py:135-143`）：哈希相同不算陈旧；**没有历史记录（首次运行）时跳过新鲜度判断**；产物缺失时优先报 `missing` 而不是 `stale`（否则修复建议会误导）。

#### 实现要点：必须在 `init_output` 重写清单**之前**采样哈希

```python
# **必须在 init_output 之前采样**：init_output 会重写清单，
# 抹掉「已有产物属于哪份 PRD」的记录。若在其后才读，
# 陈旧检测拿到的永远是刚写入的新哈希，等于失效（实测踩到）。
recorded_prd_hash = artifacts.read_manifest_hash(root)          # pipeline.py:240
...
if not request.resume or request.force or (清单不存在):
    init_output(root, product, version, str(path.resolve()))    # pipeline.py:242-243
```

`artifacts.check_one` 的 docstring 还专门解释了**为什么不让它自己去读清单**：pipeline 在 `resume=False` 时会调用 `init_output` 重写清单，若在函数内部才读，拿到的永远是刚写入的新哈希，陈旧检测形同虚设（`artifacts.py:159-163`）。回归测试用「采样值 vs 重读值」的对比把这个差异钉死（`tests/test_stage_resume.py:146-168`）：

```python
sampled = artifacts.read_manifest_hash(root)        # 重写前
... 模拟 init_output 重写 ...
check_one(..., recorded_hash=sampled)               # 能识别 stale
check_one(..., recorded_hash=artifacts.read_manifest_hash(root))  # 检测失效（修复前的行为）
```

**两层防护**：`--resume` 时还有一层前置检查——`old_manifest["prdHash"] != prd_hash` 直接报「`--resume` 输入 PRD 与历史运行不一致；如确认覆盖请使用 `--force`」（`pipeline.py:318-319`）。不带 `--resume` 的 `--stage 3` 没有这一层，因此完全依赖 stale 检测（`tests/test_stage_resume.py:298-331` 明确覆盖了这第二种情形）。

### 5.5 preflight 的实际遍历方式

`pipeline.generate` 的 preflight（`pipeline.py:374-392`）刻意不复用 `artifacts.check_inputs`，而是自己遍历：

```python
running_stages = set(artifacts.stages_to_run(request.stage, request.from_stage))
for stage_key in running_stages:
    for artifact in artifacts.STAGE_INPUTS.get(stage_key, ()):
        if artifact.stage in running_stages:
            continue          # 该产物由本次运行的上游阶段产出
        problems = artifacts.check_one(root, artifact, prd_hash=prd_hash,
                                       recorded_hash=recorded_prd_hash,
                                       allow_stale=request.allow_stale_artifacts)
        if problems:
            raise ValueError(artifacts.format_problems(problems, stage_key))
```

关键在 `if artifact.stage in running_stages: continue`——否则 `--stage all` 会因为「阶段 1 还没生成」而误报（`artifacts.check_inputs:184-186` 的 docstring 也写了这条前置条件）。`artifacts.check_inputs` 是给外部调用方的便捷封装，当前只有测试在用。

---

## 6. 恢复与检查点

### 6.1 `--resume` / `--force` 的判定条件

`can_resume` 需要四个条件**同时**成立（`pipeline.py:320-323`）：

```python
can_resume = bool(request.resume and not request.force
                  and old_state.get("inputHash") == input_hash
                  and old_state.get("promptHash") == prompt_hash
                  and old_state.get("model", self.config.model) == self.config.model
                  and old_state.get("configHash") == config_hash)
```

| 条件 | 含义 | 变了会怎样 |
| --- | --- | --- |
| `inputHash` | 正文 + 资产内容一致 | 换图/换附件 → 旧检查点失效（`ingest.Bundle.fingerprint:154-164`） |
| `promptHash` | 5 个 prompt/template 资产 + 示例库的 sha256 拼接一致 | 改提示词或换示例库 → 旧检查点失效（`pipeline.py:286-295`） |
| `model` | 模型名一致 | 换模型 → 失效 |
| `configHash` | 参数指纹（provider/temperature/maxGateRetries/promptHash/profile/minCases/repro/seed/bundleHash/uiSpecHash/imageSpecHash/maxTokens） | 任一变化 → 失效（`pipeline.py:296-313`） |

`--force` 的作用是**明确放弃旧检查点**：它既让 `can_resume=False`，也让 `init_output` 重建清单骨架（`pipeline.py:242`、`:320`）。

`--resume` 命中后还有两处「整段复用」优化：

- 三阶段全在计划内且 `analysis/需求分析报告.md` 与 `requirements.json` 都在 → `resume_all=True`，阶段 1 整段跳过（`pipeline.py:394-396`）；
- 阶段 2 的条件是 `can_resume and "3" in running_stages and plan 存在`（`pipeline.py:410`）。

### 6.2 模块级检查点 `cases/.checkpoints/<MOD>.json`

检查点的读写由 pipeline 以闭包形式注入 `run_stage_3_modules`（`pipeline.py:450-465`）：

```python
cdir = root/"cases"/".checkpoints"; lock = threading.Lock()
modules = dict(old_state.get("modules", {})) if can_resume else {}

def load(m):    # 只有 can_resume 且 run-state.modules[m] ∈ {succeeded, skipped} 且文件在，才复用
    p = cdir/f"{m}.json"
    return normalize_cases(json.loads(p.read_text(encoding="utf-8"))) \
           if can_resume and modules.get(m) in {"succeeded", "skipped"} and p.exists() else None

def save(m, c): atomic_write_text(cdir/f"{m}.json", json.dumps(c, ensure_ascii=False, indent=2)+"\n"); status(m, "succeeded")

def fail_save(m, text):  # 失败模块的原始输出，写到 cases/.failures/<MOD>.txt
    fdir = root/"cases"/".failures"; fdir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(fdir/f"{m}.txt", text)
```

复用前**必须重新校验**，不能盲信文件（`stages.py:843-856`）：

1. 对检查点的每条用例跑 `validate_case(c, i, seen, {module})`，任何字段级问题都报「检查点无效: …」；
2. 检查它的 `requirement` 引用是否都属于**当前**该模块的需求集合，出现越界/不存在则报「检查点用例 X 引用了其他模块或不存在的需求 Y」；
3. 通过则回调 `status_callback(module, "skipped")`，`load` 返回的批次标记 `from_checkpoint=True`（`stages.py:856`）。

`save` 与 `status` 都通过锁串行化，因为 `jobs>1` 时多个 worker 会并发写 `run-state.json`（`pipeline.py:450-452`）；`token_callback` 同样加了锁，注释里点明了此前是无锁写入、与 `status` 回调并发（`pipeline.py:467-471`）。

写检查点放在模块**全部门禁通过之后**（`stages.py:1006-1014`），因此检查点里不会有「半成品」；失败模块则走 `fail_save` 留原始输出。

---

## 7. 可复现

### 7.1 `--repro` 做了什么

| 动作 | 实现 |
| --- | --- |
| 三阶段温度归零（贪婪解码） | `stage_temperature` 在 `cfg.repro` 时直接 `return 0.0`（`stages.py:59-67`） |
| 强制关闭流式，走非流式 POST | `effective_stream(config) = config.stream and not config.repro`（`llm.py:165-167`）；非流式路径见 `llm.py:226`、`:49` |
| 进入 `configHash` / `run-manifest.parameters` | `pipeline.py:299`、`:333` —— 因此「repro 与非 repro」的检查点不互通，这是刻意的 |

`--repro` **不**自动设置采样种子；种子是独立开关 `--seed` / `PRD2TC_SEED`，只对 OpenAI 兼容端点生效（`llm.py:227-228`，Anthropic/MiniMax 无此参数）。回归测试：`tests/test_realtime.py` 的 `test_repro_forces_non_stream`。

### 7.2 `reproduce.json` 记录什么

成功路径的最后一步写入（`pipeline.py:518-521`），内容来自 `reproduce.run_fingerprint:29-60`：

| 字段 | 来源 | 是否参与对拍 |
| --- | --- | --- |
| `hashes.prd` | `run-manifest.prdHash` | 源判定 |
| `hashes.prompt` | `run-state.promptHash` | 源判定 |
| `hashes.config` | `run-state.configHash` | 源判定 |
| `modules.<MOD>.{requirements,cases}` | `requirements.json` 与 `cases/cases/*.json` 的分模块计数 | 漂移判定 |
| `reqTotal` / `reqCovered` / `caseTotal` | 需求总数、被 active 用例引用的需求数、用例总数 | 漂移判定 |
| `gates.<stage>` | `run-manifest.stages[*].status` | 漂移判定 |
| `runId` | 清单 | 仅标识 |

刻意**不含**自由文本（标题、步骤、预期），因为那属于 LLM 采样范围（`reproduce.py:3-7`）。

### 7.3 `compare-runs` 的结构化指纹对拍

```
prd2tc compare-runs output/p/v1 output/p/v2 [--out report.json] [--fail-on-drift]
```

（`cli.py:273-287`、`reproduce.compare_runs:88-94`。）

1. **先比源**：`hashes` 三个全等 → `sameSource=true`；否则列出 `changedHashes`，并在报告里写明「源不同属正常变更（PRD/Prompt/参数变了），非采样漂移，不算复现失败」（`reproduce.py:110-111`）。
2. **同源才比结构**：逐模块比 `{requirements, cases}`、比 `reqTotal/reqCovered/caseTotal`、比 `gates`，差异进 `drift`；`ok = sameSource && not drift`（`reproduce.py:69-85`）。
3. `--fail-on-drift` 时「不同源或存在漂移」返回非零退出码，可直接用作 CI 卡口。

回归：`tests/test_reproduce.py:39-50`（措辞全改、结构不变 → 指纹完全相同）、`:53-65`（某模块用例数变化 → 漂移定位到该模块）、`:68-78`（只改 PRD → 归因 `changedHashes=["prd"]`）。

### 7.4 除 LLM 之外，还有哪些确定性设计在支撑复现

| 环节 | 确定性做法 | 代码 |
| --- | --- | --- |
| 需求编号 | 模块内顺序编号，显式编号保留 | `stages.py:668-689` |
| 模块码 | 从 PRD 编号前缀/节别名机器锚定 | `stages.py:630-638` |
| 用例 id / 模块 | 批次后统一 `renumber`，模块码强制 | `stages.py:828-832` |
| 用例优先级 | `priority.regrade`：需求优先级 × 维度 + 数据安全例外，原值存 `modelPriority` | `priority.py:226-277` |
| 冒烟集 `@smoke` | 纯函数选取：P0 且非边界/异常、每需求 1 条、上限 `min(25, 20%×总数)`；顺序由数据决定 | `priority.py:163-223` |
| 跨批归属 | `dedup_cross_batch` 纯函数，规则只看 `produced` 与 `owners` | `stages.py:752-781` |
| 评审稿步骤文案 | 确定性自然语言渲染（零 token、零采样） | `nlsteps.py:63-128` |
| 导出字节 | ZIP 固定时间戳 `(2020,1,1,0,0,0)`；XMind topic id 由内容 sha1 派生 | `export/common.py:19`、`export/xmind.py:30-43` |
| 摄取结果 | 成员排序固定 → `Bundle.fingerprint` 稳定 | `ingest.py:37`、`:154-164` |

---

## 8. 门禁与自愈

### 8.1 三层约束（概览）

| 层 | 形态 | 位置 | 失败后果 |
| --- | --- | --- | --- |
| ① Prompt 内「输出自检门」 | 靠模型自觉（`prompts/00-testing-principles.md` 注入各阶段任务指令区） | `constraints.load_principles:74`、`stages.py:172/184` | 无强制力 |
| ② 机器门禁 | 程序化校验，返回错误清单 | `gates.py`、`validator.py`、`prototype.validate_targets` | 触发 ③ |
| ③ 反馈重出 | 把错误清单回灌给模型修正，最多 `max_gate_retries` 次 | `stages.generate_with_feedback:1317-1405` | 仍不过 → 阶段/整轮失败（除非 `--allow-gate-fail`） |

阶段 3 的门禁是「通用校验 + 契约校验」的组合闭包（`stages.py:912-945`）；阶段 1/2 直接传 `gates.check_*`（`stages.py:1188-1194`、`pipeline.py:421-425`）。逐条门禁清单、三层约束的分工与「示例必须过门禁」的一致性要求，见 `docs/prompts-and-gates.md`；约束登记表见 `docs/prompt-gate-optimization.md`（`validator.py:32` 要求改门禁时同步登记）。

### 8.2 截断为什么无条件报错

`generate_with_feedback` 里有一段**刻意不看 `errors` 是否为空**的逻辑（`stages.py:1358-1384`）：

```python
errors = gate_fn(result.content) if gate_fn else []
if result.truncated:          # finish_reason ∈ {length, max_tokens}
    # 截断是**独立于内容错误**的根因，必须无条件报错。
    if looks_like_reasoning_exhausted(result.content, finish_reason=result.finish_reason):
        errors = ["⚠ 诊断：输出预算被**思考过程**耗尽（finish_reason=length，正文为空）…"] + errors
    else:
        errors = [f"⚠ 诊断：模型输出被长度截断（finish_reason={result.finish_reason}），本次输出**不完整**…"] + errors
```

**事故本体**（`stages.py:1361-1365`，运行 9d97060e）：输出被 `max_tokens` 截断后，`extract_cases` 的「修补截断 JSON」回退会 `rfind("]")` 找到用例内部 `tags`/`requirement` 的 `]`，从而修补出一个**合法但残缺**的数组 → 门禁只看修补后的部分 → 零错误 → 模块被判 `succeeded`、检查点只存下残缺用例，直到最后 `traceability` 才发现 P0 未覆盖。当时 AGENT 只产出 1 条（6 条需求）、SCENE 只 2 条（7 条需求）。

**为什么修补后的结构能骗过门禁**：`tests/test_budget_healing.py:42-48` 用一条测试把这个前提固定下来——残缺 JSON 能被修补成合法数组，且 `check_cases_text` 返回空错误列表。随后 `:51-64` 断言「只要 `finish_reason=length` 就必须报错，不能因修补成功而放过」。

两种诊断文案的判别依据是 `reasoning.looks_like_reasoning_exhausted`（`reasoning.py:126-139`）：`finish_reason ∈ {length, max_tokens}` **且** 存在未闭合的 `<think>` **且** 正文里没有 `[`/`{`。测试覆盖了对照项：正常结束（`finish_reason=stop`）不得误报（`tests/test_budget_healing.py:80-89`）。

### 8.3 批次对半拆分自愈

`_split_batch_if_budget_exhausted`（`stages.py:1267-1297`）在门禁失败后、抛错前被调用（`stages.py:959-967`）：

```python
if depth >= MAX_BATCH_SPLIT_DEPTH or len(batch) <= 1: return False
if not getattr(result, "truncated", False): return False
mid = len(batch) // 2
for offset, half in enumerate((batch[:mid], batch[mid:])):
    pending.insert(offset, (half, depth + 1))
```

| 规则 | 取值/行为 | 理由 |
| --- | --- | --- |
| 触发条件 | **任何** `finish_reason=length`，不限「思考耗尽」 | 截断的本质是「单次要输出的内容超过 `max_tokens`」，无论是模型啰嗦、推理占用还是批量过大，正确杠杆都是**降低单次输出量**（`stages.py:1272-1277`） |
| 拆分深度上限 | `MAX_BATCH_SPLIT_DEPTH = 2`（`stages.py:1264`） | 2 层足够把 8 条需求降到 2 条；再拆下去调用次数与成本不可控 |
| 单条需求不再拆 | `len(batch) <= 1` 返回 False | 拆到 1 条仍截断说明是模型/上限问题，应让模块失败并暴露 |
| 队列插入位置 | `pending.insert(offset, ...)`，拆出的两半插到队首 | 自愈重试优先于后续新批次，缩短反馈回路 |
| 事件与文案 | `split_callback` + 区分「推理耗尽」/「长度截断」两种说明 | 便于定位根因（测试 `tests/test_budget_healing.py:181-193`） |

`pending` 是一个「动态增长的工作队列」（`stages.py:887-889`），批次号 `batch_index` 按**出队顺序**递增，因此拆批会产生新的批次号，进度回调总数也会随之变化（`stages.py:893-895`）。

端到端回归：`tests/test_budget_healing.py:217-254`（4 条需求 → 首轮「截断+全思考」失败 → 拆成 2 批后成功，最终 4 条需求全覆盖）。

### 8.4 阶段级与运行级门禁

| 检查 | 何时 | 失败处置 |
| --- | --- | --- |
| 阶段 1 `check_analysis` | 生成后（含分段合并后全量再查一次） | 重试 → 仍失败则 `pipeline._fail`（`pipeline.py:405`） |
| 阶段 2 `check_plan` | 生成后 | 同上（`pipeline.py:433`） |
| 阶段 3 批内 `check_cases_text` + 原型校验 | 每批 | 重试 → 拆批 → 仍失败该模块 `failed` |
| 阶段 3 模块内 `wrong_refs` | 批次返回后 | 直接 `ValidationError`（引用其他模块/不存在需求） |
| 覆盖率 `validate_traceability` | 全部用例汇总后 | 未知需求引用、重复映射、**未覆盖 P0** 均报错（`traceability.py:49-54`） |

覆盖率阈值来自 profile 或 `--min-cases-per-requirement`（`pipeline.py:226`），传给 `build_traceability(reqs, cases, min_cases=...)`（`pipeline.py:483`）。`--allow-gate-fail` 会把这些错误降级为「记录但不中断」。

---

## 9. 失败产物处置与可观测性

### 9.1 为什么写入 `cases/partial/` 而不是丢弃

门禁失败时（`pipeline.py:484-502`）：

```python
if errors and not request.allow_gate_fail:
    # 门禁失败时**不再丢弃**已生成的用例：实测运行 9d97060e 已产出 43 条，
    # 却因最后一步覆盖率不过而全部消失，用户只看到「失败」与空目录。
    if cases:
        save_cases(root/"cases"/"partial", cases)
        atomic_write_text(root/"cases"/"partial"/"READ-ME-部分用例未通过门禁.md", ...)
    manifest_stage(root, "cases", ..., status="failed", gate={"errors": errors},
                   moduleTokens=module_tokens, partialCaseCount=len(cases))
    return self._fail(result, errors)
```

`READ-ME-部分用例未通过门禁.md` 的内容是**可操作**的：标题写明「未通过门禁，请勿直接采信」，列出全部门禁错误，并给出四步处置建议（判断根因类型 → 重试失败模块 → 编辑 PRD 重新分析 → 不要把本目录当正式交付）（`pipeline.py:492-501`）。

**设计取舍**：正式产物 `cases/cases/` 在门禁未通过时**保持为空**（`save_cases` 只在成功路径调用），因此 `partial/` 不会被误当成交付结果；但它会出现在 `artifact_inventory` 里，便于下载后人工修复。数量记入 `stages.cases.partialCaseCount`。

### 9.2 失败信号的三个层次

| 层次 | 载体 | 内容 |
| --- | --- | --- |
| 运行级 | `run-manifest.status` / `run-state.status` | `failed`；异常路径还会写 `error: format_exception(exc)`（`pipeline.py:523-530`） |
| 阶段级 | `run-manifest.stages.<name>` | `status`、`gate.errors`、`model`、`tokens`、`durationMs`、`retries`、`moduleTokens`、`exports` |
| 模块级 | `run-state.modules` + `cases/.failures/<MOD>.txt` | 模块状态（`succeeded`/`skipped`/`failed`）；失败模块的原始输出样本 |

`_fail`（`pipeline.py:532-538`）除写清单外，还会把仍在运行的模块在实时指标里标记为失败并**附上门禁原因**（`metrics.fail_active:167-181`），再出最终快照——否则 UI 只看到 `failed` 却不知道原因。

### 9.3 可观测性：实时指标与结构化事件

| 通道 | 契约 | 谁在用 |
| --- | --- | --- |
| `progress_callback(stage, percent)` | 固定的 33/66/100 三档 | CLI 打印（`cli.py:91-93`）、服务任务状态（`service.py:366-372`） |
| `event_callback(payload)` | `{type: stage_started/module_started/batch_started/module_progress/module_finished/...}` | 服务写事件流 → WS 推送（`pipeline.py:348-360`、`service.py:374-383`） |
| `LiveMetrics` 快照 | 阶段/模块/批次、prompt/completion token（估算标 `≈`）、首 token 延迟、**静默时长**、思考字符数、正文尾部预览 | WS 按需推送（`metrics.py:185-262`、`service.py:979-1005`） |
| 心跳 | 每 5s 一条 `heartbeat` 事件（含静默秒数与已用时长） | `service.py:385-397` |

`metrics.delta`（`metrics.py:94-126`）在热路径上只做整数累加与有界字符串拼接，并用 `ThinkScanner` 在线把 `<think>` 内容归入推理通道——这样看板上的「正在产出」不会被思考污染（`metrics.py:227-232`）。取消通过 `CancelFlag` 传递，`call_llm` 以 `should_cancel` 钩子注入（`stages.py:1107-1108`），流式回调逐块检查即真正止损（`metrics.py:306-316`）。

---

## 10. 关键不变量速查

改动流水线时，下面这些约束如果被破坏，现有回归测试会（或应该）失败：

| # | 不变量 | 守护它的测试 |
| --- | --- | --- |
| 1 | 只跑 `--stage N` 时不得重写上游产物文件 | `test_stage_resume.py:238-271` |
| 2 | 空/残缺/陈旧上游产物必须**明确失败** | `test_stage_resume.py:85-115`、`:119-126`、`:298-331` |
| 3 | 陈旧判定必须用清单重置**之前**采样的哈希 | `test_stage_resume.py:146-168` |
| 4 | `finish_reason=length` 一律报错，不看内容是否可修补 | `test_budget_healing.py:51-64` |
| 5 | 截断 → 拆批（≤2 层），拆批后需求不丢不重 | `test_budget_healing.py:124-137`、`:217-254` |
| 6 | 同一需求只能有一个供例批次（含拥有批未产出的回退） | `test_cross_batch_dedup.py:59-118` |
| 7 | 修复提示词只能声明**本批**需求编号 | `test_cross_batch_dedup.py:167-180`、`:288-335` |
| 8 | 完全遵守提示词的模型绝不能失败（反 livelock） | `test_cross_batch_dedup.py:183-217` |
| 9 | 调高 `max_tokens` 必须换来真实输出余量 | `test_long_doc.py:165-181` |
| 10 | `maxTokens` 变化必须使 `--resume` 不复用旧检查点 | `test_long_doc.py:213-239` |
| 11 | 长度最大的章节由程序化渲染，不依赖单次 LLM 输出 | `test_long_doc.py:34-46` |
| 12 | 出界/编造 target 被原型契约拦下（有 spec 时） | `tests/test_prototype.py` 的 `validate_targets_*` 系列、`test_stage_3_gate_rejects_fabricated_target_when_spec_present` |
| 13 | 结构指纹对措辞不敏感、对结构变化敏感 | `test_reproduce.py:39-78` |
| 14 | `--repro` 必须强制非流式 | `tests/test_realtime.py` 的 `test_repro_forces_non_stream` |

---

## 11. 本文未验证/存疑的点

写明而不是含糊带过，便于复核：

1. **§4.2 的逐模块实测表**（KB 20 条/34005 tok 等）只出现在本次重写前的 `docs/pipeline.md` §7.2（可用 `git show HEAD:docs/pipeline.md` 复核），我未在代码、测试或产物中找到独立来源；代码侧只有「1700 tok/需求」与「≥2731」两个锚点被测试断言（`tests/test_long_doc.py:149-162`）。可由公式复算的是：默认配置下 `budget=9830`、每批 `5` 条需求。
2. **`field_spec` 的 `allowed_modules` 分支是死代码**：全仓只有 `stages.py:126` 的定义与 `stages.py:229` 的无参调用，没有任何调用方传 `allowed_modules`（连测试也没有）。其 docstring 声称「用于非按模块批次的 `run_stage_3` 回退路径」，但 `run_stage_3:1306` 同样没有传参。因此该分支的实际行为未经验证。
3. **`task.json` 里 `request.stage` 与 `from_stage` 的关系**：服务层只用 `stage`（`service.py:306-309` 的 `require_stage` 只接受 `1/2/3/all`），因此 `--from-stage` 语义在服务路径上不可用；「重跑单阶段」是通过 `regenerate_stage` 丢弃下游产物来实现的（`service.py:803-854`）。这条我是从代码结构推断的，没有跑过服务端到端。
4. **`manifest.json` 与 `run-manifest.json` 的主次定义在两个模块里相反**（`io.py:18-19` vs `artifacts.py:29-30`）。当前两者字节相同、读侧都有兜底，所以无功能影响；我没有构造「只写其一」的场景去验证。
5. **阶段 2 与阶段 3 之间的复用边界**：`pipeline.py:410` 只在「阶段 3 也要跑且方案存在」时跳过阶段 2，但 `configHash` 并不包含 `plan_md` 的哈希，因此「方案文件被手工替换」这一情形不会被检测到（阶段 3 的检查点校验只查需求引用，不查方案一致性）。我未编写实验验证这一路径。
6. **`tests/` 用例总数**：静态可数的 `def test_` 为 637 个（参数化会展开更多）；任务说明中的 663 个未在仓库内找到可直接核对的记录。前端 74 个用例与 `ui/src/test/` 中的 `it(`/`test(` 计数一致。
